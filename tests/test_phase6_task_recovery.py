from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from eurus.gateway.tasks import (
    TRANSITIONS,
    InvalidTaskTransition,
    RecoveryBlocked,
    TaskStatus,
    TaskStore,
)
from eurus.gateway.telegram.adapter import BackgroundTaskManager
from eurus.gateway.telegram.approval import TelegramApprovalBroker
from eurus.runtime.models import RunStatus
from eurus.runtime.state_machine import ALLOWED_TRANSITIONS, can_transition


def make_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.sqlite3")


async def noop_notify(chat_id: int, text: str) -> None:
    return None


def test_gateway_transitions_are_derived_from_runtime_state_machine() -> None:
    assert set(TRANSITIONS) == {status.value for status in RunStatus}
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert TRANSITIONS[status.value] == {target.value for target in targets}
    assert can_transition(RunStatus.QUEUED, RunStatus.INTERRUPTED)
    assert not can_transition(RunStatus.FAILED, RunStatus.QUEUED)


@pytest.mark.parametrize(
    ("path", "target"),
    [
        (["running"], "running"),
        ([], "completed"),
        ([], "interrupted_unknown"),
        (["running", "failed"], "queued"),
        (["running", "failed"], "running"),
        (["cancelled"], "running"),
        (["running", "verifying", "completed"], "failed"),
    ],
)
def test_invalid_transitions_are_rejected_and_state_is_unchanged(
    tmp_path: Path, path: list[str], target: str
) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "objective")
    for step in path:
        store.transition(task.task_id, cast(TaskStatus, step))
    before = store.get(task.task_id)
    with pytest.raises(InvalidTaskTransition):
        store.transition(task.task_id, cast(TaskStatus, target))
    assert store.get(task.task_id) == before


def test_unknown_task_transition_raises_key_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(KeyError):
        store.transition("missing", "running")


def test_complete_requires_running_or_verifying(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "objective")
    with pytest.raises(InvalidTaskTransition):
        store.complete(task.task_id, {"result": "x"})
    store.transition(task.task_id, "running")
    done = store.complete(task.task_id, {"result": "x"})
    assert done.status == "completed"
    assert done.state == {"result": "x"}
    with pytest.raises(InvalidTaskTransition):
        store.complete(task.task_id, {"result": "y"})


def test_legacy_statuses_are_migrated_on_open(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    paused = store.create("1", "s", "a")
    recovering = store.create("1", "s", "b")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE gateway_tasks SET status='paused' WHERE task_id=?", (paused.task_id,))
        db.execute(
            "UPDATE gateway_tasks SET status='recovering' WHERE task_id=?",
            (recovering.task_id,),
        )
    reopened = TaskStore(store.path)
    first = reopened.get(paused.task_id)
    second = reopened.get(recovering.task_id)
    assert first is not None and first.status == "interrupted"
    assert second is not None and second.status == "interrupted"


def test_claim_is_atomic_across_concurrent_claimants(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "objective")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(task.task_id), range(16)))
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"
    stored = store.get(task.task_id)
    assert stored is not None and stored.state["attempts"] == 1


def test_claim_rejects_tasks_that_are_not_queued(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "objective")
    store.transition(task.task_id, "cancelled")
    assert store.claim(task.task_id) is None
    assert store.claim("missing") is None


def test_orphaned_tasks_are_interrupted_with_recovery_classification(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    queued = store.create("1", "s", "queued")
    bare = store.create("1", "s", "running without checkpoint")
    in_flight = store.create("1", "s", "running mid side effect", {"checkpoint": {}})
    clean = store.create("1", "s", "running with clean checkpoint", {"checkpoint": {}})
    finished = store.create("1", "s", "finished")
    store.transition(bare.task_id, "running")
    store.transition(in_flight.task_id, "running")
    store.transition(clean.task_id, "running")
    store.transition(finished.task_id, "running")
    store.complete(finished.task_id, {"result": "ok"})
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE gateway_tasks SET state_json=? WHERE task_id=?",
            ('{"checkpoint": {"in_flight_side_effect": true}}', in_flight.task_id),
        )

    assert store.interrupt_orphaned() == 4

    def recovery(task_id: str) -> dict[str, str]:
        stored = store.get(task_id)
        assert stored is not None and stored.status == "interrupted"
        value = stored.state["recovery"]
        assert isinstance(value, dict)
        return value

    assert recovery(queued.task_id)["action"] == "start_fresh"
    assert recovery(bare.task_id)["action"] == "manual_review"
    assert recovery(in_flight.task_id)["action"] == "reconcile_unknown_outcome"
    assert recovery(clean.task_id)["action"] == "resume_checkpoint"
    assert recovery(clean.task_id)["previous_status"] == "running"
    untouched = store.get(finished.task_id)
    assert untouched is not None and untouched.status == "completed"
    assert store.interrupt_orphaned() == 0


