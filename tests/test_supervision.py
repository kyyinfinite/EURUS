from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.automation.supervision import RuntimeSupervisor
from eurus.events.bus import EventBus
from eurus.goals.engine import GoalEngine
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.storage.database import Database
from eurus.workflows.engine import WorkflowEngine


class QuietExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Any, context: Any) -> str:
        self.executed.append(job.job_id)
        return "supervised output"


def build(tmp_path: Path):
    from eurus.scheduler import ScheduleStore

    database = Database(tmp_path / "supervision.sqlite3")
    executor = QuietExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=3))
    schedule_store = ScheduleStore(tmp_path / "supervision.sqlite3")
    automation = AutomationEngine(
        database,
        executor,
        retry_manager=retry,
        poll_seconds=0.05,
        scheduler=schedule_store,
        schedule_runs=schedule_store,
    )
    events = EventBus(database, automation=automation)

    def publish(event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
        events.publish(
            event_type, subject_type=subject_type, subject_id=subject_id, payload=payload
        )

    goals = GoalEngine(database, automation, event_publisher=publish)
    workflows = WorkflowEngine(database, automation, event_publisher=publish)
    orchestrator = AgentOrchestrator(database, automation, event_publisher=publish)
    supervisor = RuntimeSupervisor(
        automation,
        goals=goals,
        workflows=workflows,
        events=events,
        assignments=orchestrator,
        stuck_after_seconds=60,
        stale_assignment_seconds=60,
    )
    return supervisor, automation, executor, goals, workflows, orchestrator, events, database


def test_supervision_rejects_invalid_windows(tmp_path: Path) -> None:
    _, automation, *_ = build(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        RuntimeSupervisor(automation, stuck_after_seconds=1)
    with pytest.raises(ValueError):
        RuntimeSupervisor(automation, stale_assignment_seconds=5)


def test_supervision_recovers_expired_leases(tmp_path: Path) -> None:
    supervisor, automation, executor, *_ = build(tmp_path)
    automation.submit("owner", "Leased job")
    claimed = automation.jobs.claim("worker-a", lease_seconds=5, now=datetime.now(UTC))
    assert claimed is not None
    future = datetime.now(UTC) + timedelta(seconds=120)
    report = supervisor.supervise(now=future)
    assert report.leases_recovered.get("requeued", 0) >= 1
    assert supervisor.health()["jobs_queued"] >= 1


def test_supervision_cancels_stuck_jobs(tmp_path: Path) -> None:
    supervisor, automation, executor, *_ = build(tmp_path)
    job = automation.submit("owner", "Stuck job")
    claimed = automation.jobs.claim("worker-a", lease_seconds=3600, now=datetime.now(UTC))
    assert claimed is not None
    assert (
        automation.jobs.heartbeat(
            claimed.job_id, "worker-a", claimed.lease_version, lease_seconds=7200
        )
        is True
    )
    stored = automation.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "running"
    future = datetime.now(UTC) + timedelta(seconds=3700)
    report = supervisor.supervise(now=future)
    assert job.job_id in report.stuck_jobs_cancelled
    final = automation.jobs.get(job.job_id)
    assert final is not None
    assert final.cancel_requested or final.status == "cancelled"


def test_supervision_health_snapshot(tmp_path: Path) -> None:
    supervisor, automation, *_ = build(tmp_path)
    automation.submit("owner", "Queued job")
    health = supervisor.health()
    assert health["jobs_queued"] == 1
    assert health["jobs_running"] == 0
    assert "metrics" in health
    assert "events" in health
    assert health["oldest_queued_age_seconds"] >= 0.0


def test_supervision_ticks_all_subsystems(tmp_path: Path) -> None:
    supervisor, automation, executor, goals, workflows, orchestrator, events, _ = build(tmp_path)
    goal = goals.create_goal("owner", "Supervised goal")
    events.subscribe(
        "supervision-watcher",
        event_types=["GOAL_CREATED"],
        handler_config={"objective": "React to goal"},
    )
    report = supervisor.supervise()
    assert report.goals_ticked >= 1
    assert report.jobs_submitted_by_goals >= 1
    assert report.events_dispatched >= 1
    jobs = automation.jobs.list_jobs(limit=20)
    kinds = {(job.payload or {}).get("kind") for job in jobs}
    assert "goal_plan" in kinds
    assert "event" in kinds
    assert goals.store.get(goal.goal_id).status.value in ("planning", "active", "analyzing")


def test_supervision_reconciles_stale_assignments(tmp_path: Path) -> None:
    supervisor, automation, executor, goals, workflows, orchestrator, events, _ = build(tmp_path)
    orchestrator.registry.register("clerk", description="Clerk")
    assignment = orchestrator.dispatch("File the report", role="clerk", owner_id="owner")
    claimed = automation.jobs.claim("worker-a", lease_seconds=3600)
    assert claimed is not None
    automation.jobs.finish(
        claimed.job_id,
        "worker-a",
        claimed.lease_version,
        status="completed",
        result="filed",
    )
    with automation.jobs.database.connection() as connection:
        backdated = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        connection.execute(
            "UPDATE eurus_agent_assignments SET updated_at=? WHERE assignment_id=?",
            (backdated, assignment.assignment_id),
        )
        connection.execute(
            "UPDATE eurus_agent_heartbeats SET last_beat=? WHERE assignment_id=?",
            (backdated, assignment.assignment_id),
        )
    supervisor.supervise()
    final = orchestrator.store.get(assignment.assignment_id)
    assert final is not None
    assert final.status == "succeeded"


def test_supervision_dispatches_due_schedules(tmp_path: Path) -> None:
    supervisor, automation, executor, *_ = build(tmp_path)
    from eurus.scheduler import ScheduleStore

    schedule_store = ScheduleStore(tmp_path / "supervision.sqlite3")
    backdated_now = datetime.now(UTC) - timedelta(minutes=10)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    schedule_store.create(
        "owner",
        "Overdue maintenance",
        kind="once",
        run_at=past,
        misfire_policy="run_once",
        now=backdated_now,
    )
    supervisor.supervise()
    jobs = automation.jobs.list_jobs(limit=20)
    assert any("Overdue maintenance" in job.objective for job in jobs)
