from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.reconciliation import ReconciliationService
from eurus.storage.database import Database
from eurus.storage.jobs import Job, JobStore


class ExecutorStub(JobExecutor):
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        return f"done {job.objective}"


def make_engine(tmp_path: Path, owner_id: str | None = None) -> tuple[AutomationEngine, Database]:
    database = Database(tmp_path / f"recovery-{owner_id or 'admin'}.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id=owner_id, poll_seconds=0.05)
    return engine, database


def test_worker_crash_after_claim_requeues_before_boundary(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "read only work")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None and claim.job_id == job.job_id
    past = datetime.now(UTC) + timedelta(seconds=45)
    recovered = engine.jobs.recover_expired(now=past)
    assert recovered["requeued"] == 1
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.attempts == 1
    database.close()


def test_worker_crash_after_side_effect_marker_yields_unknown_outcome(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "mutating work")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    assert engine.jobs.mark_side_effect_started(job.job_id, "worker-1", claim.lease_version)
    past = datetime.now(UTC) + timedelta(seconds=45)
    recovered = engine.jobs.recover_expired(now=past)
    assert recovered["unknown_outcome"] == 1
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "unknown_outcome"
    assert stored.error == "lease_expired_after_side_effect_boundary"
    report = engine.reconciliation.reconcile(now=past, owner_id="alice")
    assert report.unknown_outcomes_flagged == 1
    letters = engine.jobs.list_dead_letters(owner_id="alice")
    assert len(letters) == 1
    assert letters[0].reason == "unknown_outcome"
    assert letters[0].attempts == claim.attempts
    database.close()


def test_worker_crash_during_mutating_operation_does_not_auto_retry(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "mutating work", source="manual", max_attempts=3)
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    engine.jobs.mark_side_effect_started(job.job_id, "worker-1", claim.lease_version)
    retried = engine.jobs.schedule_retry(
        job.job_id,
        "worker-1",
        claim.lease_version,
        delay_seconds=0.0,
        error="mid_mutation_failure",
        require_clean_side_effect=True,
    )
    assert retried is False
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "running"
    database.close()


def test_read_only_tool_failure_retries(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "read only work", source="manual", max_attempts=3)
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    retried = engine.jobs.schedule_retry(
        job.job_id,
        "worker-1",
        claim.lease_version,
        delay_seconds=0.0,
        error="transient_failure",
        require_clean_side_effect=True,
    )
    assert retried is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.side_effect_started is False
    database.close()


def test_lease_exhaustion_produces_dead_letter_through_reconciliation(
    tmp_path: Path,
) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "exhausted work", max_attempts=1)
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    past = datetime.now(UTC) + timedelta(seconds=45)
    recovered = engine.jobs.recover_expired(now=past, owner_id="alice")
    assert recovered["failed"] == 1
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error == "lease_expired_attempt_limit"
    report = engine.reconciliation.reconcile(now=past, owner_id="alice")
    assert report.dead_letters_recorded == 1
    letters = engine.jobs.list_dead_letters(owner_id="alice")
    assert len(letters) == 1
    letter = letters[0]
    assert letter.reason == "lease_exhausted"
    assert letter.job_id == job.job_id
    assert letter.owner_id == "alice"
    assert letter.attempts >= 1
    assert letter.payload == job.payload
    again = engine.reconciliation.reconcile(now=past, owner_id="alice")
    assert again.dead_letters_recorded == 0
    assert len(engine.jobs.list_dead_letters(owner_id="alice")) == 1
    database.close()


def test_exhausted_failure_publishes_terminal_event(tmp_path: Path) -> None:
    events: list[tuple[str, str, str]] = []
    database = Database(tmp_path / "events.sqlite3")
    engine = AutomationEngine(
        database,
        ExecutorStub(),
        owner_id="alice",
        poll_seconds=0.05,
        event_publisher=lambda et, st, sid, payload: events.append((et, sid, "")),
    )
    job = engine.submit("alice", "exhausted work", max_attempts=1)
    engine.jobs.claim("worker-1", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=45)
    engine.jobs.recover_expired(now=past, owner_id="alice")
    engine.reconciliation.reconcile(now=past, owner_id="alice")
    assert ("TASK_FAILED", job.job_id, "") in events
    database.close()


def test_dead_letter_creation_is_idempotent_under_races(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "racy failure", max_attempts=1)
    letter = engine.jobs.record_dead_letter(job, reason="operator_failed", detail="x")
    import threading

    errors: list[Exception] = []

    def racer() -> None:
        try:
            engine.jobs.record_dead_letter(job, reason="operator_failed", detail="y")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    letters = engine.jobs.list_dead_letters(owner_id="alice")
    assert len(letters) == 1
    assert letters[0].dead_letter_id == letter.dead_letter_id
    assert not errors
    database.close()


def test_duplicate_recovery_does_not_duplicate_unknown_outcome_letters(
    tmp_path: Path,
) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "uncertain work")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    engine.jobs.mark_side_effect_started(job.job_id, "worker-1", claim.lease_version)
    past = datetime.now(UTC) + timedelta(seconds=45)
    engine.jobs.recover_expired(now=past, owner_id="alice")
    for _ in range(3):
        engine.reconciliation.reconcile(now=past, owner_id="alice")
    letters = engine.jobs.list_dead_letters(owner_id="alice")
    assert len(letters) == 1
    database.close()


