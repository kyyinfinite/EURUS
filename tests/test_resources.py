from __future__ import annotations

import asyncio
import gc
import importlib.util
import logging
import sqlite3
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eurus.gateway.approval import ApprovalBroker
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.limits import RateLimiter
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.router import GatewayRouter
from eurus.gateway.tasks import PRUNE_EVERY_CLAIMS, DurableTask, TaskStore, scope_key
from eurus.gateway.telegram.adapter import TelegramNotifier
from eurus.providers.base import MAX_RETRY_LIMIT, RetryPolicy
from eurus.runtime.event_bridge import AgentEventBridge


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def platform(self) -> str:
        return "test"

    async def send_text(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


def iso_ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def insert_update(store: TaskStore, update_id: str, received_at: str) -> None:
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO gateway_updates(platform, update_id, received_at) VALUES (?, ?, ?)",
            ("telegram", update_id, received_at),
        )


def count_updates(store: TaskStore) -> int:
    with sqlite3.connect(store.path) as db:
        return int(db.execute("SELECT COUNT(*) FROM gateway_updates").fetchone()[0])


def test_retry_policy_rejects_unbounded_or_negative_settings() -> None:
    assert RetryPolicy(max_retries=MAX_RETRY_LIMIT).max_retries == MAX_RETRY_LIMIT
    for options in (
        {"max_retries": MAX_RETRY_LIMIT + 1},
        {"max_retries": -1},
        {"base_delay": -0.1},
        {"max_delay": -1},
        {"max_retry_after": -1},
    ):
        with pytest.raises(ValueError):
            RetryPolicy(**options)


def test_retry_delay_is_bounded_for_any_attempt_number() -> None:
    policy = RetryPolicy(max_retries=3, base_delay=0.5, max_delay=8.0, max_retry_after=30.0)
    ceiling = max(policy.max_delay, policy.max_retry_after)
    for attempt in (-5, 0, 1, 5, 10, 64, 5000):
        assert 0 <= policy.delay(attempt, None) <= policy.max_delay
        assert 0 <= policy.delay(attempt, 10**9) <= ceiling
    assert policy.delay(0, 12.0) == 12.0
    assert policy.delay(0, 10**9) == 30.0


def test_rate_limiter_allows_a_burst_then_rejects_until_the_window_slides() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 10.0, clock=clock)
    assert [limiter.allow("a") for _ in range(5)] == [True, True, True, False, False]
    assert limiter.retry_after("a") == pytest.approx(10.0)
    clock.now += 4.0
    assert not limiter.allow("a")
    assert limiter.retry_after("a") == pytest.approx(6.0)
    clock.now += 6.0
    assert limiter.allow("a")
    assert limiter.retry_after("b") == 0.0


def test_rate_limiter_isolates_keys_and_ignores_rejected_attempts() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.allow("a")
    for _ in range(100):
        clock.now += 0.05
        assert not limiter.allow("a")
    assert limiter.allow("b")
    clock.now = 10.0
    assert limiter.allow("a")


def test_rate_limiter_memory_stays_bounded_under_many_keys() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 60.0, max_keys=50, clock=clock)
    for index in range(5000):
        clock.now += 0.001
        assert limiter.allow(f"user-{index}")
        assert limiter.tracked_keys <= 50
    clock.now += 120.0
    assert limiter.allow("fresh")
    assert limiter.tracked_keys <= 2


def test_rate_limiter_validates_arguments() -> None:
    for args in ((0, 1.0), (1, 0.0), (1, -1.0)):
        with pytest.raises(ValueError):
            RateLimiter(*args)
    with pytest.raises(ValueError):
        RateLimiter(1, 1.0, max_keys=0)


@pytest.mark.asyncio
async def test_router_throttles_floods_without_starving_other_users(tmp_path: Path) -> None:
    gate = asyncio.Event()
    adapter = RecordingAdapter()
    store = TaskStore(tmp_path / "tasks.sqlite3")

    async def runner(task: DurableTask) -> str:
        await gate.wait()
        return "ok"

    manager = BackgroundTaskManager(store, runner, adapter.send_text, max_concurrent=8)
    broker = ApprovalBroker()
    router = GatewayRouter(
        adapter=adapter,
        access=lambda message: True,
        store=store,
        manager=manager,
        broker=broker,
        submit_limiter=RateLimiter(5, 60.0),
    )
    for index in range(50):
        await router.handle(GatewayMessage("test", "1", "10", f"m{index}", f"job {index}"))
    await router.handle(GatewayMessage("test", "2", "20", "other", "my job"))

    assert len(store.list_owner(scope_key("test", "1"), 100)) == 5
    assert len(store.list_owner(scope_key("test", "2"), 100)) == 1
    assert sum(text.startswith("Too many requests") for text in adapter.texts()) == 45
    gate.set()
    await asyncio.gather(*manager.active.values())
    await manager.shutdown()
    await broker.close()


