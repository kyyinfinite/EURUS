from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from eurus.storage.jobs import DeadLetter, Job, JobStore

log = logging.getLogger(__name__)

OccurrenceFinalizer = Callable[[str, str, str], bool]
UnknownOutcomeObserver = Callable[[Job], None]

RECOVERED_FAILURE_WINDOW_LIMIT = 500


class ScheduleRunSync(Protocol):
    def finish_run(self, run_id: str, *, status: str, result: str = "") -> bool: ...


@dataclass(slots=True)
class ReconciliationReport:
    recovered: dict[str, int] = field(default_factory=dict)
    schedule_runs_synced: int = 0
    occurrences_finalized: int = 0
    unknown_outcomes_flagged: int = 0
    dead_letters_recorded: int = 0


class ReconciliationService:
    def __init__(
        self,
        jobs: JobStore,
        *,
        schedules: ScheduleRunSync | None = None,
        occurrence_finalizer: OccurrenceFinalizer | None = None,
        unknown_outcome_observer: UnknownOutcomeObserver | None = None,
    ) -> None:
        self.jobs = jobs
        self.schedules = schedules
        self.occurrence_finalizer = occurrence_finalizer
        self.unknown_outcome_observer = unknown_outcome_observer

    def reconcile(
        self, *, now: datetime | None = None, owner_id: str | None = None
    ) -> ReconciliationReport:
        report = ReconciliationReport()
        report.recovered = self.jobs.recover_expired(now=now, owner_id=owner_id)
        report.dead_letters_recorded = self._record_recovery_dead_letters(
            owner_id=owner_id, now=now
        )
        report.unknown_outcomes_flagged = self._flag_unknown_outcomes(owner_id=owner_id)
        report.schedule_runs_synced = self._sync_schedule_runs(owner_id=owner_id)
        report.occurrences_finalized = self._finalize_occurrences(owner_id=owner_id)
        return report

    def _record_recovery_dead_letters(self, *, owner_id: str | None, now: datetime | None) -> int:
        recovered_failures = self.jobs.list_jobs(
            owner_id=owner_id,
            statuses=("failed", "unknown_outcome"),
            limit=RECOVERED_FAILURE_WINDOW_LIMIT,
        )
        if not recovered_failures:
            return 0
        known_letters = {
            letter.job_id for letter in self.jobs.list_dead_letters(owner_id=owner_id, limit=500)
        }
        recorded = 0
        for job in recovered_failures:
            if job.error != "lease_expired_attempt_limit":
                continue
            if job.job_id in known_letters:
                continue
            try:
                self.jobs.record_dead_letter(job, reason="lease_exhausted", detail=job.error)
            except Exception:
                log.exception("dead-letter recording failed for recovered job %s", job.job_id)
                continue
            recorded += 1
            if self.unknown_outcome_observer is not None:
                try:
                    self.unknown_outcome_observer(job)
                except Exception:
                    log.exception("recovered-failure observer failed for job %s", job.job_id)
        return recorded

    def _sync_schedule_runs(self, *, owner_id: str | None) -> int:
        if self.schedules is None:
            return 0
        synced = 0
        terminal = self.jobs.list_jobs(
            owner_id=owner_id,
            statuses=("unknown_outcome", "failed", "cancelled"),
            limit=1000,
        )
        for job in terminal:
            if not job.schedule_id:
                continue
            if self.schedules.finish_run(
                job.job_id, status=job.status, result=job.error or job.result
            ):
                synced += 1
        return synced

    def _finalize_occurrences(self, *, owner_id: str | None) -> int:
        if self.occurrence_finalizer is None:
            return 0
        finalized = 0
        for status in ("completed", "failed", "cancelled", "unknown_outcome"):
            for job in self.jobs.list_jobs(owner_id=owner_id, statuses=(status,), limit=1000):
                if not job.schedule_id:
                    continue
                if self.occurrence_finalizer(job.job_id, job.status, job.error or job.result):
                    finalized += 1
        return finalized

    def _flag_unknown_outcomes(self, *, owner_id: str | None) -> int:
        flagged = 0
        known_letters = {
            letter.job_id for letter in self.jobs.list_dead_letters(owner_id=owner_id, limit=500)
        }
        for job in self.jobs.list_jobs(
            owner_id=owner_id, statuses=("unknown_outcome",), limit=1000
        ):
            if job.job_id in known_letters:
                continue
            self.jobs.record_dead_letter(
                job, reason="unknown_outcome", detail=job.error or "outcome uncertain"
            )
            if self.unknown_outcome_observer is not None:
                try:
                    self.unknown_outcome_observer(job)
                except Exception:
                    log.exception("unknown-outcome observer failed for job %s", job.job_id)
            flagged += 1
        return flagged


def dead_letters_for_owner(jobs: JobStore, owner_id: str, *, limit: int = 100) -> list[DeadLetter]:
    return jobs.list_dead_letters(owner_id=owner_id, limit=limit)
