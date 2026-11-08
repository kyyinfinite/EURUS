from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.concurrency import ConcurrencyGovernor
from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext
from eurus.automation.notifications import CollectingNotificationSink, Notification
from eurus.automation.policies import ExecutionPolicy, PolicyEnforcer, PolicyViolation
from eurus.automation.reconciliation import ReconciliationService
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.storage.database import Database
from eurus.storage.jobs import Job, JobStore


class FakeExecutor:
    def __init__(self, behavior: dict[str, str] | None = None) -> None:
        self.behavior = behavior or {}
        self.executed: list[Job] = []
        self.contexts: list[ExecutionContext] = []
        self.fail_first: dict[str, int] = {}
        self.side_effects: list[str] = []

    def fail_first_attempts(self, job_id: str, attempts: int) -> None:
        self.fail_first[job_id] = attempts

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job)
        self.contexts.append(context)
        if job.job_id in self.fail_first and job.attempts <= self.fail_first[job.job_id]:
            raise TimeoutError("simulated provider timeout")
        if job.job_id in self.behavior:
            outcome = self.behavior[job.job_id]
            if outcome == "fail":
                raise ValueError("permanent configuration error")
            if outcome == "timeout":
                raise TimeoutError("simulated provider timeout")
            return outcome
        return f"done:{job.objective}"


def build_engine(
    tmp_path: Path,
    executor: FakeExecutor | None = None,
    *,
    retry: RetryManager | None = None,
    governor: ConcurrencyGovernor | None = None,
    notifications: CollectingNotificationSink | None = None,
    publisher=None,
    on_job_finished=(),
    policy: ExecutionPolicy | None = None,
) -> tuple[AutomationEngine, Database]:
    database = Database(tmp_path / "automation.sqlite3")
    engine = AutomationEngine(
        database,
        executor or FakeExecutor(),
        retry_manager=retry,
        governor=governor,
        notifications=notifications,
        event_publisher=publisher,
        on_job_finished=on_job_finished,
        policy_enforcer=PolicyEnforcer(policy=policy),
        poll_seconds=0.05,
    )
    return engine, database


async def test_submit_and_execute_roundtrip(tmp_path: Path) -> None:
    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor)
    job = engine.submit("owner", "summarize the report")
    assert job.status == "queued"
    assert job.source == "manual"
    worked = await engine.run_worker()
    assert worked is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "completed"
    assert stored.result == "done:summarize the report"
    assert executor.contexts[0].owner_id == "owner"
    assert executor.contexts[0].execution_id == job.job_id


async def test_execution_context_carries_job_fields(tmp_path: Path) -> None:
    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    engine.submit(
        "alice",
        "prepare the deck",
        payload={
            "goal_id": "g1",
            "workflow_instance_id": "wf1",
            "allowed_tools": ["read_file", "list_files"],
            "max_runtime_seconds": 120,
            "max_steps": 7,
            "deadline_at": deadline.isoformat(),
            "success_criteria": "deck exists",
        },
        source="goal",
        correlation_id="corr-1",
    )
    await engine.run_worker()
    context = executor.contexts[0]
    assert context.goal_id == "g1"
    assert context.workflow_instance_id == "wf1"
    assert context.allowed_tools == frozenset({"read_file", "list_files"})
    assert context.budget.max_runtime_seconds == 120
    assert context.budget.max_iterations == 7
    assert context.deadline_at is not None
    assert context.correlation_id == "corr-1"
    assert context.policy_scope == "goal"


async def test_budget_and_allowed_tools_are_enforced(tmp_path: Path) -> None:
    executor = FakeExecutor()
    policy = ExecutionPolicy(
        allowed_tools=frozenset({"read_file"}),
        blocked_tools=frozenset({"shell"}),
        max_steps=3,
        max_runtime_seconds=42,
        policy_scope="automation",
    )
    engine, _ = build_engine(tmp_path, executor, policy=policy)
    engine.submit("owner", "inspect")
    await engine.run_worker()
    context = executor.contexts[0]
    assert context.budget.max_iterations == 3
    assert context.budget.max_runtime_seconds == 42
    assert policy.allows_tool("read_file")
    assert not policy.allows_tool("shell")
    assert not policy.allows_tool("web_search")