@pytest.mark.asyncio
async def test_redelivered_messages_do_not_consume_rate_limit_budget(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    store = TaskStore(tmp_path / "tasks.sqlite3")

    async def runner(task: DurableTask) -> str:
        return "ok"

    manager = BackgroundTaskManager(store, runner, adapter.send_text)
    broker = ApprovalBroker()
    limiter = RateLimiter(2, 60.0)
    router = GatewayRouter(
        adapter=adapter,
        access=lambda message: True,
        store=store,
        manager=manager,
        broker=broker,
        submit_limiter=limiter,
    )
    for _ in range(5):
        await router.handle(GatewayMessage("test", "1", "10", "same-id", "job"))
    await router.handle(GatewayMessage("test", "1", "10", "second", "job two"))
    assert len(store.list_owner(scope_key("test", "1"), 100)) == 2
    assert not any(text.startswith("Too many requests") for text in adapter.texts())
    await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await manager.shutdown()
    await broker.close()


@pytest.mark.asyncio
async def test_per_owner_capacity_prevents_one_user_from_taking_every_slot(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    async def runner(task: DurableTask) -> str:
        await gate.wait()
        return "ok"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    manager = BackgroundTaskManager(
        TaskStore(tmp_path / "tasks.sqlite3"),
        runner,
        notify,
        max_concurrent=1,
        max_pending_per_owner=2,
    )
    await manager.submit("a", "s", "one")
    await manager.submit("a", "s", "two")
    with pytest.raises(RuntimeError, match="owner task capacity"):
        await manager.submit("a", "s", "three")
    await manager.submit("b", "s", "other user")
    assert len(manager._owners) == 3

    gate.set()
    await asyncio.gather(*manager.active.values())
    await asyncio.sleep(0)
    assert manager._owners == {}
    await manager.submit("a", "s", "again")
    await asyncio.gather(*manager.active.values())
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelling_a_task_before_it_starts_leaves_no_bookkeeping(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        return "never"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = BackgroundTaskManager(store, runner, notify, max_concurrent=1)
    task = await manager.submit("1", "s", "work")
    assert await manager.cancel(task.task_id, "1")
    await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await asyncio.sleep(0)
    assert manager.active == {}
    assert manager.cancelled == set()
    assert manager._owners == {}
    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "cancelled"
    await manager.shutdown()


def test_manager_validates_owner_capacity(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        return "ok"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    with pytest.raises(ValueError):
        BackgroundTaskManager(
            TaskStore(tmp_path / "tasks.sqlite3"), runner, notify, max_pending_per_owner=0
        )


def test_prune_updates_removes_only_expired_dedupe_rows(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3", update_retention=3600.0)
    insert_update(store, "old-1", iso_ago(hours=3))
    insert_update(store, "old-2", iso_ago(days=2))
    insert_update(store, "recent", iso_ago(minutes=5))
    assert store.prune_updates() == 2
    assert count_updates(store) == 1
    assert not store.claim_update("telegram", "recent")
    assert store.claim_update("telegram", "old-1")


def test_claim_update_prunes_periodically_so_the_table_cannot_grow_forever(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3", update_retention=3600.0)
    for index in range(20):
        insert_update(store, f"stale-{index}", iso_ago(days=1))
    store._claims_since_prune = PRUNE_EVERY_CLAIMS - 1
    assert store.claim_update("telegram", "trigger")
    assert count_updates(store) == 1
    assert store._claims_since_prune == 0
    assert store.claim_update("telegram", "next")
    assert store._claims_since_prune == 1


def test_update_retention_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TaskStore(tmp_path / "tasks.sqlite3", update_retention=0)


def test_prune_tasks_only_touches_old_finished_tasks_and_supports_dry_run(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    old_done = store.create("1", "s", "old done")
    old_failed = store.create("1", "s", "old failed")
    old_running = store.create("1", "s", "old running")
    old_interrupted = store.create("1", "s", "old interrupted")
    fresh_done = store.create("1", "s", "fresh done")
    for task in (old_done, fresh_done):
        store.transition(task.task_id, "running")
        store.complete(task.task_id, {"result": "ok"})
    store.transition(old_failed.task_id, "running")
    store.transition(old_failed.task_id, "failed", error="Boom")
    store.transition(old_running.task_id, "running")
    store.interrupt(old_interrupted.task_id, "shutdown")
    stale = iso_ago(days=200)
    aged = (old_done, old_failed, old_running, old_interrupted)
    with sqlite3.connect(store.path) as db:
        db.executemany(
            "UPDATE gateway_tasks SET updated_at=? WHERE task_id=?",
            [(stale, task.task_id) for task in aged],
        )

    assert store.prune_tasks(90, dry_run=True) == 2
    assert len(store.list_owner("1", 100)) == 5
    assert store.prune_tasks(90, dry_run=False) == 2
    remaining = {task.task_id for task in store.list_owner("1", 100)}
    assert remaining == {old_running.task_id, old_interrupted.task_id, fresh_done.task_id}
    assert store.prune_tasks(90, dry_run=False) == 0
    with pytest.raises(ValueError):
        store.prune_tasks(0)


@pytest.mark.asyncio
async def test_notifier_rate_limit_state_stays_bounded_across_many_chats() -> None:
    clock = FakeClock()

    async def sender(chat_id: int, text: str) -> None:
        return None

    async def no_sleep(delay: float) -> None:
        clock.now += delay

    notifier = TelegramNotifier(
        sender, global_interval=0, chat_interval=1.0, sleep=no_sleep, clock=clock
    )
    for chat in range(6000):
        clock.now += 2.0
        await notifier(chat + 1, "hello")
    assert len(notifier._next_chat_send) <= 2049


@pytest.mark.asyncio
async def test_mixed_workload_leaves_no_residual_tasks_or_state(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    bridge = AgentEventBridge()
    release = asyncio.Event()
    adapter = RecordingAdapter()

    async def runner(task: DurableTask) -> str:
        kind = task.objective
        if kind == "fail":
            raise RuntimeError("boom")
        if kind == "hang":
            await asyncio.Event().wait()
        if kind == "wait":
            await release.wait()
        return kind

    manager = BackgroundTaskManager(
        store,
        runner,
        adapter.send_text,
        max_concurrent=4,
        task_timeout=0.2,
        event_bridge=bridge,
    )
    manager.approval_broker = ApprovalBroker()
    kinds = ["ok", "fail", "hang", "wait"]
    submitted: list[DurableTask] = []
    index = 0
    while len(submitted) < 160:
        try:
            owner = f"user-{index % 7}"
            session = f"session-{index % 11}"
            submitted.append(await manager.submit(owner, session, kinds[index % 4]))
            index += 1
        except RuntimeError:
            release.set()
            for task in submitted[-8:]:
                await manager.cancel(task.task_id, task.owner_id)
            await asyncio.gather(*manager.active.values(), return_exceptions=True)
    for task in submitted[::5]:
        await manager.cancel(task.task_id, task.owner_id)
    release.set()
    await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await asyncio.sleep(0.05)

    assert manager.active == {}
    assert manager.cancelled == set()
    assert manager._owners == {}
    assert bridge.subscriber_count == 0
    assert manager.approval_broker.pending == {}
    pending = [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]
    assert pending == []
    live = {"queued", "running", "waiting_approval", "waiting_input", "retry_wait", "verifying"}
    assert not live & set(store.count_by_status())
    await manager.shutdown()
    await manager.approval_broker.close()
    bridge.close()


@pytest.mark.asyncio
async def test_task_throughput_does_not_retain_memory_per_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")

    async def runner(task: DurableTask) -> str:
        return "ok"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    manager = BackgroundTaskManager(store, runner, notify, max_concurrent=4)

    async def batch(count: int) -> None:
        done = 0
        while done < count:
            try:
                await manager.submit("1", "s", "work")
                done += 1
            except RuntimeError:
                await asyncio.gather(*manager.active.values(), return_exceptions=True)
        await asyncio.gather(*manager.active.values(), return_exceptions=True)
        await asyncio.sleep(0)

    eurus_logger = logging.getLogger("eurus")
    previous_level = eurus_logger.level
    eurus_logger.setLevel(logging.WARNING)
    await batch(50)
    gc.collect()
    tracemalloc.start()
    growth: list[int] = []
    try:
        for _ in range(3):
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            await batch(300)
            gc.collect()
            growth.append(tracemalloc.get_traced_memory()[0] - before)
    finally:
        tracemalloc.stop()
        eurus_logger.setLevel(previous_level)
    assert min(growth) < 512 * 1024, growth
    await manager.shutdown()


@pytest.mark.asyncio
async def test_benchmark_harness_runs_and_reports_no_leaks() -> None:
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "gateway_bench.py"
    spec = importlib.util.spec_from_file_location("gateway_bench", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = SimpleNamespace(
        store_ops=5, events=50, tasks=12, runs=6, sessions=3, scale_rows=200, json=False
    )
    report: dict[str, Any] = await module.run_all(args)
    assert report["store"]["create"]["ops"] == 5
    assert report["scale"]["rows"] == 200
    assert all(item["leaked_entries"] == 0 for item in report["manager"])
    assert report["runtime"]["active_runs_after"] == 0
    assert report["memory"]["asyncio_tasks_left"] == 0
    assert "store.create" in module.render(report)
