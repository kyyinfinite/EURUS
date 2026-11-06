from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eurus.learning.evaluation import EvaluationEngine
from eurus.learning.lessons import LessonStore
from eurus.storage.database import Database, DatabasePath
from eurus.storage.events import EventStore
from eurus.storage.jobs import Job
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.trajectory import redact

log = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "unknown_outcome"})
LEARNING_TASK_STATUSES = frozenset({"pending", "running", "done", "failed", "discarded"})
DEFAULT_MAX_PROCESS_ATTEMPTS = 3
DEFAULT_STALE_RUNNING_SECONDS = 300.0
MAX_LESSON_CANDIDATES_PER_RUN = 3
CANDIDATE_EVENT_TYPES = ("lesson",)
FAILURE_STOP_MARKERS = ("timeout", "budget", "deadline", "provider", "cancelled")
MAX_RETENTION_DAYS = 3650
DEFAULT_RETENTION_BATCH = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LearningTask:
    task_id: str
    run_id: str
    job_id: str
    owner_id: str
    status: str
    attempts: int
    error: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class LearningTickReport:
    processed: int = 0
    failed: int = 0
    lessons_accepted: int = 0
    evaluations_recorded: int = 0
    evaluations_pruned: int = 0
    requeued: int = 0
    missions_processed: int = 0
    missions_failed: int = 0
    missions_skipped: int = 0

    @property
    def to_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "failed": self.failed,
            "lessons_accepted": self.lessons_accepted,
            "evaluations_recorded": self.evaluations_recorded,
            "evaluations_pruned": self.evaluations_pruned,
            "requeued": self.requeued,
            "missions_processed": self.missions_processed,
            "missions_failed": self.missions_failed,
            "missions_skipped": self.missions_skipped,
        }