async def test_policy_rejects_expired_deadline(tmp_path: Path) -> None:
    executor = FakeExecutor()
    stale = ExecutionPolicy(deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    engine, _ = build_engine(tmp_path, executor, policy=stale)
    job = engine.submit("owner", "late work")
    with pytest.raises(PolicyViolation):
        engine.policy_enforcer.verify_startable(job)
    worked = await engine.run_worker()
    assert worked is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error == "PolicyViolation"


async def test_retry_with_backoff_on_transient_failure(tmp_path: Path) -> None:
    executor = FakeExecutor()
    retry = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=3,
            backoff_base_seconds=0.05,
            backoff_max_seconds=0.05,
            jitter_fraction=0.0,
        )
    )
    engine, _ = build_engine(tmp_path, executor, retry=retry)
    job = engine.submit("owner", "flaky provider call", max_attempts=3)
    executor.fail_first_attempts(job.job_id, 2)
    first = await engine.run_worker()
    assert first is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.attempts == 1
    assert stored.available_at > stored.updated_at or stored.available_at == stored.updated_at
    await asyncio.sleep(0.06)
    await engine.run_worker()
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.attempts == 2
    await asyncio.sleep(0.06)
    await engine.run_worker()
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "completed"
    assert stored.result == "done:flaky provider call"
    assert len(executor.executed) == 3


async def test_permanent_failure_is_not_retried(tmp_path: Path) -> None:
    executor = FakeExecutor(behavior={"job-1": "fail"})
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=3, backoff_base_seconds=0.05))
    engine, _ = build_engine(tmp_path, executor, retry=retry)
    job = engine.submit("owner", "broken", max_attempts=3, job_id="job-1")
    await engine.run_worker()
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "failed"
    assert len(executor.executed) == 1


async def test_retry_exhaustion_creates_dead_letter(tmp_path: Path) -> None:
    executor = FakeExecutor(behavior={"job-2": "timeout"})
    retry = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=2, backoff_base_seconds=0.05, backoff_max_seconds=0.05, jitter_fraction=0.0
        )
    )
    engine, _ = build_engine(tmp_path, executor, retry=retry)
    engine.submit("owner", "always fails", max_attempts=2, job_id="job-2")
    await engine.run_worker()
    await asyncio.sleep(0.06)
    await engine.run_worker()
    stored = engine.jobs.get("job-2")
    assert stored is not None
    assert stored.status == "failed"
    letters = engine.jobs.list_dead_letters()
    assert len(letters) == 1
    assert letters[0].reason == "retry_exhausted"
    assert letters[0].job_id == "job-2"


async def test_manual_replay_restarts_failed_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor)
    engine.submit("owner", "retry me", job_id="job-3")
    executor.behavior["job-3"] = "fail"
    await engine.run_worker()
    assert engine.jobs.get("job-3").status == "failed"
    executor.behavior["job-3"] = "recovered output"
    replayed = engine.retry_failed("job-3")
    assert replayed is not None
    assert replayed.status == "queued"
    assert replayed.attempts == 0
    await engine.run_worker()
    stored = engine.jobs.get("job-3")
    assert stored.status == "completed"
    assert stored.result == "recovered output"


async def test_dead_letter_resolution_workflow(tmp_path: Path) -> None:
    executor = FakeExecutor(behavior={"job-4": "fail"})
    engine, _ = build_engine(tmp_path, executor)
    engine.submit("owner", "dead", job_id="job-4")
    await engine.run_worker()
    letter = engine.jobs.list_dead_letters()[0]
    executor.behavior["job-4"] = "fixed"
    resolved, replayed = engine.jobs.resolve_dead_letter(
        letter.dead_letter_id, resolution="replayed", replay_job=True
    )
    assert resolved.resolution == "replayed"
    assert replayed is not None
    await engine.run_worker()
    assert engine.jobs.get("job-4").status == "completed"
    assert engine.jobs.list_dead_letters() == []


