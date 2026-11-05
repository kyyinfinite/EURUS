from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.supervision import RuntimeSupervisor
from eurus.storage.database import Database
from eurus.storage.jobs import Job


class ExecutorStub(JobExecutor):
    async def execute(self, job: Job, context: ExecutionContext) -> str:
        return f"done {job.objective}"


class FailingGoals:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.calls = 0

    def tick(self, *, owner_id: str | None = None, limit: int = 50) -> Any:
        self.calls += 1
        raise RuntimeError("goal store exploded")

    def recover(self, *, owner_id: str | None = None) -> int:
        return 0


class CountingWorkflows:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.ticks: list[str | None] = []

    def tick(self, *, owner_id: str | None = None, limit: int = 20) -> Any:
        self.ticks.append(owner_id)
        report: Any = type("Report", (), {"instances_advanced": 0, "jobs_submitted": 0})()
        return report

    def recover(self, *, owner_id: str | None = None) -> int:
        return 0


@pytest.fixture()
def runtime(tmp_path: Path) -> tuple[AutomationEngine, Database]:
    database = Database(tmp_path / "supervisor.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    return engine, database


def test_subsystem_failure_is_captured(
    runtime: tuple[AutomationEngine, Database],
) -> None:
    engine, database = runtime
    goals = FailingGoals(store=object())
    supervisor = RuntimeSupervisor(engine, goals=goals, owner_id="alice")
    report = supervisor.supervise()
    assert "goals" in report.subsystem_errors
    assert "goal store exploded" in report.subsystem_errors["goals"]
    health = supervisor.health()
    assert health["status"] == "degraded"
    assert health["subsystems"]["goals"]["healthy"] is False
    assert health["subsystems"]["goals"]["consecutive_failures"] == 1
    assert health["subsystems"]["goals"]["total_failures"] == 1
    assert health["subsystems"]["leases"]["healthy"] is True
    database.close()


def test_healthy_supervisor_reports_healthy(runtime: tuple[AutomationEngine, Database]) -> None:
    engine, database = runtime
    supervisor = RuntimeSupervisor(engine, owner_id="alice")
    supervisor.supervise()
    health = supervisor.health()
    assert health["status"] == "healthy"
    assert all(state["healthy"] for state in health["subsystems"].values())
    assert health["active_workers"] == 0
    database.close()


def test_core_subsystem_failure_is_unhealthy(runtime: tuple[AutomationEngine, Database]) -> None:
    engine, database = runtime

    class BrokenJobs:
        def recover_expired(
            self, *, now: Any = None, owner_id: str | None = None
        ) -> dict[str, int]:
            raise RuntimeError("db gone")

        def list_jobs(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("db gone")

        def list_dead_letters(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("db gone")

    supervisor = RuntimeSupervisor(engine, owner_id="alice")
    original_jobs = supervisor.automation.jobs
    supervisor.automation.jobs = BrokenJobs()
    supervisor.supervise()
    health = supervisor.health()
    assert health["status"] == "unhealthy"
    assert health["subsystems"]["leases"]["healthy"] is False
    supervisor.automation.jobs = original_jobs
    database.close()


def test_backoff_skips_failing_subsystem_then_recovers(
    runtime: tuple[AutomationEngine, Database],
) -> None:
    engine, database = runtime
    goals = FailingGoals(store=object())
    supervisor = RuntimeSupervisor(
        engine,
        goals=goals,
        owner_id="alice",
        backoff_base_seconds=60.0,
        backoff_max_seconds=60.0,
    )
    first = supervisor.supervise()
    assert "goals" in first.subsystem_errors
    skipped = supervisor.supervise()
    assert any("backoff" in note for note in skipped.notes)
    assert goals.calls == 1
    assert supervisor._subsystems["goals"].skipped_by_backoff == 1
    assert supervisor._backoff_until["goals"] > 0
    database.close()


def test_backoff_is_bounded(runtime: tuple[AutomationEngine, Database]) -> None:
    engine, database = runtime
    supervisor = RuntimeSupervisor(
        engine,
        owner_id="alice",
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
    )
    state = supervisor._subsystems["goals"]
    for _ in range(12):
        delay = state.record_failure(0.0, "still broken")
    assert delay <= 300.0
    assert delay == 300.0
    database.close()


def test_success_resets_failure_tracking(runtime: tuple[AutomationEngine, Database]) -> None:
    engine, database = runtime

    class ToggleGoals(FailingGoals):
        def __init__(self, store: Any) -> None:
            super().__init__(store)
            self.broken = True

        def tick(self, *, owner_id: str | None = None, limit: int = 50) -> Any:
            if self.broken:
                return super().tick(owner_id=owner_id, limit=limit)
            return type("Report", (), {"goals_advanced": 0, "jobs_submitted": 0})()

    toggle = ToggleGoals(store=object())
    supervisor = RuntimeSupervisor(engine, goals=toggle, owner_id="alice")
    supervisor.supervise()
    assert supervisor._subsystems["goals"].consecutive_failures == 1
    toggle.broken = False
    supervisor._backoff_until.clear()
    supervisor.supervise()
    assert supervisor._subsystems["goals"].healthy is True
    assert supervisor._subsystems["goals"].total_failures == 1
    database.close()


def test_worker_liveness_and_lease_info_exposed(
    runtime: tuple[AutomationEngine, Database],
) -> None:
    engine, database = runtime
    engine.submit("alice", "work")
    import asyncio

    async def one_pass() -> None:
        await engine.run_worker("worker-live")

    asyncio.run(one_pass())
    health = RuntimeSupervisor(engine, owner_id="alice").health()
    assert "worker_liveness" in health
    assert "active_workers" in health
    assert health["jobs_running"] + health["jobs_queued"] + health["jobs_failed"] >= 0
    database.close()


def test_supervise_is_repeat_safe_and_close_idempotent(
    runtime: tuple[AutomationEngine, Database],
) -> None:
    engine, database = runtime
    supervisor = RuntimeSupervisor(engine, owner_id="alice")
    supervisor.supervise()
    supervisor.supervise()
    supervisor.close()
    supervisor.close()
    report = supervisor.supervise()
    assert isinstance(report.jobs_submitted_by_goals, int)
    database.close()


def test_health_includes_queue_and_dead_letter_counts(
    runtime: tuple[AutomationEngine, Database],
) -> None:
    engine, database = runtime
    job = engine.submit("alice", "queued work")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    health = RuntimeSupervisor(engine, owner_id="alice").health()
    assert health["jobs_queued"] >= 1
    assert "dead_letters" in health
    assert "jobs_unknown_outcome" in health
    assert "last_reconciliation_age_seconds" in health
    assert "metrics" in health
    database.close()
