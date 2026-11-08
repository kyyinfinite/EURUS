from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from eurus.automation.engine import AutomationEngine
from eurus.goals.states import TERMINAL_GOAL_STATUSES, GoalStatus
from eurus.runtime.liveness import DEFAULT_STALE_RUN_SECONDS, reap_stale_runs
from eurus.storage.database import Database
from eurus.storage.jobs import Job
from eurus.storage.runs import RunStore
from eurus.trajectory import TrajectoryStore
from eurus.workflows.states import WorkflowStatus

log = logging.getLogger(__name__)

DEFAULT_STUCK_SECONDS = 2 * 3600
DEFAULT_STALE_ASSIGNMENT_SECONDS = 1800
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0

SUBSYSTEM_NAMES = (
    "leases",
    "assignments",
    "missions",
    "workflows",
    "goals",
    "events",
    "scheduler",
    "learning",
    "runs",
    "trajectory",
)


class GoalTicker(Protocol):
    store: Any

    def tick(self, *, owner_id: str | None = None, limit: int = 50) -> Any: ...


class WorkflowTicker(Protocol):
    store: Any

    def tick(self, *, owner_id: str | None = None, limit: int = 20) -> Any: ...

    def recover(self, *, owner_id: str | None = None) -> int: ...


class EventDispatcher(Protocol):
    store: Any

    def dispatch_tick(self, *, now: datetime | None = None, owner_id: str | None = None) -> Any: ...


class LearningTicker(Protocol):
    def tick(
        self, *, now: datetime | None = None, limit: int = 20, owner_id: str | None = None
    ) -> Any: ...

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]: ...


class MissionTicker(Protocol):
    def tick(self, *, owner_id: str | None = None, now: datetime | None = None) -> Any: ...

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]: ...


@dataclass(slots=True)
class SubsystemState:
    name: str
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    total_failures: int = 0
    skipped_by_backoff: int = 0

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0

    def record_success(self, moment: float) -> None:
        self.last_success_at = moment
        self.consecutive_failures = 0
        self.last_error = ""

    def record_failure(self, moment: float, error: str) -> float:
        self.last_failure_at = moment
        self.last_error = error[:500]
        self.consecutive_failures += 1
        self.total_failures += 1
        return self.backoff_seconds()

    def backoff_seconds(
        self,
        *,
        base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    ) -> float:
        attempt = max(1, self.consecutive_failures)
        raw = base_seconds * (2 ** (attempt - 1))
        return float(min(raw, max_seconds))