async def test_worker_crash_before_side_effect_requeues(tmp_path: Path) -> None:
    database = Database(tmp_path / "crash.sqlite3")
    jobs = JobStore(database)
    job = jobs.enqueue("owner", "interrupted work", max_attempts=3)
    jobs.claim("worker-a", lease_seconds=5)
    now = datetime.now(UTC) + timedelta(seconds=10)
    report = jobs.recover_expired(now=now)
    assert report["requeued"] == 1
    recovered = jobs.get(job.job_id)
    assert recovered.status == "queued"


async def test_unknown_outcome_after_side_effect_boundary(tmp_path: Path) -> None:
    database = Database(tmp_path / "unknown.sqlite3")
    jobs = JobStore(database)
    job = jobs.enqueue("owner", "mutating work")
    claimed = jobs.claim("worker-a", lease_seconds=5)
    assert claimed is not None
    assert jobs.mark_side_effect_started(job.job_id, "worker-a", claimed.lease_version)
    now = datetime.now(UTC) + timedelta(seconds=10)
    report = jobs.recover_expired(now=now)
    assert report["unknown_outcome"] == 1
    stored = jobs.get(job.job_id)
    assert stored.status == "unknown_outcome"
    reconciliation = ReconciliationService(jobs)
    result = reconciliation.reconcile()
    assert result.unknown_outcomes_flagged == 1
    letters = jobs.list_dead_letters()
    assert len(letters) == 1
    assert letters[0].reason == "unknown_outcome"
    second = reconciliation.reconcile()
    assert second.unknown_outcomes_flagged == 0


async def test_expired_deadline_policy_blocks_execution(tmp_path: Path) -> None:
    executor = FakeExecutor()
    stale = ExecutionPolicy(deadline_at=datetime.now(UTC) - timedelta(seconds=5))
    engine, _ = build_engine(tmp_path, executor, policy=stale)
    engine.submit("owner", "too late", job_id="late-1")
    await engine.run_worker()
    stored = engine.jobs.get("late-1")
    assert stored.status == "failed"


async def test_cancellation_during_execution(tmp_path: Path) -> None:
    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor)
    job = engine.submit("owner", "cancel while running", job_id="cancel-1")

    started = asyncio.Event()

    class SlowExecutor(FakeExecutor):
        async def execute(self, job: Job, context: ExecutionContext) -> str:
            started.set()
            await asyncio.sleep(30)
            return "never"

    engine.executor = SlowExecutor()
    task = asyncio.create_task(engine.run_worker())
    await asyncio.wait_for(started.wait(), timeout=2)
    engine.jobs.request_cancel(job.job_id, owner_id="owner")
    result = await asyncio.wait_for(task, timeout=5)
    assert result is True
    stored = engine.jobs.get(job.job_id)
    assert stored.status == "cancelled"


async def test_concurrent_automation_respects_limits(tmp_path: Path) -> None:
    database = Database(tmp_path / "limits.sqlite3")
    jobs = JobStore(database)
    for index in range(6):
        jobs.enqueue(f"owner-{index % 3}", f"job {index}")
    governor = ConcurrencyGovernor(jobs, global_limit=2, per_owner_limit=1)
    first = governor.claim("w1", lease_seconds=60)
    assert first is not None
    second = governor.claim("w2", lease_seconds=60)
    assert second is not None
    assert second.owner_id != first.owner_id
    third = governor.claim("w3", lease_seconds=60)
    assert third is None
    snapshot = governor.snapshot()
    assert snapshot.running_global == 2
    assert sum(snapshot.running_by_owner.values()) == 2


async def test_priority_ordering_in_claim(tmp_path: Path) -> None:
    database = Database(tmp_path / "priority.sqlite3")
    jobs = JobStore(database)
    jobs.enqueue("owner", "low", priority=9, job_id="p-low")
    jobs.enqueue("owner", "high", priority=1, job_id="p-high")
    jobs.enqueue("owner", "mid", priority=5, job_id="p-mid")
    claimed = jobs.claim("worker", lease_seconds=60)
    assert claimed is not None
    assert claimed.job_id == "p-high"


