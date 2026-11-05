from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from eurus.automation.concurrency import ConcurrencyGovernor
from eurus.automation.executor import (
    AgentJobExecutor,
    ExecutionContext,
    JobExecutor,
)
from eurus.automation.notifications import (
    LoggingNotificationSink,
    Notification,
    NotificationSink,
)
from eurus.automation.policies import PolicyEnforcer, SpecialistProfileResolver
from eurus.automation.reconciliation import (
    ReconciliationReport,
    ReconciliationService,
    ScheduleRunSync,
)
from eurus.automation.retry import RetryManager
from eurus.automation.worker import AutomationWorkerPool
from eurus.storage.database import Database, DatabasePath
from eurus.storage.jobs import Job, JobStore

log = logging.getLogger(__name__)

JobFinishedCallback = Callable[[Job, str, str, str | None], Awaitable[None] | None]
EventPublisher = Callable[[str, str, str, Mapping[str, Any]], None]


class SchedulerDispatch(Protocol):
    def dispatch_due(
        self, *, now: datetime | None = None, limit: int = 20, owner_id: str | None = None
    ) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    dispatched: int
    reconciliation: ReconciliationReport


def _database_specialist_profile_resolver(database: Database) -> SpecialistProfileResolver:

    from eurus.multiagent.registry import SpecialistRegistry

    registry = SpecialistRegistry(database)

    def resolve(role: str) -> Mapping[str, Any] | None:
        specialist = registry.get(role)
        if specialist is None:
            return None
        return {
            "allowed_tools": list(specialist.allowed_tools),
            "tool_policy_version": getattr(specialist, "tool_policy_version", None),
        }

    return resolve


