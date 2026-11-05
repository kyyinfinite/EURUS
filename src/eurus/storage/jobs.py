from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eurus.storage.database import Database, DatabasePath

_JOB_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "cancelled", "unknown_outcome"}
)

JOB_DELIVERY_STATUSES = frozenset({"", "ok", "delivery_queued", "delivery_failed", "suppressed"})

CLAIM_CANDIDATE_LIMIT = 32

DEAD_LETTER_REASONS = frozenset(
    {
        "retry_exhausted",
        "unknown_outcome",
        "retry_duration_exceeded",
        "operator_failed",
        "lease_exhausted",
    }
)
DEAD_LETTER_RESOLUTIONS = frozenset({"replayed", "discarded", "completed"})


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return current.astimezone(UTC)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    owner_id: str
    objective: str
    status: str
    schedule_id: str | None
    scheduled_at: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    available_at: str
    worker_id: str | None
    lease_expires_at: str | None
    lease_version: int
    side_effect_started: bool
    cancel_requested: bool
    result: str
    error: str | None
    created_at: str
    updated_at: str
    priority: int = 5
    source: str = "schedule"
    correlation_id: str = ""
    delivery_status: str = ""


@dataclass(frozen=True, slots=True)
class DeadLetter:
    dead_letter_id: str
    job_id: str
    owner_id: str
    objective: str
    source: str
    reason: str
    detail: str
    attempts: int
    payload: dict[str, Any]
    created_at: str
    resolved_at: str | None
    resolution: str | None