async def test_notifications_on_completion_and_failure(tmp_path: Path) -> None:
    sink = CollectingNotificationSink()
    executor = FakeExecutor(behavior={"n-1": "ok text"})
    engine, _ = build_engine(tmp_path, executor, notifications=sink)
    engine.submit("owner", "notify me", job_id="n-1")
    engine.submit("owner", "fail me", job_id="n-2")
    executor.behavior["n-2"] = "fail"
    await engine.run_worker()
    await engine.run_worker()
    kinds = sorted(notification.kind for notification in sink.delivered)
    assert kinds == ["job_completed", "job_failed"]


async def test_event_publisher_receives_lifecycle(tmp_path: Path) -> None:
    events: list[tuple[str, str, str]] = []

    def publisher(event_type: str, subject_type: str, subject_id: str, payload: Any) -> None:
        events.append((event_type, subject_type, subject_id))

    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor, publisher=publisher)
    job = engine.submit("owner", "observed")
    await engine.run_worker()
    types = [item[0] for item in events]
    assert types == ["TASK_CREATED", "TASK_STARTED", "TASK_COMPLETED"]
    assert events[0][2] == job.job_id


async def test_on_job_finished_callback_invoked(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def callback(job: Job, status: str, result: str, error: str | None) -> None:
        seen.append((job.job_id, status))

    executor = FakeExecutor()
    engine, _ = build_engine(tmp_path, executor, on_job_finished=[callback])
    engine.submit("owner", "watched", job_id="w-1")
    await engine.run_worker()
    assert seen == [("w-1", "completed")]


async def test_engine_restart_recovers_running_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "restart.sqlite3")
    jobs = JobStore(database)
    jobs.enqueue("owner", "survives restart", job_id="r-1", max_attempts=3)
    claimed = jobs.claim("worker-old", lease_seconds=5)
    assert claimed is not None
    del jobs
    database.close()
    reopened = Database(tmp_path / "restart.sqlite3")
    executor = FakeExecutor()
    engine = AutomationEngine(reopened, executor, poll_seconds=0.05)
    future = datetime.now(UTC) + timedelta(seconds=30)
    engine.dispatch_tick(now=future)
    stored = engine.jobs.get("r-1")
    assert stored.status == "queued"
    assert stored.worker_id is None
    await engine.run_worker(now=future)
    stored = engine.jobs.get("r-1")
    assert stored.status == "completed"


async def test_retry_duration_limit(tmp_path: Path) -> None:
    executor = FakeExecutor(behavior={"d-1": "timeout"})
    retry = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=10,
            backoff_base_seconds=0.05,
            max_retry_duration_seconds=0.001,
        )
    )
    engine, _ = build_engine(tmp_path, executor, retry=retry)
    engine.submit("owner", "duration limited", job_id="d-1")
    await asyncio.sleep(0.01)
    await engine.run_worker()
    stored = engine.jobs.get("d-1")
    assert stored.status == "failed"
    letters = engine.jobs.list_dead_letters()
    assert letters[0].reason == "retry_duration_exceeded"


def test_retry_manager_classification() -> None:
    manager = RetryManager(
        default_policy=RetryPolicy(max_attempts=3, backoff_base_seconds=1.0, jitter_fraction=0.0)
    )
    job = Job(
        job_id="j",
        owner_id="o",
        objective="x",
        status="queued",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=1,
        max_attempts=3,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=1,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    transient = manager.decide(job, TimeoutError("read timed out"))
    assert transient.retry is True
    assert transient.delay_seconds == 1.0
    permanent = manager.decide(job, ValueError("invalid schema"))
    assert permanent.retry is False
    job_exhausted = Job(
        job_id="j",
        owner_id="o",
        objective="x",
        status="queued",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=3,
        max_attempts=3,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=1,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    exhausted = manager.decide(job_exhausted, TimeoutError("again"))
    assert exhausted.retry is False
    assert exhausted.reason == "retry_exhausted"


def test_notification_validation() -> None:
    with pytest.raises(ValueError):
        Notification(kind="bogus", title="x")
    with pytest.raises(ValueError):
        Notification(kind="job_failed", title="  ")