class AutomationEngine:
    def __init__(
        self,
        database: Database | DatabasePath,
        executor: JobExecutor,
        *,
        retry_manager: RetryManager | None = None,
        policy_enforcer: PolicyEnforcer | None = None,
        specialist_profile_resolver: SpecialistProfileResolver | None = None,
        governor: ConcurrencyGovernor | None = None,
        notifications: NotificationSink | None = None,
        event_publisher: EventPublisher | None = None,
        scheduler: SchedulerDispatch | None = None,
        schedule_runs: ScheduleRunSync | None = None,
        occurrence_finalizer: Callable[[str, str, str], bool] | None = None,
        on_job_finished: Sequence[JobFinishedCallback] = (),
        workers: int = 2,
        poll_seconds: float = 1.0,
        lease_seconds: int = 60,
        dispatch_limit: int = 100,
        owner_id: str | None = None,
        recover_expired_on_poll: bool = True,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.jobs = JobStore(self.database)
        self.executor = executor
        self.retry_manager = retry_manager or RetryManager()
        if policy_enforcer is not None:
            self.policy_enforcer = policy_enforcer
        else:
            self.policy_enforcer = PolicyEnforcer(
                specialist_profile_resolver=(
                    specialist_profile_resolver
                    if specialist_profile_resolver is not None
                    else _database_specialist_profile_resolver(self.database)
                )
            )
        self.governor = governor or ConcurrencyGovernor(self.jobs, owner_id=owner_id)
        self.notifications = notifications or LoggingNotificationSink()
        self.event_publisher = event_publisher
        self.scheduler = scheduler
        self.schedule_runs = schedule_runs
        self.on_job_finished = list(on_job_finished)
        self.workers = workers
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.dispatch_limit = dispatch_limit
        self.owner_id = owner_id
        self.reconciliation = ReconciliationService(
            self.jobs,
            schedules=schedule_runs,
            occurrence_finalizer=occurrence_finalizer,
            unknown_outcome_observer=self._on_recovered_terminal,
        )
        self.jobs_submitted = 0
        self.jobs_executed = 0
        self._pool = AutomationWorkerPool(
            self.jobs,
            self._execute_job,
            workers=workers,
            poll_seconds=poll_seconds,
            lease_seconds=lease_seconds,
            on_finish=self._handle_job_finished,
            retry_decider=self._decide_retry,
            claim_function=self.governor.claim,
            defer_side_effect_marking=self._uses_deferred_boundary,
            recovery_owner_id=owner_id,
            recover_expired_on_poll=recover_expired_on_poll,
        )

    @property
    def worker_pool(self) -> AutomationWorkerPool:
        return self._pool

    @property
    def _uses_deferred_boundary(self) -> bool:
        return isinstance(self.executor, AgentJobExecutor) and (
            self.executor.side_effect_marker_factory is not None
        )

    def submit(
        self,
        owner_id: str,
        objective: str,
        *,
        payload: dict[str, Any] | None = None,
        source: str = "manual",
        priority: int = 5,
        max_attempts: int | None = None,
        available_at: datetime | None = None,
        correlation_id: str = "",
        job_id: str | None = None,
    ) -> Job:
        merged_payload: dict[str, Any] = dict(payload or {})
        attempts = max_attempts
        if attempts is None:
            attempts = self.retry_manager.policy_for(
                _synthetic_job_for_policy(source, owner_id, objective)
            ).max_attempts
        job = self.jobs.enqueue(
            owner_id,
            objective,
            payload=merged_payload,
            max_attempts=attempts,
            available_at=available_at,
            job_id=job_id,
            priority=priority,
            source=source,
            correlation_id=correlation_id,
        )
        self.jobs_submitted += 1
        if self.event_publisher is not None:
            self.event_publisher(
                "TASK_CREATED",
                "job",
                job.job_id,
                {"objective": _short(job.objective), "source": job.source},
            )
        return job

    def retry_failed(self, job_id: str, *, owner_id: str | None = None) -> Job | None:
        return self.jobs.replay(job_id, owner_id=owner_id)

    async def run_worker(
        self, worker_id: str | None = None, *, now: datetime | None = None
    ) -> bool:
        return await self._pool.run_once(worker_id, now=now)

    async def run_workers(self) -> None:
        await self._pool.run()

    async def stop_workers(self) -> None:
        await self._pool.stop()

    def maintenance_tick(self, *, now: datetime | None = None) -> MaintenanceReport:
        dispatched = 0
        if self.scheduler is not None:
            dispatched = len(
                self.scheduler.dispatch_due(
                    now=now, limit=self.dispatch_limit, owner_id=self.owner_id
                )
            )
        report = self.reconciliation.reconcile(now=now, owner_id=self.owner_id)
        return MaintenanceReport(dispatched=dispatched, reconciliation=report)

    def dispatch_tick(self, *, now: datetime | None = None) -> int:
        return self.maintenance_tick(now=now).dispatched

    def metrics(self) -> dict[str, Any]:
        return {
            "jobs_submitted": self.jobs_submitted,
            "jobs_executed": self.jobs_executed,
            "jobs_retried": self._pool.jobs_retried,
            "jobs_finished": self._pool.jobs_finished,
            "jobs_failed": self._pool.jobs_failed,
            "commits_fenced": self._pool.commits_fenced,
            "dead_letters": len(self.jobs.list_dead_letters(owner_id=self.owner_id, limit=500)),
            "concurrency": asdict(self.governor.snapshot(owner_id=self.owner_id)),
        }

    async def _execute_job(self, job: Job) -> str:
        self.jobs_executed += 1
        policy = self.policy_enforcer.verify_startable(job)
        context = ExecutionContext.from_job(
            job,
            budget=policy.budget_spec(),
            allowed_tools=policy.allowed_tools,
            blocked_tools=policy.blocked_tools,
            policy_scope=policy.policy_scope,
            deadline_at=policy.deadline_at,
        )
        if self.event_publisher is not None:
            self.event_publisher(
                "TASK_STARTED",
                "job",
                job.job_id,
                {"attempt": job.attempts, "source": job.source},
            )
        return await self.executor.execute(job, context)

    async def _decide_retry(self, job: Job, error: BaseException) -> tuple[bool, float, str]:
        decision = self.retry_manager.decide(job, error)
        if decision.retry and self.event_publisher is not None:
            self.event_publisher(
                "TASK_RETRY_SCHEDULED",
                "job",
                job.job_id,
                {"reason": decision.reason, "attempt": job.attempts},
            )
        return decision.retry, decision.delay_seconds, decision.reason

    async def _handle_job_finished(
        self, job: Job, status: str, result: str, error: str | None
    ) -> None:
        if status == "failed":
            self._record_failure_dead_letter(job, error or result)
        if status == "unknown_outcome":
            self.jobs.record_dead_letter(
                job, reason="unknown_outcome", detail=error or "outcome uncertain"
            )
        if self.schedule_runs is not None and job.schedule_id:
            self.schedule_runs.finish_run(job.job_id, status=status, result=error or result)
        await self._notify(job, status, result, error)
        if self.event_publisher is not None:
            event_type = _terminal_event(status)
            if event_type is not None:
                self.event_publisher(
                    event_type,
                    "job",
                    job.job_id,
                    {"error": error or "", "source": job.source},
                )
        for callback in self.on_job_finished:
            try:
                outcome = callback(job, status, result, error)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:
                log.exception(
                    "job-finished callback failed for job %s (status=%s)", job.job_id, status
                )

    def _on_unknown_outcome_observed(self, job: Job) -> None:
        if self.event_publisher is not None:
            try:
                self.event_publisher(
                    "TASK_UNKNOWN_OUTCOME",
                    "job",
                    job.job_id,
                    {"error": job.error or "", "source": job.source},
                )
            except Exception:
                log.exception("unknown-outcome publication failed for job %s", job.job_id)

    def _on_recovered_terminal(self, job: Job) -> None:
        event_type = _terminal_event(job.status)
        if event_type is None or self.event_publisher is None:
            return
        try:
            self.event_publisher(
                event_type,
                "job",
                job.job_id,
                {"error": job.error or "", "source": job.source},
            )
        except Exception:
            log.exception("recovered-terminal publication failed for job %s", job.job_id)

    def _record_failure_dead_letter(self, job: Job, detail: str) -> None:
        current = self.jobs.get(job.job_id)
        if current is not None and current.status in ("queued", "running"):
            return
        decision = self.retry_manager.decide(job, detail or "failure")
        if decision.reason in {"retry_exhausted", "retry_duration_exceeded"}:
            reason: str = decision.reason
        elif current is not None and current.error == "lease_expired_attempt_limit":
            reason = "lease_exhausted"
        else:
            reason = "operator_failed"
        self.jobs.record_dead_letter(job, reason=reason, detail=detail[:2000])

    async def _notify(self, job: Job, status: str, result: str, error: str | None) -> None:
        policy = self.policy_enforcer.policy_for(job)
        if not policy.notifications:
            return
        kind = {
            "completed": "job_completed",
            "failed": "job_failed",
            "cancelled": "job_cancelled",
            "unknown_outcome": "job_unknown_outcome",
        }.get(status)
        if kind is None:
            return
        notification = Notification(
            kind=kind,
            title=f"{kind}:{job.job_id[:8]} {status}",
            body=(error or result)[:500],
            owner_id=job.owner_id,
            correlation_id=job.correlation_id,
            job_id=job.job_id,
            result=result[:8000],
            deliver=str(job.payload.get("deliver", "") or ""),
            origin_gateway=str(job.payload.get("origin_gateway", "") or ""),
            origin_conversation=str(job.payload.get("origin_conversation", "") or ""),
            silent=bool(job.payload.get("silent", False)),
            attempt=job.attempts,
        )
        try:
            await self.notifications.deliver(notification)
        except Exception:
            log.exception("notification delivery failed for job %s", job.job_id)


def _terminal_event(status: str) -> str | None:
    return {
        "completed": "TASK_COMPLETED",
        "failed": "TASK_FAILED",
        "cancelled": "TASK_CANCELLED",
        "unknown_outcome": "TASK_UNKNOWN_OUTCOME",
    }.get(status)


def _synthetic_job_for_policy(source: str, owner_id: str, objective: str) -> Job:
    return Job(
        job_id="synthetic",
        owner_id=owner_id,
        objective=objective,
        status="queued",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=0,
        max_attempts=1,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=0,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at="",
        updated_at="",
        source=source,
    )


def _short(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
