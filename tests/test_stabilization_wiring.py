from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import AgentJobExecutor, ExecutionContext
from eurus.cli.autonomy import AutonomyStack, build_stack
from eurus.config import Config
from eurus.events.bus import EventBus
from eurus.storage.database import Database
from eurus.storage.jobs import Job


class ScriptedExecutor(AgentJobExecutor):
    def __init__(
        self,
        config: Config,
        *,
        database: Database | None = None,
        trajectory_store: Any = None,
        side_effect_marker_factory: Any = None,
        behavior: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            config,
            database=database,
            trajectory_store=trajectory_store,
            side_effect_marker_factory=side_effect_marker_factory,
        )
        self.behavior = behavior or {}
        self.executed: list[str] = []
        self.marked: list[str] = []

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        behavior = self.behavior.get(job.payload.get("kind", job.objective), "ok")
        if behavior == "fail":
            raise TimeoutError(f"{job.objective} failed")
        if behavior == "mutate":
            marker = (
                self.side_effect_marker_factory(job)
                if self.side_effect_marker_factory is not None
                else None
            )
            if marker is not None:
                marker()
                self.marked.append(job.job_id)
            return f"mutated {job.objective}"
        return f"done {job.objective}"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def make_stack(home_path: Path, *, behavior: dict[str, str] | None = None) -> AutonomyStack:
    import eurus.automation.executor as executor_module

    config = Config()
    scripted = ScriptedExecutor(config, behavior=behavior)
    original = executor_module.AgentJobExecutor

    def factory(*args: Any, **kwargs: Any) -> ScriptedExecutor:
        scripted.database = kwargs.get("database")
        scripted.trajectory_store = kwargs.get("trajectory_store")
        scripted.side_effect_marker_factory = kwargs.get("side_effect_marker_factory")
        return scripted

    executor_module.AgentJobExecutor = factory
    try:
        stack = build_stack(config, workers=1, poll_seconds=0.05, owner_id="alice")
    finally:
        executor_module.AgentJobExecutor = original
    assert stack.automation.executor is scripted
    return stack


def test_daemon_binds_automation_engine_to_event_bus(home: Path) -> None:
    stack = make_stack(home)
    try:
        assert isinstance(stack.events, EventBus)
        assert stack.events.automation is stack.automation
        assert isinstance(stack.automation, AutomationEngine)
    finally:
        stack.close()


def test_daemon_uses_deferred_side_effect_boundary(home: Path) -> None:
    stack = make_stack(home)
    try:
        assert stack.automation._uses_deferred_boundary is True
        assert stack.automation.executor.side_effect_marker_factory is not None
    finally:
        stack.close()


