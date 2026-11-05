from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus.automation.engine import AutomationEngine
from eurus.events.bus import EventBus
from eurus.storage.database import Database


class SimpleExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Any, context: Any) -> str:
        self.executed.append(job.job_id)
        return f"handled {job.objective}"


class RecordingTriggers:
    def __init__(self) -> None:
        self.triggered: list[tuple[str, str | None]] = []

    def trigger_event_schedules(self, event: Any, *, owner_id: str | None = None) -> list[str]:
        self.triggered.append((event.event_type, owner_id))
        return [f"schedule-{len(self.triggered)}"]


def build(tmp_path: Path, *, owner_a: str = "alice", owner_b: str = "bob"):
    database = Database(tmp_path / "events.sqlite3")
    executor = SimpleExecutor()
    engine_a = AutomationEngine(database, executor, owner_id=owner_a, poll_seconds=0.05)
    engine_b = AutomationEngine(database, executor, owner_id=owner_b, poll_seconds=0.05)
    triggers = RecordingTriggers()
    bus = EventBus(database, automation=engine_a, schedule_triggers=triggers)
    return bus, engine_a, engine_b, triggers, database


def event_jobs(engine: AutomationEngine, owner_id: str) -> list[Any]:
    return [
        job
        for job in engine.jobs.list_jobs(owner_id=owner_id, limit=100)
        if (job.payload or {}).get("kind") == "event"
    ]


