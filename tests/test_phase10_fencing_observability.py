from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.notifications import LoggingNotificationSink
from eurus.automation.supervision import RuntimeSupervisor
from eurus.cli.autonomy import _SideEffectMarker
from eurus.events.bus import EventBus
from eurus.storage.database import Database
from eurus.storage.jobs import Job, JobStore


class ExecutorStub(JobExecutor):
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        return f"done {job.objective}"


def make_engine(tmp_path: Path, owner_id: str | None = "alice"):
    database = Database(tmp_path / "fence.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id=owner_id, poll_seconds=0.05)
    return engine, database


def make_job(job_id: str, worker_id: str | None) -> Job:
    return Job(
        job_id=job_id,
        owner_id="alice",
        objective="objective",
        status="running",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=1,
        max_attempts=2,
        available_at="",
        worker_id=worker_id,
        lease_expires_at=None,
        lease_version=1,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at="",
        updated_at="",
        source="manual",
    )


def test_marker_requires_worker_identity() -> None:
    marker = _SideEffectMarker()
    marker.bind(object())
    with pytest.raises(RuntimeError, match="no worker identity"):
        marker.factory(make_job("job-1", None))()


def test_marker_forwards_worker_identity_to_store(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "mutate work")
    claim = engine.jobs.claim("worker-x", lease_seconds=60)
    assert claim is not None
    marker = _SideEffectMarker()
    marker.bind(engine.jobs)
    marker.factory(claim)()
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.side_effect_started is True
    database.close()


def test_mark_side_effect_started_requires_worker_id(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.enqueue("alice", "objective")
    store.claim("worker-1", lease_seconds=60)
    with pytest.raises(ValueError):
        store.mark_side_effect_started("missing-job", "", 1)


def test_mark_side_effect_started_rejects_expired_lease(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    engine.submit("alice", "objective")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    late = datetime.now(UTC) + timedelta(seconds=120)
    assert (
        engine.jobs.mark_side_effect_started(
            claim.job_id, "worker-1", claim.lease_version, now=late
        )
        is False
    )
    stored = engine.jobs.get(claim.job_id)
    assert stored is not None
    assert stored.side_effect_started is False
    database.close()


def test_mark_side_effect_started_rejects_superseded_worker(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    engine.submit("alice", "objective")
    claim = engine.jobs.claim("worker-1", lease_seconds=60)
    assert claim is not None
    assert (
        engine.jobs.mark_side_effect_started(claim.job_id, "worker-2", claim.lease_version) is False
    )
    assert (
        engine.jobs.mark_side_effect_started(claim.job_id, "worker-1", claim.lease_version) is True
    )
    database.close()


def test_stale_worker_cannot_mark_commit_or_retry_after_lease_expiry(
    tmp_path: Path,
) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "objective")
    stale_claim = engine.jobs.claim("worker-old", lease_seconds=30)
    assert stale_claim is not None
    future = datetime.now(UTC) + timedelta(seconds=120)
    recovered = engine.jobs.recover_expired(now=future, owner_id="alice")
    assert recovered["requeued"] == 1
    new_claim = engine.jobs.claim("worker-new", lease_seconds=60, now=future)
    assert new_claim is not None and new_claim.lease_version > stale_claim.lease_version
    assert (
        engine.jobs.mark_side_effect_started(
            job.job_id, "worker-old", stale_claim.lease_version, now=future
        )
        is False
    )
    assert (
        engine.jobs.heartbeat(job.job_id, "worker-old", stale_claim.lease_version, lease_seconds=60)
        is False
    )
    assert (
        engine.jobs.finish(job.job_id, "worker-old", stale_claim.lease_version, status="completed")
        is False
    )
    assert (
        engine.jobs.schedule_retry(
            job.job_id,
            "worker-old",
            stale_claim.lease_version,
            delay_seconds=0.0,
        )
        is False
    )
    assert (
        engine.jobs.finish(
            job.job_id,
            "worker-new",
            new_claim.lease_version,
            status="completed",
            result="new worker output",
        )
        is True
    )
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "completed"
    assert stored.result == "new worker output"
    database.close()


def test_owner_scoped_health_hides_other_owner_metadata(tmp_path: Path) -> None:
    database = Database(tmp_path / "health.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    engine.submit("alice", "alice queued")
    bob_job = engine.submit("bob", "bob queued")
    engine.jobs.claim("worker-bob", lease_seconds=30, owner_id="bob")
    engine.jobs.finish(bob_job.job_id, "worker-bob", bob_job.lease_version + 1, status="failed")
    engine.submit("bob", "bob second")
    supervisor = RuntimeSupervisor(engine, owner_id="alice")
    health = supervisor.health()
    assert health["jobs_queued"] == 1
    assert health["jobs_failed"] == 0
    metrics = health["metrics"]
    assert metrics["dead_letters"] == 0
    concurrency = metrics["concurrency"]
    assert "bob" not in concurrency["running_by_owner"]
    admin_supervisor = RuntimeSupervisor(engine, owner_id=None)
    admin_health = admin_supervisor.health()
    assert admin_health["jobs_queued"] == 2
    assert admin_health["jobs_failed"] == 1
    database.close()


def test_health_event_stats_are_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "healthev.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    bus = EventBus(database, automation=engine)
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    supervisor = RuntimeSupervisor(engine, events=bus, owner_id="alice")
    health = supervisor.health()
    assert health["events"]["events"] == 1
    admin_health = RuntimeSupervisor(engine, events=bus, owner_id=None).health()
    assert admin_health["events"]["events"] == 2
    database.close()


class RecordingSink:
    def __init__(self) -> None:
        self.delivered: list[Any] = []

    async def deliver(self, notification: Any) -> None:
        self.delivered.append(notification)


class FailingSink:
    def __init__(self) -> None:
        self.calls = 0

    def deliver(self, notification: Any) -> None:
        self.calls += 1
        raise RuntimeError("provider outage")


async def test_notify_delivery_preserves_event_owner(tmp_path: Path) -> None:
    database = Database(tmp_path / "notify.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    sink = RecordingSink()
    bus = EventBus(database, automation=engine, notifications=sink)
    bus.subscribe(
        "notify-handler",
        event_types=["PING"],
        handler_kind="notify",
        handler_config={"message": "hello {{subject_id}}"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    bus.dispatch_tick(owner_id="alice")
    await asyncio.sleep(0.05)
    assert len(sink.delivered) == 1
    assert sink.delivered[0].owner_id == "alice"
    bus.dispatch_tick(owner_id="alice")
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "delivered"
    database.close()


async def test_notify_provider_outage_retries_then_dead_letters(tmp_path: Path) -> None:
    database = Database(tmp_path / "outage.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    sink = FailingSink()
    bus = EventBus(database, automation=engine, notifications=sink, max_delivery_attempts=2)
    bus.subscribe(
        "notify-handler",
        event_types=["PING"],
        handler_kind="notify",
        handler_config={"message": "hello"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    now = datetime.now(UTC)
    bus.dispatch_tick(now=now, owner_id="alice")
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    bus.dispatch_tick(now=now + timedelta(seconds=60), owner_id="alice")
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "dead"
    assert delivery.last_error is not None
    assert "provider outage" in delivery.last_error
    database.close()


async def test_notify_without_sink_is_never_acknowledged(tmp_path: Path) -> None:
    database = Database(tmp_path / "nosink.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    bus = EventBus(database, automation=engine, notifications=None)
    bus.subscribe(
        "notify-handler",
        event_types=["PING"],
        handler_kind="notify",
        handler_config={"message": "hello"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    bus.dispatch_tick(owner_id="alice")
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status in ("pending", "dead")
    assert delivery.status != "delivered"
    database.close()


async def test_notify_async_sink_without_event_loop_is_not_acknowledged(
    tmp_path: Path,
) -> None:
    import threading

    database = Database(tmp_path / "noloop.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    sink = RecordingSink()
    bus = EventBus(database, automation=engine, notifications=sink)
    subscription = bus.subscribe(
        "notify-handler",
        event_types=["PING"],
        handler_kind="notify",
        handler_config={"message": "hello"},
        owner_id="alice",
    )
    event = bus.publish("PING", subject_id="a-1", owner_id="alice")
    assert event is not None
    assert bus.store.fan_out(event) == 1
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    bus.store.mark_delivering(delivery.delivery_id)
    stored_event = bus.store.get_event(event.event_id)
    stored_subscription = bus.store.get_subscription(subscription.subscription_id)
    assert stored_event is not None and stored_subscription is not None
    result_box: dict[str, str] = {}

    def run_outside_loop() -> None:
        result_box["outcome"] = bus._notify(stored_event, stored_subscription, delivery.delivery_id)

    thread = threading.Thread(target=run_outside_loop)
    thread.start()
    thread.join()
    assert result_box["outcome"] == "no running event loop for asynchronous notification sink"
    refreshed = bus.store.get_delivery(delivery.delivery_id)
    assert refreshed is not None
    assert refreshed.status == "delivering"
    assert sink.delivered == []
    database.close()


def test_build_stack_wires_production_notification_sink(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    from eurus.cli.autonomy import build_stack
    from eurus.config import Config

    sink = LoggingNotificationSink()
    stack = build_stack(Config(), workers=1, poll_seconds=0.05, notification_sink=sink)
    try:
        assert stack.events.notifications is sink
        assert stack.automation.notifications is sink
    finally:
        stack.close()


def test_default_build_stack_sink_is_logging_sink(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    from eurus.cli.autonomy import build_stack
    from eurus.config import Config

    stack = build_stack(Config(), workers=1, poll_seconds=0.05)
    try:
        assert stack.events.notifications is not None
        assert stack.automation.notifications is stack.events.notifications
    finally:
        stack.close()


def test_cross_owner_dead_letter_resolution_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "letters.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    job = engine.submit("bob", "bob objective", max_attempts=1)
    engine.jobs.claim("worker-bob", lease_seconds=30)
    engine.jobs.finish(job.job_id, "worker-bob", job.lease_version + 1, status="failed")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    letter = engine.jobs.record_dead_letter(stored, reason="operator_failed", detail="boom")
    with pytest.raises(KeyError):
        engine.jobs.resolve_dead_letter(
            letter.dead_letter_id, resolution="discarded", owner_id="alice"
        )
    resolved, replayed = engine.jobs.resolve_dead_letter(
        letter.dead_letter_id, resolution="replayed", owner_id="bob", replay_job=True
    )
    assert resolved.resolution == "replayed"
    assert replayed is not None
    assert replayed.owner_id == "bob"
    assert replayed.status == "queued"
    database.close()