def test_event_subscription_creates_durable_job_through_build_stack(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.events.subscribe(
            "react",
            event_types=("THING_HAPPENED",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice", "objective": "react to {{event_type}}"},
        )
        event = stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        assert event is not None
        report = stack.events.dispatch_tick()
        assert report.deliveries_delivered == 1
        assert report.jobs_submitted == 1
        jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        assert len(jobs) == 1
        assert jobs[0].payload["event_id"] == event.event_id
        assert jobs[0].source == "event"
        assert jobs[0].status == "queued"
    finally:
        stack.close()


def test_duplicate_event_delivery_does_not_duplicate_jobs(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.events.subscribe(
            "react",
            event_types=("THING_HAPPENED",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice"},
        )
        event = stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        assert event is not None
        first = stack.events.dispatch_tick()
        assert first.jobs_submitted == 1
        second = stack.events.dispatch_tick()
        assert second.jobs_submitted == 0
        jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        assert len(jobs) == 1
    finally:
        stack.close()


def test_task_lifecycle_events_do_not_retrigger_subscriptions(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.events.subscribe(
            "react",
            event_types=("*",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice"},
        )
        stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        first = stack.events.dispatch_tick()
        assert first.jobs_submitted == 1
        second = stack.events.dispatch_tick()
        assert second.jobs_submitted == 0
        assert second.jobs_suppressed >= 1
        asyncio.run(stack.automation.run_worker("worker-1"))
        third = stack.events.dispatch_tick()
        assert third.jobs_submitted == 0
        jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        assert len(jobs) == 1
    finally:
        stack.close()


def test_event_job_execution_completes_through_daemon_composition(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.events.subscribe(
            "react",
            event_types=("*",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice"},
        )
        stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        stack.events.dispatch_tick()
        worked = asyncio.run(stack.automation.run_worker("worker-1"))
        assert worked is True
        jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        assert jobs[0].status == "completed"
    finally:
        stack.close()


def test_automation_submission_failure_is_retried_then_dead_lettered(home: Path) -> None:
    stack = make_stack(home, behavior={"event": "fail"})
    try:
        stack.events.subscribe(
            "react",
            event_types=("THING_HAPPENED",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice", "max_attempts": 1},
        )
        stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        stack.events.dispatch_tick()
        asyncio.run(stack.automation.run_worker("worker-1"))
        report = stack.automation.maintenance_tick()
        assert report.reconciliation.dead_letters_recorded >= 0
        letters = stack.automation.jobs.list_dead_letters(owner_id="alice")
        assert len(letters) == 1
        assert letters[0].reason in ("retry_exhausted", "operator_failed", "lease_exhausted")
    finally:
        stack.close()


def test_exhausted_event_delivery_reaches_dead_letter_state(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.events.automation = None
        stack.events.subscribe(
            "react",
            event_types=("THING_HAPPENED",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice"},
        )
        stack.events.publish("THING_HAPPENED", subject_type="thing", subject_id="t1")
        moment = datetime.now(UTC)
        total_dead = 0
        for _ in range(6):
            report = stack.events.dispatch_tick(now=moment)
            total_dead += report.deliveries_dead
            moment += timedelta(seconds=600)
        assert total_dead >= 1
        stats = stack.events.store.stats()
        assert stats["dead_deliveries"] >= 1
    finally:
        stack.close()


def test_daemon_shutdown_closes_database_and_supervisor(home: Path) -> None:
    stack = make_stack(home)
    database = stack.database
    supervisor = stack.supervisor
    with database.connection() as connection:
        connection.execute("SELECT 1")
    stack.close()
    assert supervisor._closed is True
    assert database._pool == []
    with database.connection() as connection:
        connection.execute("SELECT 1")
    database.close()


def test_occurrence_finalizer_is_wired(home: Path) -> None:
    stack = make_stack(home)
    try:
        assert stack.automation.reconciliation.occurrence_finalizer is not None
    finally:
        stack.close()


def test_learning_coordinator_is_wired(home: Path) -> None:
    stack = make_stack(home)
    try:
        assert stack.learning is not None
        owners = [getattr(cb, "__self__", None) for cb in stack.automation.on_job_finished]
        assert stack.learning in owners
    finally:
        stack.close()


def test_marker_refuses_use_before_wiring(home: Path) -> None:
    from eurus.cli.autonomy import _SideEffectMarker

    marker = _SideEffectMarker()
    job = Job(
        job_id="j1",
        owner_id="alice",
        objective="o",
        status="running",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=1,
        max_attempts=1,
        available_at="",
        worker_id="w",
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
    with pytest.raises(RuntimeError, match="before runtime wiring"):
        marker.factory(job)


def test_side_effect_marker_marks_within_lease(home: Path) -> None:
    stack = make_stack(home, behavior={"event": "mutate"})
    try:
        job = stack.automation.submit(
            "alice", "mutate the thing", payload={"kind": "event"}, source="event"
        )
        claim = stack.automation.jobs.claim("worker-x", lease_seconds=60)
        assert claim is not None and claim.job_id == job.job_id
        assert claim.side_effect_started is False
        marker = stack.automation.executor.side_effect_marker_factory(claim)
        assert marker is not None
        marker()
        stored = stack.automation.jobs.get(job.job_id)
        assert stored is not None
        assert stored.side_effect_started is True
    finally:
        stack.close()


def test_marker_fails_when_lease_lost(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.automation.submit("alice", "touch nothing", payload={"kind": "event"})
        claim = stack.automation.jobs.claim("worker-x", lease_seconds=60)
        assert claim is not None
        stale_version = claim.lease_version
        future = datetime.now(UTC) + timedelta(seconds=90)
        recovered = stack.automation.jobs.recover_expired(now=future)
        assert recovered["requeued"] == 1
        re_claim = stack.automation.jobs.claim("worker-y", lease_seconds=60, now=future)
        assert re_claim is not None and re_claim.lease_version > stale_version
        marker = stack.automation.executor.side_effect_marker_factory(claim)
        assert marker is not None
        with pytest.raises(RuntimeError, match="lease lost"):
            marker()
    finally:
        stack.close()


def test_database_schema_migrated_on_build_stack(home: Path) -> None:
    stack = make_stack(home)
    try:
        with stack.database.connection() as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM eurus_schema_migrations"
            ).fetchone()[0]
        assert int(version) >= 8
    finally:
        stack.close()


def test_unscoped_admin_health_is_explicit(home: Path) -> None:
    stack = make_stack(home)
    try:
        stack.automation.submit("bob", "bob work")
        stack.automation.submit("alice", "alice work")
        health = stack.supervisor.health()
        assert health["owner_id"] == "alice"
        assert health["status"] in ("healthy", "degraded", "unhealthy")
        assert "active_workers" in health
        assert "subsystems" in health
    finally:
        stack.close()