async def test_owner_scoped_dispatch_drains_only_own_outbox(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    bus.subscribe(
        "bob-handler",
        event_types=["PING"],
        handler_config={"objective": "Bob work"},
        owner_id="bob",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    report = bus.dispatch_tick(owner_id="alice")
    assert report.events_fanned_out == 1
    remaining = bus.store.pending_outbox(owner_id="bob")
    assert len(remaining) == 1
    assert remaining[0].owner_id == "bob"
    _ = engine_b
    database.close()


async def test_owner_subscription_ignores_other_owner_events(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-only",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    bob_event = bus.publish("PING", subject_id="b-1", owner_id="bob")
    assert bob_event is not None
    bus.dispatch_tick()
    deliveries = bus.store.deliveries_for_event(bob_event.event_id)
    assert deliveries == []
    alice_event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert alice_event is not None
    bus.dispatch_tick()
    deliveries = bus.store.deliveries_for_event(alice_event.event_id)
    assert len(deliveries) == 1
    _ = engine_a, engine_b
    database.close()


async def test_cross_owner_event_ack_is_refused(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    assert bus.store.fan_out(event) == 1
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    claimed = bus.store.mark_delivering(delivery.delivery_id, owner_id="bob")
    assert claimed is None
    claimed = bus.store.mark_delivering(delivery.delivery_id, owner_id="alice")
    assert claimed is not None
    assert bus.store.ack_delivery(delivery.delivery_id, owner_id="bob") is False
    stored = bus.store.get_delivery(delivery.delivery_id)
    assert stored is not None
    assert stored.status == "delivering"
    assert bus.store.ack_delivery(delivery.delivery_id, owner_id="alice") is True
    _ = engine_a, engine_b
    database.close()


async def test_cross_owner_dead_delivery_replay_is_refused(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    bus.dispatch_tick()
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    bus.store.mark_delivering(delivery.delivery_id)
    bus.store.fail_delivery(delivery.delivery_id, error="boom", max_attempts=1, owner_id="alice")
    stored = bus.store.get_delivery(delivery.delivery_id)
    assert stored is not None and stored.status == "dead"
    assert bus.store.retry_dead_delivery(delivery.delivery_id, owner_id="bob") is False
    revived = bus.store.get_delivery(delivery.delivery_id)
    assert revived is not None and revived.status == "dead"
    assert bus.store.retry_dead_delivery(delivery.delivery_id, owner_id="alice") is True
    _ = engine_a, engine_b
    database.close()


async def test_same_dedup_key_across_owners_does_not_collide(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    first = bus.publish("EXTERNAL", dedup_key="ext-1", owner_id="alice")
    second = bus.publish("EXTERNAL", dedup_key="ext-1", owner_id="bob")
    assert first is not None
    assert second is not None
    assert first.event_id != second.event_id
    duplicate = bus.publish("EXTERNAL", dedup_key="ext-1", owner_id="alice")
    assert duplicate is None
    _ = engine_a, engine_b
    database.close()


async def test_event_triggered_job_preserves_event_owner(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "handler",
        event_types=["PING"],
        handler_config={"objective": "Do the work"},
        owner_id="alice",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.dispatch_tick(owner_id="bob")
    assert engine_b.jobs.list_jobs(owner_id="bob", limit=50) == []
    bus.dispatch_tick(owner_id="alice")
    jobs = event_jobs(engine_a, "alice")
    assert len(jobs) == 1
    assert jobs[0].owner_id == "alice"
    assert jobs[0].payload["event_owner"] == "alice"
    _ = engine_b
    database.close()


async def test_unowned_event_falls_back_to_subscription_owner(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "legacy-handler",
        event_types=["PING"],
        handler_config={"objective": "Legacy work", "owner_id": "carol"},
    )
    bus.publish("PING", subject_id="legacy-1")
    bus.dispatch_tick()
    jobs = event_jobs(engine_a, "carol")
    assert len(jobs) == 1
    assert jobs[0].owner_id == "carol"
    _ = engine_b
    database.close()


async def test_restart_preserves_owner_scope_of_pending_events(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    database = Database(path)
    executor = SimpleExecutor()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    bus = EventBus(database, automation=engine)
    bus.subscribe(
        "handler",
        event_types=["PING"],
        handler_config={"objective": "Work for {{event_type}}"},
        owner_id="alice",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    database.close()

    reopened = Database(path)
    executor2 = SimpleExecutor()
    engine2 = AutomationEngine(reopened, executor2, owner_id="alice", poll_seconds=0.05)
    engine_b2 = AutomationEngine(reopened, executor2, owner_id="bob", poll_seconds=0.05)
    bus2 = EventBus(reopened, automation=engine2)
    bus2.subscribe(
        "bob-handler",
        event_types=["PING"],
        handler_config={"objective": "Bob work"},
        owner_id="bob",
    )
    bus2.dispatch_tick(owner_id="alice")
    assert len(event_jobs(engine2, "alice")) == 1
    assert event_jobs(engine_b2, "bob") == []
    bus2.dispatch_tick(owner_id="bob")
    assert len(event_jobs(engine_b2, "bob")) == 1
    reopened.close()


async def test_interleaved_owner_dispatch_creates_one_job_per_event(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "handler",
        event_types=["PING"],
        handler_config={"objective": "Work"},
        owner_id="alice",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    for _ in range(3):
        bus.dispatch_tick(owner_id="alice")
        bus.dispatch_tick(owner_id="bob")
    jobs = event_jobs(engine_a, "alice")
    assert len(jobs) == 1
    assert engine_b.jobs.list_jobs(owner_id="bob", limit=50) == []
    _ = engine_b
    database.close()


async def test_admin_dispatch_processes_all_owners(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    bus.subscribe(
        "bob-handler",
        event_types=["PING"],
        handler_config={"objective": "Bob work"},
        owner_id="bob",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    report = bus.dispatch_tick()
    assert report.deliveries_created == 2
    jobs_a = event_jobs(engine_a, "alice")
    jobs_b = event_jobs(engine_b, "bob")
    assert len(jobs_a) == 1
    assert len(jobs_b) == 1
    assert jobs_a[0].owner_id == "alice"
    assert jobs_b[0].owner_id == "bob"
    _ = engine_b
    database.close()


async def test_scoped_event_stats_hide_other_owners(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    bus.dispatch_tick(owner_id="alice")
    alice_stats = bus.store.stats(owner_id="alice")
    assert alice_stats["events"] == 1
    assert alice_stats["pending_outbox"] == 0
    assert alice_stats["pending_deliveries"] == 0
    bob_stats = bus.store.stats(owner_id="bob")
    assert bob_stats["events"] == 1
    assert bob_stats["pending_outbox"] == 1
    global_stats = bus.store.stats()
    assert global_stats["events"] == 2
    _ = engine_a, engine_b
    database.close()


async def test_scoped_dead_and_due_deliveries_are_partitioned(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice work"},
        owner_id="alice",
    )
    bus.subscribe(
        "bob-handler",
        event_types=["PING"],
        handler_config={"objective": "Bob work"},
        owner_id="bob",
    )
    alice_event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    bob_event = bus.publish("PING", subject_id="b-1", owner_id="bob")
    assert alice_event is not None and bob_event is not None
    assert bus.store.fan_out(alice_event) == 1
    assert bus.store.fan_out(bob_event) == 1
    due_bob = bus.store.due_deliveries(owner_id="bob")
    assert len(due_bob) == 1
    assert due_bob[0].event_id == bob_event.event_id
    for delivery in bus.store.deliveries_for_event(alice_event.event_id):
        bus.store.mark_delivering(delivery.delivery_id)
        bus.store.fail_delivery(
            delivery.delivery_id, error="boom", max_attempts=1, owner_id="alice"
        )
    dead_bob = bus.store.dead_deliveries(owner_id="bob")
    assert dead_bob == []
    dead_alice = bus.store.dead_deliveries(owner_id="alice")
    assert len(dead_alice) == 1
    _ = engine_a, engine_b
    database.close()


async def test_trigger_schedules_preserve_event_owner(tmp_path: Path) -> None:
    bus, engine_a, engine_b, triggers, database = build(tmp_path)
    bus.subscribe(
        "schedule-trigger",
        event_types=["PING"],
        handler_kind="trigger_schedules",
        handler_config={},
        owner_id="alice",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.dispatch_tick(owner_id="alice")
    assert triggers.triggered == [("PING", "alice")]
    _ = engine_a, engine_b
    database.close()


async def test_scoped_listing_and_subscription_management(tmp_path: Path) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)
    sub_a = bus.subscribe("a-sub", event_types=["PING"], owner_id="alice")
    sub_b = bus.subscribe("b-sub", event_types=["PING"], owner_id="bob")
    alice_subs = bus.list_subscriptions(owner_id="alice")
    assert [sub.name for sub in alice_subs] == ["a-sub"]
    assert bus.unsubscribe(sub_b.subscription_id, owner_id="alice") is False
    assert bus.unsubscribe(sub_b.subscription_id, owner_id="bob") is True
    assert bus.set_subscription_enabled(sub_a.subscription_id, False, owner_id="bob") is False
    assert bus.set_subscription_enabled(sub_a.subscription_id, False, owner_id="alice") is True
    _ = engine_a, engine_b
    database.close()


async def test_subscription_staleness_after_retry_backoff_is_owner_scoped(
    tmp_path: Path,
) -> None:
    bus, engine_a, engine_b, _, database = build(tmp_path)

    class FailingBus(EventBus):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)

        def _deliver(self, delivery: Any, report: Any, owner_id: str | None = None) -> str:
            return "handler unavailable"

    failing = FailingBus(database, max_delivery_attempts=3)
    failing.subscribe("doomed", event_types=["PING"], owner_id="alice")
    failing.publish("PING", subject_id="a-1", owner_id="alice")
    now = datetime.now(UTC)
    failing.dispatch_tick(now=now, owner_id="alice")
    failing.dispatch_tick(now=now + timedelta(seconds=60), owner_id="alice")
    failing.dispatch_tick(now=now + timedelta(seconds=120), owner_id="bob")
    failing.dispatch_tick(now=now + timedelta(seconds=120), owner_id="alice")
    dead = failing.store.dead_deliveries(owner_id="alice")
    assert len(dead) == 1
    assert dead[0].attempts == 3
    _ = engine_a, engine_b
    await asyncio.sleep(0)
    database.close()