@dataclass(slots=True)
class SupervisionReport:
    leases_recovered: dict[str, int] = field(default_factory=dict)
    stuck_jobs_cancelled: list[str] = field(default_factory=list)
    assignments_recovered: int = 0
    workflows_recovered: int = 0
    goals_ticked: int = 0
    workflows_ticked: int = 0
    events_dispatched: int = 0
    jobs_submitted_by_goals: int = 0
    learning_processed: int = 0
    missions_ticked: int = 0
    mission_tasks_dispatched: int = 0
    missions_finalized: int = 0
    scheduler_dispatched: int = 0
    runs_reaped: list[str] = field(default_factory=list)
    subsystem_errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class RuntimeSupervisor:
    def __init__(
        self,
        automation: AutomationEngine,
        *,
        goals: GoalTicker | None = None,
        workflows: WorkflowTicker | None = None,
        events: EventDispatcher | None = None,
        assignments: Any = None,
        missions: MissionTicker | None = None,
        learning: LearningTicker | None = None,
        runs: RunStore | None = None,
        trajectory: TrajectoryStore | None = None,
        stuck_after_seconds: float = DEFAULT_STUCK_SECONDS,
        stale_assignment_seconds: float = DEFAULT_STALE_ASSIGNMENT_SECONDS,
        stale_run_seconds: float = DEFAULT_STALE_RUN_SECONDS,
        owner_id: str | None = None,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if stuck_after_seconds < 60:
            raise ValueError("stuck_after_seconds must be at least 60")
        if stale_assignment_seconds < 60:
            raise ValueError("stale_assignment_seconds must be at least 60")
        if stale_run_seconds < 60:
            raise ValueError("stale_run_seconds must be at least 60")
        if backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be positive")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must not be below backoff_base_seconds")
        self.automation = automation
        self.goals = goals
        self.workflows = workflows
        self.events = events
        self.assignments = assignments
        self.missions = missions
        self.learning = learning
        self.runs = runs
        self.trajectory = trajectory
        self.stuck_after_seconds = stuck_after_seconds
        self.stale_assignment_seconds = stale_assignment_seconds
        self.stale_run_seconds = stale_run_seconds
        self.owner_id = owner_id
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self._clock = clock
        self._subsystems: dict[str, SubsystemState] = {
            name: SubsystemState(name=name) for name in SUBSYSTEM_NAMES
        }
        self.enabled_subsystems: set[str] | None = None
        self._backoff_until: dict[str, float] = {}
        self._last_reconciliation_at: float | None = None
        self._closed = False

    def supervise(self, *, now: datetime | None = None) -> SupervisionReport:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        report = SupervisionReport()
        self._run_subsystem("leases", report, self._recover_leases, current)
        self._run_subsystem("assignments", report, self._recover_stale_assignments, current)
        self._run_subsystem("missions", report, self._tick_missions, current)
        self._run_subsystem("workflows", report, self._tick_workflows, current)
        self._run_subsystem("goals", report, self._tick_goals, current)
        self._run_subsystem("events", report, self._dispatch_events, current)
        self._run_subsystem("scheduler", report, self._dispatch_scheduler, current)
        self._run_subsystem("learning", report, self._tick_learning, current)
        self._run_subsystem("runs", report, self._reap_stale_runs, current)
        self._run_subsystem("trajectory", report, self._prune_trajectory, current)
        self._last_reconciliation_at = self._clock()
        return report

    async def supervise_async(self, *, now: datetime | None = None) -> SupervisionReport:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        report = SupervisionReport()
        await asyncio.to_thread(
            self._run_subsystem, "leases", report, self._recover_leases, current
        )
        await asyncio.to_thread(
            self._run_subsystem, "assignments", report, self._recover_stale_assignments, current
        )
        await asyncio.to_thread(
            self._run_subsystem, "missions", report, self._tick_missions, current
        )
        await asyncio.to_thread(
            self._run_subsystem, "workflows", report, self._tick_workflows, current
        )
        await asyncio.to_thread(self._run_subsystem, "goals", report, self._tick_goals, current)
        self._run_subsystem("events", report, self._dispatch_events, current)
        await asyncio.to_thread(
            self._run_subsystem, "scheduler", report, self._dispatch_scheduler, current
        )
        await asyncio.to_thread(
            self._run_subsystem, "learning", report, self._tick_learning, current
        )
        await asyncio.to_thread(self._run_subsystem, "runs", report, self._reap_stale_runs, current)
        await asyncio.to_thread(
            self._run_subsystem, "trajectory", report, self._prune_trajectory, current
        )
        self._last_reconciliation_at = self._clock()
        return report

    def _run_subsystem(
        self,
        name: str,
        report: SupervisionReport,
        operation: Callable[[SupervisionReport, datetime], None],
        current: datetime,
    ) -> None:
        if self._closed:
            return
        if self.enabled_subsystems is not None and name not in self.enabled_subsystems:
            return
        state = self._subsystems[name]
        until = self._backoff_until.get(name)
        moment = self._clock()
        if until is not None and moment < until:
            state.skipped_by_backoff += 1
            report.notes.append(f"{name} skipped: backoff until {until:.1f}")
            return
        try:
            operation(report, current)
        except Exception as exc:
            delay = state.record_failure(
                moment,
                f"{type(exc).__name__}: {exc}",
            )
            delay = min(delay, self.backoff_max_seconds)
            self._backoff_until[name] = moment + delay
            report.subsystem_errors[name] = state.last_error
            log.exception(
                "subsystem %s failed (consecutive=%s, backoff=%.1fs)",
                name,
                state.consecutive_failures,
                delay,
            )
        else:
            state.record_success(moment)
            self._backoff_until.pop(name, None)

    def _recover_leases(self, report: SupervisionReport, current: datetime) -> None:
        report.leases_recovered = self.automation.jobs.recover_expired(
            now=current, owner_id=self.owner_id
        )
        for job in self._stuck_jobs(current):
            if self.automation.jobs.request_cancel(job.job_id, owner_id=job.owner_id):
                report.stuck_jobs_cancelled.append(job.job_id)
                log.warning("supervisor requested cancel for stuck job %s", job.job_id)

    def _tick_workflows(self, report: SupervisionReport, current: datetime) -> None:
        if self.workflows is None:
            return
        report.workflows_recovered = self.workflows.recover(owner_id=self.owner_id)
        workflow_report = self.workflows.tick(owner_id=self.owner_id)
        report.workflows_ticked = workflow_report.instances_advanced
        report.jobs_submitted_by_goals += workflow_report.jobs_submitted

    def _tick_goals(self, report: SupervisionReport, current: datetime) -> None:
        if self.goals is None:
            return
        goal_report = self.goals.tick(owner_id=self.owner_id)
        report.goals_ticked = goal_report.goals_advanced
        report.jobs_submitted_by_goals += goal_report.jobs_submitted

    def _dispatch_events(self, report: SupervisionReport, current: datetime) -> None:
        if self.events is None:
            return
        event_report = self.events.dispatch_tick(now=current, owner_id=self.owner_id)
        report.events_dispatched = event_report.deliveries_processed

    def _dispatch_scheduler(self, report: SupervisionReport, current: datetime) -> None:
        outcome = self.automation.maintenance_tick(now=current)
        report.scheduler_dispatched = outcome.dispatched

    def _tick_learning(self, report: SupervisionReport, current: datetime) -> None:
        if self.learning is None:
            return
        learning_report = self.learning.tick(now=current, owner_id=self.owner_id)
        report.learning_processed = learning_report.processed

    def _tick_missions(self, report: SupervisionReport, current: datetime) -> None:
        if self.missions is None:
            return
        progression = self.missions.tick(owner_id=self.owner_id, now=current)
        report.missions_ticked = int(getattr(progression, "missions_checked", 0) or 0)
        report.mission_tasks_dispatched = len(getattr(progression, "dispatched", ()) or ())
        report.missions_finalized = len(getattr(progression, "finalized", ()) or ())

    def _reap_stale_runs(self, report: SupervisionReport, current: datetime) -> None:
        if self.runs is None:
            return
        report.runs_reaped = reap_stale_runs(self.runs, older_than_seconds=self.stale_run_seconds)

    def _prune_trajectory(self, report: SupervisionReport, current: datetime) -> None:
        if self.trajectory is None:
            return
        pruned = self.trajectory.prune(now=current)
        if pruned:
            report.notes.append(f"trajectory pruned {pruned} expired events")

    def _recover_stale_assignments(self, report: SupervisionReport, current: datetime) -> None:
        if self.assignments is None:
            return
        recovered = 0
        stale = self.assignments.store.stale_running(
            older_than_seconds=self.stale_assignment_seconds,
            owner_id=self.owner_id,
        )
        for assignment in stale:
            if self.owner_id is not None and assignment.owner_id != self.owner_id:
                continue
            if assignment.job_id is None:
                continue
            job = self.automation.jobs.get(assignment.job_id)
            if job is None or job.owner_id != assignment.owner_id:
                continue
            if job.status == "completed":
                self.assignments.store.update(
                    assignment.assignment_id,
                    status="succeeded",
                    result=job.result,
                    owner_id=assignment.owner_id,
                )
                recovered += 1
            elif job.status in ("failed", "cancelled", "unknown_outcome"):
                self.assignments.store.update(
                    assignment.assignment_id,
                    status="failed",
                    error=job.error or job.status,
                    owner_id=assignment.owner_id,
                )
                recovered += 1
            elif job.status in ("queued", "running"):
                self.assignments.store.heartbeat(
                    assignment.assignment_id,
                    f"job {job.status}",
                    owner_id=assignment.owner_id,
                )
        report.assignments_recovered = recovered

    def health(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        jobs = self.automation.jobs
        failing = [state for state in self._subsystems.values() if not state.healthy]
        core_failing = any(
            state.name in ("leases", "scheduler") and not state.healthy for state in failing
        )
        if core_failing:
            status = "unhealthy"
        elif failing:
            status = "degraded"
        else:
            status = "healthy"
        try:
            metrics = self.automation.metrics()
        except Exception:
            log.exception("automation metrics collection failed")
            metrics = {}
        try:
            running = jobs.list_jobs(owner_id=self.owner_id, statuses=("running",), limit=500)
        except Exception:
            running = []
        try:
            queued = jobs.list_jobs(owner_id=self.owner_id, statuses=("queued",), limit=500)
        except Exception:
            queued = []
        try:
            unknown = jobs.list_jobs(
                owner_id=self.owner_id, statuses=("unknown_outcome",), limit=500
            )
        except Exception:
            unknown = []
        try:
            failed = jobs.list_jobs(owner_id=self.owner_id, statuses=("failed",), limit=500)
        except Exception:
            failed = []
        try:
            dead_letters = jobs.list_dead_letters(owner_id=self.owner_id, limit=500)
        except Exception:
            dead_letters = []
        try:
            stuck = self._stuck_jobs(current)
        except Exception:
            stuck = []
        oldest_queued = min(
            (
                datetime.fromisoformat(job.created_at) if job.created_at else current
                for job in queued
            ),
            default=None,
        )
        oldest_age_seconds = (
            (current - oldest_queued.replace(tzinfo=oldest_queued.tzinfo or UTC)).total_seconds()
            if oldest_queued is not None
            else 0.0
        )
        snapshot: dict[str, Any] = {
            "status": status,
            "owner_id": self.owner_id,
            "jobs_running": len(running),
            "jobs_queued": len(queued),
            "jobs_failed": len(failed),
            "jobs_unknown_outcome": len(unknown),
            "dead_letters": len(dead_letters),
            "oldest_queued_age_seconds": round(oldest_age_seconds, 1),
            "stuck_jobs": len(stuck),
            "active_workers": self.automation.worker_pool.active_workers(),
            "worker_liveness": self.automation.worker_pool.liveness(),
            "last_reconciliation_age_seconds": (
                round(self._clock() - self._last_reconciliation_at, 3)
                if self._last_reconciliation_at is not None
                else None
            ),
            "subsystems": {
                state.name: {
                    "healthy": state.healthy,
                    "in_backoff": state.name in self._backoff_until,
                    "consecutive_failures": state.consecutive_failures,
                    "total_failures": state.total_failures,
                    "skipped_by_backoff": state.skipped_by_backoff,
                    "last_error": state.last_error,
                    "last_success_age_seconds": (
                        round(self._clock() - state.last_success_at, 3)
                        if state.last_success_at is not None
                        else None
                    ),
                    "last_failure_age_seconds": (
                        round(self._clock() - state.last_failure_at, 3)
                        if state.last_failure_at is not None
                        else None
                    ),
                }
                for state in self._subsystems.values()
            },
            "metrics": metrics,
        }
        if self.events is not None:
            snapshot["events"] = self.events.store.stats(owner_id=self.owner_id)
        if self.goals is not None:
            try:
                snapshot["active_goals"] = len(
                    self.goals.store.list_goals(
                        owner_id=self.owner_id,
                        statuses=tuple(
                            status for status in GoalStatus if status not in TERMINAL_GOAL_STATUSES
                        ),
                        limit=500,
                    )
                )
            except Exception:
                log.exception("goal health collection failed")
                snapshot["active_goals"] = None
        if self.workflows is not None:
            try:
                snapshot["active_workflows"] = len(
                    self.workflows.store.list_instances(
                        owner_id=self.owner_id,
                        statuses=(
                            WorkflowStatus.PENDING,
                            WorkflowStatus.RUNNING,
                            WorkflowStatus.WAITING_APPROVAL,
                            WorkflowStatus.WAITING_SCHEDULE,
                        ),
                        limit=500,
                    )
                )
            except Exception:
                log.exception("workflow health collection failed")
                snapshot["active_workflows"] = None
        if self.assignments is not None:
            try:
                snapshot["stale_assignments"] = len(
                    self.assignments.store.stale_running(
                        older_than_seconds=self.stale_assignment_seconds,
                        owner_id=self.owner_id,
                    )
                )
            except Exception:
                log.exception("assignment health collection failed")
                snapshot["stale_assignments"] = None
        if self.missions is not None:
            try:
                snapshot["missions"] = self.missions.stats(owner_id=self.owner_id)
            except Exception:
                log.exception("mission health collection failed")
                snapshot["missions"] = None
        if self.learning is not None:
            try:
                snapshot["learning"] = self.learning.stats(owner_id=self.owner_id)
            except Exception:
                log.exception("learning health collection failed")
                snapshot["learning"] = None
        return snapshot

    def close(self) -> None:
        self._closed = True
        self._backoff_until.clear()

    def _stuck_jobs(self, current: datetime) -> list[Job]:
        cutoff = current.timestamp() - self.stuck_after_seconds
        stuck: list[Job] = []
        for job in self.automation.jobs.list_jobs(
            owner_id=self.owner_id, statuses=("running",), limit=500
        ):
            progress_marker = job.updated_at or job.created_at
            if not progress_marker:
                continue
            try:
                parsed = datetime.fromisoformat(str(progress_marker))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed.timestamp() < cutoff:
                stuck.append(job)
        return stuck


def build_supervisor(
    database: Database,
    automation: AutomationEngine,
    *,
    goals: GoalTicker | None = None,
    workflows: WorkflowTicker | None = None,
    events: EventDispatcher | None = None,
    assignments: Any = None,
    learning: LearningTicker | None = None,
    owner_id: str | None = None,
) -> RuntimeSupervisor:
    return RuntimeSupervisor(
        automation,
        goals=goals,
        workflows=workflows,
        events=events,
        assignments=assignments,
        learning=learning,
        owner_id=owner_id,
    )