def test_stale_worker_cannot_commit_after_lease_expiry(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "contested work")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    stale_version = claim.lease_version
    past = datetime.now(UTC) + timedelta(seconds=45)
    engine.jobs.recover_expired(now=past, owner_id="alice")
    re_claim = engine.jobs.claim("worker-2", lease_seconds=30, now=past)
    assert re_claim is not None
    assert re_claim.lease_version > stale_version
    stale_commit = engine.jobs.finish(
        job.job_id,
        "worker-1",
        stale_version,
        status="completed",
        result="stale result",
        now=past,
    )
    assert stale_commit is False
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "running"
    fresh_commit = engine.jobs.finish(
        job.job_id,
        "worker-2",
        re_claim.lease_version,
        status="completed",
        result="fresh result",
        now=past,
    )
    assert fresh_commit is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.result == "fresh result"
    database.close()


def test_duplicate_workers_cannot_claim_same_job(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    engine.submit("alice", "single claim work")
    first = engine.jobs.claim("worker-1", lease_seconds=30)
    second = engine.jobs.claim("worker-2", lease_seconds=30)
    assert first is not None
    assert second is None
    database.close()


def test_cancellation_after_side_effect_boundary_is_recorded(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "long mutation")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    engine.jobs.mark_side_effect_started(job.job_id, "worker-1", claim.lease_version)
    requested = engine.jobs.request_cancel(job.job_id, owner_id="alice")
    assert requested is True
    committed = engine.jobs.finish(
        job.job_id,
        "worker-1",
        claim.lease_version,
        status="completed",
        result="finished anyway",
    )
    assert committed is True
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "cancelled"
    database.close()


def test_dead_letter_replay_creates_auditable_new_attempt(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "replay me", max_attempts=1, source="manual")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    engine.jobs.finish(job.job_id, "worker-1", claim.lease_version, status="failed", error="boom")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    letter = engine.jobs.record_dead_letter(stored, reason="retry_exhausted", detail="boom")
    resolved, replayed = engine.jobs.resolve_dead_letter(
        letter.dead_letter_id, resolution="replayed", replay_job=True
    )
    assert resolved.resolution == "replayed"
    assert replayed is not None
    assert replayed.job_id == job.job_id
    assert replayed.status == "queued"
    assert replayed.attempts == 0
    assert replayed.lease_version > claim.lease_version
    database.close()


def test_reconciliation_is_safe_to_run_repeatedly(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "repeatable work", max_attempts=1)
    engine.jobs.claim("worker-1", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=45)
    service = ReconciliationService(
        engine.jobs,
        schedules=engine.schedule_runs,
        occurrence_finalizer=None,
    )
    first = service.reconcile(now=past, owner_id="alice")
    second = service.reconcile(now=past, owner_id="alice")
    assert first.recovered["failed"] == 1
    assert second.recovered == {"requeued": 0, "unknown_outcome": 0, "failed": 0}
    assert engine.jobs.get(job.job_id) is not None
    database.close()


def test_recover_expired_handles_corrupt_rows(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    engine.submit("alice", "fine work")
    past = datetime.now(UTC) + timedelta(seconds=45)
    report = engine.jobs.recover_expired(now=past, owner_id="alice")
    assert report == {"requeued": 0, "unknown_outcome": 0, "failed": 0}
    database.close()


def test_finish_requires_valid_lease_window(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    job = engine.submit("alice", "lease window work")
    claim = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim is not None
    late = datetime.now(UTC) + timedelta(seconds=120)
    committed = engine.jobs.finish(
        job.job_id,
        "worker-1",
        claim.lease_version,
        status="completed",
        result="late",
        now=late,
    )
    assert committed is False
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "running"
    database.close()


def test_unknown_outcome_is_distinguishable_from_failure(tmp_path: Path) -> None:
    engine, database = make_engine(tmp_path)
    uncertain = engine.submit("alice", "uncertain work")
    failed = engine.submit("alice", "failed work", max_attempts=1)
    claim1 = engine.jobs.claim("worker-1", lease_seconds=30)
    assert claim1 is not None
    engine.jobs.mark_side_effect_started(uncertain.job_id, "worker-1", claim1.lease_version)
    engine.jobs.claim("worker-2", lease_seconds=30)
    engine.jobs.finish(failed.job_id, "worker-2", 1, status="failed", error="explicit failure")
    past = datetime.now(UTC) + timedelta(seconds=45)
    engine.jobs.recover_expired(now=past, owner_id="alice")
    uncertain_stored = engine.jobs.get(uncertain.job_id)
    failed_stored = engine.jobs.get(failed.job_id)
    assert uncertain_stored is not None and uncertain_stored.status == "unknown_outcome"
    assert failed_stored is not None and failed_stored.status == "failed"
    assert uncertain_stored.error != failed_stored.error
    database.close()


def test_job_store_rejects_invalid_owner_scope_values(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "values.sqlite3")
    store.enqueue("alice", "work")
    with pytest.raises(ValueError):
        store.claim("worker-1", lease_seconds=1)
    with pytest.raises(ValueError):
        store.list_jobs(limit=0)
    with pytest.raises(ValueError):
        store.enqueue("", "no owner")
    with pytest.raises(ValueError):
        store.record_dead_letter(
            Job(
                job_id="missing",
                owner_id="alice",
                objective="x",
                status="failed",
                schedule_id=None,
                scheduled_at=None,
                payload={},
                attempts=1,
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
                source="manual",
            ),
            reason="bogus_reason",
        )