def test_recoverable_lists_only_tasks_safe_to_resume(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    safe = store.create("1", "s", "never started")
    risky = store.create("1", "s", "was running")
    store.transition(risky.task_id, "running")
    store.interrupt_orphaned()
    assert [item.task_id for item in store.recoverable()] == [safe.task_id]


def test_requeue_requires_acknowledgement_for_possible_side_effects(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    risky = store.create("1", "s", "was running")
    store.transition(risky.task_id, "running")
    store.interrupt_orphaned()

    with pytest.raises(RecoveryBlocked):
        store.requeue_interrupted(risky.task_id, "1")
    still = store.get(risky.task_id)
    assert still is not None and still.status == "interrupted"
    with pytest.raises(KeyError):
        store.requeue_interrupted(risky.task_id, "other-owner", acknowledge_side_effects=True)

    requeued = store.requeue_interrupted(risky.task_id, "1", acknowledge_side_effects=True)
    assert requeued.status == "queued"
    assert requeued.error == ""
    with pytest.raises(InvalidTaskTransition):
        store.requeue_interrupted(risky.task_id, "1", acknowledge_side_effects=True)


def test_requeue_allows_tasks_that_never_started(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "never started")
    store.interrupt_orphaned()
    assert store.requeue_interrupted(task.task_id, "1").status == "queued"


def test_interrupt_rejects_terminal_tasks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create("1", "s", "objective")
    store.transition(task.task_id, "cancelled")
    with pytest.raises(InvalidTaskTransition):
        store.interrupt(task.task_id, "shutdown")


@pytest.mark.asyncio
async def test_same_task_is_never_executed_twice(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    calls: list[str] = []

    async def runner(task: object) -> str:
        calls.append("run")
        await asyncio.sleep(0.01)
        return "done"

    manager = BackgroundTaskManager(store, runner, noop_notify, max_concurrent=2)
    task = store.create("1", "s", "objective", {"notify_chat_id": 1})
    await asyncio.gather(manager._execute(task), manager._execute(task))
    assert calls == ["run"]
    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "completed"
    assert stored.state["result"] == "done"


@pytest.mark.asyncio
async def test_cancel_releases_resources_and_keeps_state_consistent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    started = asyncio.Event()
    notifications: list[str] = []

    async def runner(task: object) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def notify(chat_id: int, text: str) -> None:
        notifications.append(text)

    manager = BackgroundTaskManager(store, runner, notify, max_concurrent=1)
    broker = TelegramApprovalBroker()
    manager.approval_broker = broker
    task = await manager.submit("1", "s", "long", notify_chat_id=1)
    worker = manager.active[task.task_id]
    await started.wait()

    assert await manager.cancel(task.task_id, "1")
    await asyncio.gather(worker, return_exceptions=True)
    await asyncio.sleep(0)

    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "cancelled"
    assert manager.active == {}
    assert manager.cancelled == set()
    assert broker.pending == {}
    assert not await manager.cancel(task.task_id, "1")


@pytest.mark.asyncio
async def test_cancel_rejects_other_owner_and_finished_tasks(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    async def runner(task: object) -> str:
        return "ok"

    manager = BackgroundTaskManager(store, runner, noop_notify, max_concurrent=1)
    task = await manager.submit("1", "s", "quick", notify_chat_id=1)
    await manager.active[task.task_id]
    assert not await manager.cancel(task.task_id, "1")
    assert not await manager.cancel(task.task_id, "2")
    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "completed"


@pytest.mark.asyncio
async def test_shutdown_interrupts_running_task_and_blocks_new_submissions(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    started = asyncio.Event()
    cancelled_tasks: list[str] = []

    class RecordingBroker(TelegramApprovalBroker):
        async def cancel_task(self, task_id: str) -> None:
            cancelled_tasks.append(task_id)
            await super().cancel_task(task_id)

    async def runner(task: object) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    manager = BackgroundTaskManager(store, runner, noop_notify, max_concurrent=1)
    manager.approval_broker = RecordingBroker()
    task = await manager.submit("1", "s", "long", notify_chat_id=1)
    await started.wait()

    await manager.shutdown()

    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "interrupted"
    assert stored.state["recovery"]["action"] == "manual_review"
    assert set(cancelled_tasks) == {task.task_id}
    assert manager.active == {}
    with pytest.raises(RuntimeError):
        await manager.submit("1", "s", "late", notify_chat_id=1)
    assert store.recoverable() == []


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_a_worker_ignores_cancellation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(task: object) -> str:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    manager = BackgroundTaskManager(
        store, runner, noop_notify, max_concurrent=1, shutdown_timeout=0.05
    )
    task = await manager.submit("1", "s", "stubborn", notify_chat_id=1)
    worker = manager.active[task.task_id]
    await started.wait()

    loop = asyncio.get_running_loop()
    began = loop.time()
    await manager.shutdown()
    assert loop.time() - began < 1.0

    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "interrupted"
    release.set()
    await asyncio.gather(worker, return_exceptions=True)


def test_shutdown_timeout_must_be_positive(tmp_path: Path) -> None:
    async def runner(task: object) -> str:
        return "ok"

    with pytest.raises(ValueError):
        BackgroundTaskManager(make_store(tmp_path), runner, noop_notify, shutdown_timeout=0)


@pytest.mark.asyncio
async def test_failed_task_cleans_pending_approvals(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    released: list[str] = []

    class RecordingBroker(TelegramApprovalBroker):
        async def cancel_task(self, task_id: str) -> None:
            released.append(task_id)
            await super().cancel_task(task_id)

    async def runner(task: object) -> str:
        raise RuntimeError("boom")

    manager = BackgroundTaskManager(store, runner, noop_notify, max_concurrent=1)
    manager.approval_broker = RecordingBroker()
    task = await manager.submit("1", "s", "fails", notify_chat_id=1)
    await manager.active[task.task_id]
    stored = store.get(task.task_id)
    assert stored is not None and stored.status == "failed"
    assert stored.error == "RuntimeError"
    assert set(released) == {task.task_id}