def _job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=str(row["job_id"]),
        owner_id=str(row["owner_id"]),
        objective=str(row["objective"]),
        status=str(row["status"]),
        schedule_id=row["schedule_id"],
        scheduled_at=row["scheduled_at"],
        payload=json.loads(row["payload_json"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        available_at=str(row["available_at"]),
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        lease_version=int(row["lease_version"]),
        side_effect_started=bool(row["side_effect_started"]),
        cancel_requested=bool(row["cancel_requested"]),
        result=str(row["result"]),
        error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        priority=int(row["priority"]),
        source=str(row["source"]),
        correlation_id=str(row["correlation_id"]),
        delivery_status=(
            str(row["delivery_status"]) if "delivery_status" in set(row.keys()) else ""
        ),
    )


def _dead_letter(row: sqlite3.Row) -> DeadLetter:
    return DeadLetter(
        dead_letter_id=str(row["dead_letter_id"]),
        job_id=str(row["job_id"]),
        owner_id=str(row["owner_id"]),
        objective=str(row["objective"]),
        source=str(row["source"]),
        reason=str(row["reason"]),
        detail=str(row["detail"]),
        attempts=int(row["attempts"]),
        payload=json.loads(row["payload_json"]),
        created_at=str(row["created_at"]),
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
    )


class JobStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def enqueue(
        self,
        owner_id: str,
        objective: str,
        *,
        payload: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        scheduled_at: str | None = None,
        max_attempts: int = 1,
        available_at: datetime | None = None,
        job_id: str | None = None,
        priority: int = 5,
        source: str = "manual",
        correlation_id: str = "",
    ) -> Job:
        if not owner_id.strip() or not objective.strip():
            raise ValueError("owner_id and objective are required")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not 1 <= priority <= 9:
            raise ValueError("priority must be between 1 and 9")
        if (schedule_id is None) != (scheduled_at is None):
            raise ValueError("schedule_id and scheduled_at must be provided together")
        current = _now()
        identifier = job_id or uuid.uuid4().hex
        available = _time(_now(available_at)) if available_at is not None else _time(current)
        serialized = json.dumps(
            payload or {}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_jobs(
                    job_id, schedule_id, scheduled_at, owner_id, objective, payload_json,
                    status, attempts, max_attempts, available_at, lease_version,
                    side_effect_started, cancel_requested, result, created_at, updated_at,
                    priority, source, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, 0, 0, 0, '', ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    schedule_id,
                    scheduled_at,
                    owner_id,
                    objective,
                    serialized,
                    max_attempts,
                    available,
                    _time(current),
                    _time(current),
                    priority,
                    source,
                    correlation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM eurus_jobs WHERE job_id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise RuntimeError("enqueued job could not be read back")
        return _job(row)

    def get(self, job_id: str) -> Job | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return _job(row) if row is not None else None

    def list_jobs(
        self, *, owner_id: str | None = None, statuses: tuple[str, ...] = (), limit: int = 100
    ) -> list[Job]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        values: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        if statuses:
            invalid = set(statuses) - _JOB_STATUSES
            if invalid:
                raise ValueError("invalid job status")
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_jobs{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_job(row) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 60,
        global_limit: int | None = None,
        owner_limit: int | None = None,
        owner_id: str | None = None,
    ) -> Job | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if not 5 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 5 and 86400")
        if global_limit is not None and not 1 <= global_limit <= 256:
            raise ValueError("global_limit must be between 1 and 256")
        if owner_limit is not None and not 1 <= owner_limit <= 256:
            raise ValueError("owner_limit must be between 1 and 256")
        current = _now(now)
        current_text = _time(current)
        expires = _time(current + timedelta(seconds=lease_seconds))
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT job_id, owner_id FROM eurus_jobs WHERE status='queued' AND available_at<=?"
            )
            args: list[Any] = [current_text]
            if owner_id is not None:
                query += " AND owner_id=?"
                args.append(owner_id)
            if global_limit is not None:
                query += " AND (SELECT COUNT(*) FROM eurus_jobs WHERE status='running')<?"
                args.append(global_limit)
            query += " ORDER BY priority, available_at, created_at LIMIT ?"
            args.append(CLAIM_CANDIDATE_LIMIT if owner_limit is not None else 1)
            rows = connection.execute(query, args).fetchall()
            row = None
            if rows and owner_limit is not None:
                busy_owners: set[str] = set()
                for candidate in rows:
                    candidate_owner = str(candidate["owner_id"])
                    if candidate_owner in busy_owners:
                        continue
                    owner_running = connection.execute(
                        "SELECT COUNT(*) FROM eurus_jobs WHERE status='running' AND owner_id=?",
                        (candidate_owner,),
                    ).fetchone()[0]
                    if int(owner_running) >= owner_limit:
                        busy_owners.add(candidate_owner)
                        continue
                    row = candidate
                    break
            elif rows:
                row = rows[0]
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                "UPDATE eurus_jobs SET status='running', attempts=attempts+1, worker_id=?, "
                "lease_expires_at=?, lease_version=lease_version+1, updated_at=? "
                "WHERE job_id=? AND status='queued'",
                (worker_id, expires, current_text, row["job_id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM eurus_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            connection.commit()
        if claimed is None:
            raise RuntimeError("claimed job could not be read back")
        return _job(claimed)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        lease_version: int,
        *,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> bool:
        current = _now(now)
        expires = _time(current + timedelta(seconds=lease_seconds))
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_jobs SET lease_expires_at=?, updated_at=? WHERE job_id=? "
                "AND status='running' AND worker_id=? AND lease_version=? "
                "AND lease_expires_at>=? AND cancel_requested=0",
                (expires, _time(current), job_id, worker_id, lease_version, _time(current)),
            )
        return cursor.rowcount == 1

    def mark_side_effect_started(
        self,
        job_id: str,
        worker_id: str,
        lease_version: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required to mark the side-effect boundary")
        current = _now(now)
        query = (
            "UPDATE eurus_jobs SET side_effect_started=1, updated_at=? WHERE job_id=? "
            "AND status='running' AND lease_version=? AND worker_id=? "
            "AND lease_expires_at>=?"
        )
        values: list[Any] = [
            _time(current),
            job_id,
            lease_version,
            worker_id,
            _time(current),
        ]
        with self.database.connection() as connection:
            cursor = connection.execute(query, values)
        return cursor.rowcount == 1

    def finish(
        self,
        job_id: str,
        worker_id: str,
        lease_version: int,
        *,
        status: str,
        result: str = "",
        error: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal job status")
        current = _time(_now(now))
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_jobs SET status=CASE WHEN cancel_requested=1 "
                "THEN 'cancelled' ELSE ? END, "
                "result=?, error=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE job_id=? AND status='running' "
                "AND worker_id=? AND lease_version=? AND lease_expires_at>=?",
                (status, result[:4000], error, current, job_id, worker_id, lease_version, current),
            )
        return cursor.rowcount == 1

    def cancel(self, job_id: str, *, owner_id: str) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_jobs SET status='cancelled', worker_id=NULL, lease_expires_at=NULL, "
                "updated_at=? WHERE job_id=? AND owner_id=? AND status='queued'",
                (_time(_now()), job_id, owner_id),
            )
        return cursor.rowcount == 1

    def set_delivery_status(self, job_id: str, status: str) -> bool:
        if status not in JOB_DELIVERY_STATUSES:
            raise ValueError("invalid job delivery status")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_jobs SET delivery_status=?, updated_at=? WHERE job_id=?",
                (status, _time(_now()), job_id),
            )
        return cursor.rowcount == 1

    def request_cancel(self, job_id: str, *, owner_id: str) -> bool:
        current = _time(_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE eurus_jobs SET status='cancelled', updated_at=? WHERE job_id=? "
                "AND owner_id=? AND status='queued'",
                (current, job_id, owner_id),
            )
            if cursor.rowcount == 0:
                cursor = connection.execute(
                    "UPDATE eurus_jobs SET cancel_requested=1, updated_at=? WHERE job_id=? "
                    "AND owner_id=? AND status='running' AND cancel_requested=0",
                    (current, job_id, owner_id),
                )
            connection.commit()
        return cursor.rowcount == 1

    def recover_expired(
        self, *, now: datetime | None = None, owner_id: str | None = None
    ) -> dict[str, int]:
        current = _now(now)
        current_text = _time(current)
        scope = ""
        scope_values: list[Any] = []
        if owner_id is not None:
            scope = " AND owner_id=?"
            scope_values = [owner_id]
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            safe = connection.execute(
                "UPDATE eurus_jobs SET status='queued', worker_id=NULL, lease_expires_at=NULL, "
                "available_at=?, updated_at=? WHERE status='running' AND lease_expires_at<? "
                "AND side_effect_started=0 AND attempts<max_attempts" + scope,
                (current_text, current_text, current_text, *scope_values),
            ).rowcount
            uncertain = connection.execute(
                "UPDATE eurus_jobs SET status='unknown_outcome', worker_id=NULL, "
                "lease_expires_at=NULL, error='lease_expired_after_side_effect_boundary', "
                "updated_at=? WHERE status='running' AND lease_expires_at<? "
                "AND side_effect_started=1" + scope,
                (current_text, current_text, *scope_values),
            ).rowcount
            exhausted = connection.execute(
                "UPDATE eurus_jobs SET status='failed', worker_id=NULL, lease_expires_at=NULL, "
                "error='lease_expired_attempt_limit', updated_at=? WHERE status='running' "
                "AND lease_expires_at<? AND side_effect_started=0 AND attempts>=max_attempts"
                + scope,
                (current_text, current_text, *scope_values),
            ).rowcount
            connection.commit()
        return {"requeued": safe, "unknown_outcome": uncertain, "failed": exhausted}

    def list_expired_before_finish(self, job_ids: Sequence[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        placeholders = ",".join("?" for _ in job_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT job_id, status FROM eurus_jobs WHERE job_id IN ({placeholders}) "
                "AND status!='running'",
                tuple(job_ids),
            ).fetchall()
        return {str(row["job_id"]): str(row["status"]) for row in rows}

    def schedule_retry(
        self,
        job_id: str,
        worker_id: str,
        lease_version: int,
        *,
        delay_seconds: float,
        error: str | None = None,
        now: datetime | None = None,
        require_clean_side_effect: bool = True,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        current = _now(now)
        available = _time(current + timedelta(seconds=delay_seconds))
        boundary = "AND side_effect_started=0" if require_clean_side_effect else ""
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_jobs SET status='queued', worker_id=NULL, "
                "lease_expires_at=NULL, available_at=?, error=?, updated_at=? "
                "WHERE job_id=? AND status='running' AND worker_id=? AND lease_version=? "
                "AND lease_expires_at>=? AND cancel_requested=0 " + boundary,
                (
                    available,
                    error,
                    _time(current),
                    job_id,
                    worker_id,
                    lease_version,
                    _time(current),
                ),
            )
        return cursor.rowcount == 1

    def replay(self, job_id: str, *, owner_id: str | None = None) -> Job | None:
        current = _time(_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = (
                "UPDATE eurus_jobs SET status='queued', attempts=0, available_at=?, "
                "worker_id=NULL, lease_expires_at=NULL, lease_version=lease_version+1, "
                "side_effect_started=0, cancel_requested=0, error=NULL, result='', "
                "updated_at=? WHERE job_id=? AND status IN "
                "('failed', 'unknown_outcome', 'cancelled')"
            )
            values: tuple[Any, ...] = (current, current, job_id)
            if owner_id is not None:
                query += " AND owner_id=?"
                values += (owner_id,)
            cursor = connection.execute(query, values)
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM eurus_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        return _job(row)

    def record_dead_letter(
        self,
        job: Job,
        *,
        reason: str,
        detail: str = "",
    ) -> DeadLetter:
        if reason not in DEAD_LETTER_REASONS:
            raise ValueError("invalid dead-letter reason")
        current = _time(_now())
        identifier = uuid.uuid4().hex
        payload_json = json.dumps(job.payload, ensure_ascii=False, separators=(",", ":"))
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO eurus_dead_letters(
                        dead_letter_id, job_id, owner_id, objective, source, reason, detail,
                        attempts, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identifier,
                        job.job_id,
                        job.owner_id,
                        job.objective,
                        job.source,
                        reason,
                        detail[:2000],
                        job.attempts,
                        payload_json,
                        current,
                    ),
                )
        except sqlite3.IntegrityError:
            for letter in self.list_dead_letters(owner_id=job.owner_id, limit=500):
                if letter.job_id == job.job_id:
                    return letter
            raise
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_dead_letters WHERE dead_letter_id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise RuntimeError("dead letter could not be read back")
        return _dead_letter(row)

    def list_dead_letters(
        self, *, owner_id: str | None = None, resolved: bool = False, limit: int = 100
    ) -> list[DeadLetter]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses = []
        values: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        if resolved:
            clauses.append("resolved_at IS NOT NULL")
        else:
            clauses.append("resolved_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_dead_letters{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_dead_letter(row) for row in rows]

    def get_dead_letter(
        self, dead_letter_id: str, *, owner_id: str | None = None
    ) -> DeadLetter | None:
        query = "SELECT * FROM eurus_dead_letters WHERE dead_letter_id=?"
        values: list[Any] = [dead_letter_id]
        if owner_id is not None:
            query += " AND owner_id=?"
            values.append(owner_id)
        with self.database.connection() as connection:
            row = connection.execute(query, values).fetchone()
        return _dead_letter(row) if row is not None else None

    def resolve_dead_letter(
        self,
        dead_letter_id: str,
        *,
        resolution: str,
        replay_job: bool = False,
        owner_id: str | None = None,
    ) -> tuple[DeadLetter, Job | None]:
        if resolution not in DEAD_LETTER_RESOLUTIONS:
            raise ValueError("invalid dead-letter resolution")
        current = _time(_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM eurus_dead_letters WHERE dead_letter_id=?"
            values: list[Any] = [dead_letter_id]
            if owner_id is not None:
                query += " AND owner_id=?"
                values.append(owner_id)
            row = connection.execute(query, values).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("dead letter not found")
            letter = _dead_letter(row)
            if letter.resolved_at is not None:
                connection.rollback()
                raise ValueError("dead letter is already resolved")
            connection.execute(
                "UPDATE eurus_dead_letters SET resolved_at=?, resolution=? WHERE dead_letter_id=?",
                (current, resolution, dead_letter_id),
            )
            connection.commit()
        replayed: Job | None = None
        if replay_job:
            replayed = self.replay(letter.job_id, owner_id=owner_id)
        refreshed = self.get_dead_letter(dead_letter_id, owner_id=owner_id)
        if refreshed is None:
            raise RuntimeError("dead letter could not be read back")
        return refreshed, replayed