class LearningCoordinator:
    def __init__(
        self,
        database: Database | DatabasePath,
        *,
        evaluation: EvaluationEngine | None = None,
        lessons: LessonStore | None = None,
        missions: Any | None = None,
        max_process_attempts: int = DEFAULT_MAX_PROCESS_ATTEMPTS,
        stale_running_seconds: float = DEFAULT_STALE_RUNNING_SECONDS,
        batch_limit: int = 20,
        process_inline: bool = True,
        evaluation_retention_days: int | None = None,
        retention_batch_limit: int = DEFAULT_RETENTION_BATCH,
    ) -> None:
        if not 1 <= max_process_attempts <= 10:
            raise ValueError("max_process_attempts must be between 1 and 10")
        if stale_running_seconds < 1:
            raise ValueError("stale_running_seconds must be at least 1")
        if not 1 <= batch_limit <= 200:
            raise ValueError("batch_limit must be between 1 and 200")
        if evaluation_retention_days is not None and not (
            1 <= evaluation_retention_days <= MAX_RETENTION_DAYS
        ):
            raise ValueError(
                "evaluation_retention_days must be between 1 and "
                f"{MAX_RETENTION_DAYS} when provided"
            )
        self.database = database if isinstance(database, Database) else Database(database)
        self.evaluation = evaluation or EvaluationEngine(self.database)
        self.lessons = lessons
        self.missions = missions
        self.runs = RunStore(self.database)
        self.events = EventStore(self.database)
        self.attempts = ToolAttemptStore(self.database)
        self.max_process_attempts = max_process_attempts
        self.stale_running_seconds = stale_running_seconds
        self.batch_limit = batch_limit
        self.process_inline = process_inline
        self.evaluation_retention_days = evaluation_retention_days
        self.retention_batch_limit = retention_batch_limit

    def on_job_finished(self, job: Job, status: str, result: str, error: str | None) -> None:
        if status not in TERMINAL_JOB_STATUSES:
            return
        run_id = _run_reference(job)
        created = self.enqueue(
            run_id=run_id,
            job_id=job.job_id,
            owner_id=job.owner_id,
        )
        if not created:
            return
        if self.process_inline:
            try:
                self.tick(limit=1, owner_id=job.owner_id)
            except Exception:
                log.exception(
                    "inline learning processing failed for run %s (task stays recoverable)",
                    run_id,
                )

    def enqueue(self, *, run_id: str, job_id: str, owner_id: str) -> bool:
        current = _now()
        task_id = uuid.uuid4().hex
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO eurus_learning_tasks(
                        task_id, run_id, job_id, owner_id, status, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)""",
                    (task_id, run_id, job_id, owner_id, _time(current), _time(current)),
                )
        except Exception:
            log.exception("learning task enqueue failed for run %s", run_id)
            return False
        return True

    def pending_tasks(self, *, limit: int = 20, owner_id: str | None = None) -> list[LearningTask]:
        scope = ""
        values: list[Any] = []
        if owner_id is not None:
            scope = " AND owner_id=?"
            values.append(owner_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_learning_tasks WHERE status='pending' "
                f"{scope} ORDER BY created_at LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_task(row) for row in rows]

    def task_for_run(self, run_id: str) -> LearningTask | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_learning_tasks WHERE run_id=?", (run_id,)
            ).fetchone()
        return _task(row) if row is not None else None

    def tick(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
        owner_id: str | None = None,
    ) -> LearningTickReport:
        report = LearningTickReport()
        current = now or _now()
        report.evaluations_pruned = self._prune_expired()
        self._requeue_stale(current, report, owner_id=owner_id)
        tasks = self.pending_tasks(limit=min(limit, self.batch_limit), owner_id=owner_id)
        for task in tasks:
            claimed = self._claim(task, owner_id=owner_id)
            if not claimed:
                continue
            try:
                outcome = self._process(task)
            except Exception as exc:
                log.exception("learning task %s failed", task.task_id)
                self._fail(task, f"{type(exc).__name__}: {exc}", owner_id=owner_id)
                report.failed += 1
                continue
            self._complete(task, owner_id=owner_id)
            report.processed += 1
            report.evaluations_recorded += outcome.evaluations_recorded
            report.lessons_accepted += outcome.lessons_accepted
        self._process_missions(report, owner_id=owner_id)
        return report

    def _prune_expired(self) -> int:
        if self.evaluation_retention_days is None:
            return 0
        try:
            return self.evaluation.prune(
                retention_days=self.evaluation_retention_days,
                batch_limit=self.retention_batch_limit,
            )
        except Exception:
            log.exception("evaluation retention pruning failed")
            return 0

    def _process_missions(self, report: LearningTickReport, *, owner_id: str | None) -> None:
        if self.missions is None:
            return
        try:
            mission_report = self.missions.backfill(owner_id=owner_id, limit=self.batch_limit)
        except Exception:
            log.exception("mission learning backfill failed")
            report.missions_failed += 1
            return
        report.missions_processed += mission_report.processed
        report.missions_failed += mission_report.failed
        report.missions_skipped += mission_report.skipped
        report.failed += mission_report.failed
        report.evaluations_recorded += mission_report.evaluations_recorded
        report.lessons_accepted += mission_report.lessons_accepted

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]:
        scope = ""
        values: list[Any] = []
        if owner_id is not None:
            scope = " AND owner_id=?"
            values.append(owner_id)
        with self.database.connection() as connection:
            counts: dict[str, int] = {}
            for status in LEARNING_TASK_STATUSES:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM eurus_learning_tasks WHERE status=?{scope}",
                    (status, *values),
                ).fetchone()
                counts[status] = int(row[0])
        if self.missions is not None:
            try:
                mission_stats = self.missions.stats(owner_id=owner_id)
            except Exception:
                log.exception("mission learning stats collection failed")
                mission_stats = {}
            counts["mission_terminal"] = int(mission_stats.get("terminal", 0))
            counts["mission_learned"] = int(mission_stats.get("learned", 0))
            counts["mission_pending"] = int(mission_stats.get("pending", 0))
        return counts

    def deactivate(self, provenance_id: str, *, owner_id: str | None = None) -> bool:
        record = self.evaluation.get_provenance(provenance_id)
        if record is None:
            return False
        if owner_id is not None and not self._provenance_owned(record, owner_id):
            log.warning("refusing cross-owner deactivation of provenance %s", provenance_id)
            return False
        reversed_ok = self.evaluation.reverse(provenance_id)
        if not reversed_ok:
            return False
        if self.lessons is not None and record.get("artifact_type") == "lesson":
            artifact_id = str(record.get("artifact_id", ""))
            if artifact_id.isdigit():
                self.lessons.remove(int(artifact_id), owner_id=owner_id)
        return True

    def _provenance_owned(self, record: Mapping[str, Any], owner_id: str) -> bool:
        source_run_id = str(record.get("source_run_id") or "")
        artifact_id = str(record.get("artifact_id") or "")
        candidates = [source_run_id]
        if "." in source_run_id:
            candidates.append(source_run_id.rsplit(".", 1)[0])
        with self.database.connection() as connection:
            for run_id in candidates:
                if not run_id:
                    continue
                row = connection.execute(
                    "SELECT owner_id FROM eurus_learning_tasks WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is not None:
                    return str(row["owner_id"]) == owner_id
            for job_id in (*candidates, artifact_id):
                if not job_id:
                    continue
                row = connection.execute(
                    "SELECT owner_id FROM eurus_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is not None:
                    return str(row["owner_id"]) == owner_id
        return False

    def _requeue_stale(
        self, current: datetime, report: LearningTickReport, *, owner_id: str | None
    ) -> None:
        cutoff = _time(current - timedelta(seconds=self.stale_running_seconds))
        scope = ""
        scope_values: list[Any] = []
        if owner_id is not None:
            scope = " AND owner_id=?"
            scope_values = [owner_id]
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT task_id, attempts FROM eurus_learning_tasks "
                f"WHERE status='running' AND updated_at<?{scope}",
                (cutoff, *scope_values),
            ).fetchall()
            for row in rows:
                if int(row["attempts"]) >= self.max_process_attempts:
                    connection.execute(
                        "UPDATE eurus_learning_tasks SET status='failed', "
                        "error='stale running task exhausted attempts', updated_at=? "
                        "WHERE task_id=? AND status='running'",
                        (_time(current), row["task_id"]),
                    )
                    continue
                connection.execute(
                    "UPDATE eurus_learning_tasks SET status='pending', updated_at=? "
                    "WHERE task_id=? AND status='running'",
                    (_time(current), row["task_id"]),
                )
                report.requeued += 1

    def _claim(self, task: LearningTask, *, owner_id: str | None = None) -> bool:
        scope = ""
        values: list[Any] = [_time(_now()), task.task_id]
        if owner_id is not None:
            scope = " AND owner_id=?"
            values.append(owner_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_learning_tasks SET status='running', "
                f"attempts=attempts+1, updated_at=? WHERE task_id=? AND status='pending'{scope}",
                values,
            )
        return cursor.rowcount == 1

    def _complete(self, task: LearningTask, *, owner_id: str | None = None) -> None:
        scope = ""
        values: list[Any] = [_time(_now()), task.task_id]
        if owner_id is not None:
            scope = " AND owner_id=?"
            values.append(owner_id)
        with self.database.connection() as connection:
            connection.execute(
                f"UPDATE eurus_learning_tasks SET status='done', error=NULL, updated_at=? "
                f"WHERE task_id=?{scope}",
                values,
            )

    def _fail(self, task: LearningTask, error: str, *, owner_id: str | None = None) -> None:
        scope = ""
        scope_values: list[Any] = []
        if owner_id is not None:
            scope = " AND owner_id=?"
            scope_values = [owner_id]
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM eurus_learning_tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if row is None:
                return
            if int(row["attempts"]) >= self.max_process_attempts:
                connection.execute(
                    "UPDATE eurus_learning_tasks SET status='failed', error=?, updated_at=? "
                    f"WHERE task_id=?{scope}",
                    (error[:1000], _time(_now()), task.task_id, *scope_values),
                )
            else:
                connection.execute(
                    "UPDATE eurus_learning_tasks SET status='pending', error=?, updated_at=? "
                    f"WHERE task_id=?{scope}",
                    (error[:1000], _time(_now()), task.task_id, *scope_values),
                )

    def _process(self, task: LearningTask) -> LearningTickReport:
        outcome = LearningTickReport()
        run = self.runs.get(task.run_id)
        attempts = self.attempts.list_for_run(task.run_id)
        status_value = str(getattr(getattr(run, "status", ""), "value", "")) if run else ""
        stop_reason = str(getattr(run, "stop_reason", "") or "") if run else ""
        if run is None:
            completed = False
            status_value = "unpersisted"
        else:
            completed = status_value == "completed"
        correctness_score = (
            1.0 if completed and getattr(run, "result_ref", None) else (0.5 if completed else 0.0)
        )
        self.evaluation.record(
            subject_type="job",
            subject_id=task.job_id,
            dimension="task_correctness",
            score=correctness_score,
            passed=completed,
            evidence=[
                f"run_id={task.run_id}",
                f"status={status_value}",
                f"stop_reason={stop_reason or 'none'}",
            ],
            source_run_id=task.run_id,
        )
        outcome.evaluations_recorded += 1
        if attempts:
            errors = sum(1 for attempt in attempts if attempt.get("is_error"))
            ratio = 1.0 - (errors / len(attempts))
            self.evaluation.record(
                subject_type="job",
                subject_id=task.job_id,
                dimension="tool_reliability",
                score=round(ratio, 3),
                passed=ratio >= 0.5,
                evidence=[
                    f"attempts={len(attempts)}",
                    f"errors={errors}",
                ],
                source_run_id=task.run_id,
            )
            outcome.evaluations_recorded += 1
        candidates = self._lesson_candidates(task, status_value, stop_reason)
        if self.lessons is None:
            return outcome
        from eurus.learning.evaluation import LessonCandidate, LessonValidator

        validator = LessonValidator(self.lessons)
        for text in candidates[:MAX_LESSON_CANDIDATES_PER_RUN]:
            candidate = LessonCandidate(text=text)
            accepted, reason = validator.review(candidate, owner_id=task.owner_id)
            if not accepted:
                continue
            lesson_id = self.lessons.add(candidate.text, owner_id=task.owner_id)
            if lesson_id is None:
                continue
            self.evaluation.record_artifact_provenance(
                artifact_type="lesson",
                artifact_id=str(lesson_id),
                summary=candidate.text[:2000],
                source_run_id=task.run_id,
            )
            outcome.lessons_accepted += 1
        return outcome

    def _lesson_candidates(
        self, task: LearningTask, status_value: str, stop_reason: str
    ) -> list[str]:
        candidates: list[str] = []
        for event in self.events.list_for_run(task.run_id, limit=1000):
            if event.event_type not in CANDIDATE_EVENT_TYPES:
                continue
            payload: Mapping[str, Any] = event.payload or {}
            text = str(payload.get("text", "")).strip()
            if text:
                candidates.append(redact(text[:200]))
        if not candidates and status_value in ("failed", "cancelled") and stop_reason:
            lowered = stop_reason.lower()
            if any(marker in lowered for marker in FAILURE_STOP_MARKERS):
                candidates.append(redact(f"Failure signal for {task.job_id}: {stop_reason}"))
        return [
            candidate.strip() for candidate in candidates if 15 <= len(candidate.strip()) <= 200
        ]


def _run_reference(job: Job) -> str:
    if job.lease_version <= 1:
        return job.job_id
    return f"{job.job_id}.{job.lease_version}"


def _task(row: Any) -> LearningTask:
    return LearningTask(
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        owner_id=str(row["owner_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
