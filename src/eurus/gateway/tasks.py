from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from eurus.runtime.models import TERMINAL_STATUSES, Run, RunStatus
from eurus.runtime.recovery import RecoveryAction, classify_recovery
from eurus.runtime.state_machine import ALLOWED_TRANSITIONS
from eurus.storage.sqlite import managed

TaskStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "waiting_input",
    "retry_wait",
    "verifying",
    "interrupted",
    "completed",
    "failed",
    "cancelled",
]
TRANSITIONS: dict[str, frozenset[str]] = {
    status.value: frozenset(target.value for target in targets)
    for status, targets in ALLOWED_TRANSITIONS.items()
}
TERMINAL_TASK_STATUSES = frozenset(status.value for status in TERMINAL_STATUSES)
ACTIVE_TASK_STATUSES = frozenset(
    status for status in TRANSITIONS if status not in TERMINAL_TASK_STATUSES
)
ORPHANABLE_TASK_STATUSES = ACTIVE_TASK_STATUSES - {RunStatus.INTERRUPTED.value}
SAFE_RESUME_ACTIONS = frozenset({RecoveryAction.START_FRESH, RecoveryAction.RESUME_CHECKPOINT})
ORPHAN_ERROR = "process interrupted; review before resubmitting"
DEFAULT_PLATFORM = "telegram"
UPDATE_RETENTION_SECONDS = 7 * 24 * 3600.0
PRUNE_EVERY_CLAIMS = 500
LEGACY_STATUS_MAP = {"paused": "interrupted", "recovering": "interrupted"}


def scope_key(platform: str, raw_id: str) -> str:
    if not platform.strip() or not raw_id.strip():
        raise ValueError("platform and id must not be empty")
    return raw_id if platform == DEFAULT_PLATFORM else f"{platform}:{raw_id}"


class InvalidTaskTransition(ValueError):
    pass


class RecoveryBlocked(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class DurableTask:
    task_id: str
    owner_id: str
    session_id: str
    objective: str
    status: TaskStatus
    state: dict[str, Any]
    created_at: str
    updated_at: str
    error: str = ""
    run_id: str = ""
    max_attempts: int = 1
    deadline_at: str | None = None

    @property
    def attempts_used(self) -> int:
        return int(self.state.get("attempts", 0))

    @property
    def failure_kind(self) -> str | None:
        kind = self.state.get("failure_kind")
        return str(kind) if kind else None


class TaskStore:
    def __init__(self, path: Path, *, update_retention: float = UPDATE_RETENTION_SECONDS) -> None:
        if update_retention <= 0:
            raise ValueError("update_retention must be positive")
        self.update_retention = update_retention
        self._claims_since_prune = 0
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_tasks (
                    task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, objective TEXT NOT NULL,
                    status TEXT NOT NULL, state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS gateway_tasks_owner_updated
                    ON gateway_tasks(owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS gateway_sessions (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(user_id, chat_id)
                );
                CREATE TABLE IF NOT EXISTS gateway_chat_locks (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, locked INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(user_id, chat_id)
                );
                CREATE TABLE IF NOT EXISTS gateway_updates (
                    platform TEXT NOT NULL, update_id TEXT NOT NULL,
                    received_at TEXT NOT NULL, PRIMARY KEY(platform, update_id)
                );
                """
            )
            for legacy, current in LEGACY_STATUS_MAP.items():
                db.execute(
                    "UPDATE gateway_tasks SET status=? WHERE status=?",
                    (current, legacy),
                )
            for column, definition in (
                ("run_id", "TEXT NOT NULL DEFAULT ''"),
                ("max_attempts", "INTEGER NOT NULL DEFAULT 1"),
                ("deadline_at", "TEXT"),
            ):
                with suppress(sqlite3.OperationalError):
                    db.execute(f"ALTER TABLE gateway_tasks ADD COLUMN {column} {definition}")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with managed(self.path, rows=True) as db:
            yield db

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def create(
        self,
        owner_id: str,
        session_id: str,
        objective: str,
        state: dict[str, Any] | None = None,
        *,
        max_attempts: int = 1,
        deadline_at: str | None = None,
    ) -> DurableTask:
        if not owner_id or not session_id or not objective.strip():
            raise ValueError("owner, session, and objective are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        now = _now()
        task = DurableTask(
            uuid.uuid4().hex,
            owner_id,
            session_id,
            objective,
            "queued",
            state or {},
            now,
            now,
            "",
            "",
            max_attempts,
            deadline_at,
        )
        with self._connection() as db:
            db.execute(
                """INSERT INTO gateway_tasks (
                    task_id, owner_id, session_id, objective, status, state_json,
                    created_at, updated_at, error, run_id, max_attempts, deadline_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.owner_id,
                    task.session_id,
                    task.objective,
                    task.status,
                    json.dumps(task.state, ensure_ascii=False),
                    now,
                    now,
                    "",
                    "",
                    task.max_attempts,
                    task.deadline_at,
                ),
            )
        return task

    def link_run(self, task_id: str, run_id: str) -> bool:
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE gateway_tasks SET run_id=?, updated_at=? WHERE task_id=?",
                (run_id, _now(), task_id),
            )
        return cursor.rowcount == 1

    def get(self, task_id: str, *, owner_id: str | None = None) -> DurableTask | None:
        with self._connection() as db:
            if owner_id is None:
                row = db.execute(
                    "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM gateway_tasks WHERE task_id=? AND owner_id=?",
                    (task_id, owner_id),
                ).fetchone()
        return self._row(row) if row else None

    def list_owner(self, owner_id: str, limit: int = 20) -> list[DurableTask]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM gateway_tasks WHERE owner_id=? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def transition(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        state: dict[str, Any] | None = None,
        error: str | None = None,
        owner_id: str | None = None,
    ) -> DurableTask:
        with self._write_connection() as db:
            row = self._locked_row(db, task_id, owner_id)
            current = str(row["status"])
            if status not in TRANSITIONS.get(current, frozenset()):
                raise InvalidTaskTransition(f"invalid task transition: {current} -> {status}")
            new_state = state if state is not None else json.loads(row["state_json"])
            new_error = str(row["error"]) if error is None else error
            db.execute(
                "UPDATE gateway_tasks SET status=?, state_json=?, updated_at=?, error=? "
                "WHERE task_id=?",
                (status, json.dumps(new_state, ensure_ascii=False), _now(), new_error, task_id),
            )
            updated = db.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row(updated)

    def complete(self, task_id: str, state: dict[str, Any]) -> DurableTask:
        with self._write_connection() as db:
            row = self._locked_row(db, task_id, None)
            current = str(row["status"])
            if current not in {"running", "verifying"}:
                raise InvalidTaskTransition(f"invalid task transition: {current} -> completed")
            db.execute(
                "UPDATE gateway_tasks SET status='completed', state_json=?, updated_at=? "
                "WHERE task_id=?",
                (json.dumps(state, ensure_ascii=False), _now(), task_id),
            )
            updated = db.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row(updated)

    def claim(self, task_id: str) -> DurableTask | None:
        with self._write_connection() as db:
            row = db.execute("SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or str(row["status"]) != "queued":
                return None
            state = json.loads(row["state_json"])
            state["attempts"] = int(state.get("attempts", 0)) + 1
            db.execute(
                "UPDATE gateway_tasks SET status='running', state_json=?, updated_at=? "
                "WHERE task_id=? AND status='queued'",
                (json.dumps(state, ensure_ascii=False), _now(), task_id),
            )
            claimed = db.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row(claimed)

    def requeue_interrupted(
        self,
        task_id: str,
        owner_id: str,
        *,
        acknowledge_side_effects: bool = False,
    ) -> DurableTask:
        with self._write_connection() as db:
            row = self._locked_row(db, task_id, owner_id)
            if str(row["status"]) != "interrupted":
                raise InvalidTaskTransition("only interrupted tasks can be requeued")
            state = json.loads(row["state_json"])
            recovery = state.get("recovery")
            action = RecoveryAction.MANUAL_REVIEW
            if isinstance(recovery, dict):
                try:
                    action = RecoveryAction(str(recovery.get("action")))
                except ValueError:
                    action = RecoveryAction.MANUAL_REVIEW
            if action not in SAFE_RESUME_ACTIONS and not acknowledge_side_effects:
                raise RecoveryBlocked(
                    "task may already have caused external side effects; "
                    "explicit acknowledgement is required"
                )
            previous = recovery if isinstance(recovery, dict) else {}
            state["recovery"] = {**previous, "requeued": True}
            db.execute(
                "UPDATE gateway_tasks SET status='queued', state_json=?, updated_at=?, error='' "
                "WHERE task_id=?",
                (json.dumps(state, ensure_ascii=False), _now(), task_id),
            )
            updated = db.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row(updated)

    @staticmethod
    def _locked_row(db: sqlite3.Connection, task_id: str, owner_id: str | None) -> sqlite3.Row:
        query = "SELECT * FROM gateway_tasks WHERE task_id=?"
        args: tuple[str, ...] = (task_id,)
        if owner_id is not None:
            query += " AND owner_id=?"
            args = (task_id, owner_id)
        found: sqlite3.Row | None = db.execute(query, args).fetchone()
        if found is None:
            raise KeyError("task not found")
        return found

    def current_session(self, user_id: str, chat_id: str, platform: str = DEFAULT_PLATFORM) -> str:
        user_key = scope_key(platform, user_id)
        chat_key = scope_key(platform, chat_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT session_id FROM gateway_sessions WHERE user_id=? AND chat_id=?",
                (user_key, chat_key),
            ).fetchone()
            if row is not None:
                return str(row["session_id"])
            session_id = f"{platform}:{user_id}:{chat_id}"
            db.execute(
                "INSERT OR IGNORE INTO gateway_sessions "
                "(user_id, chat_id, session_id, updated_at) VALUES (?, ?, ?, ?)",
                (user_key, chat_key, session_id, _now()),
            )
            row = db.execute(
                "SELECT session_id FROM gateway_sessions WHERE user_id=? AND chat_id=?",
                (user_key, chat_key),
            ).fetchone()
            return str(row["session_id"])

    def reset_session(self, user_id: str, chat_id: str, platform: str = DEFAULT_PLATFORM) -> str:
        session_id = f"{platform}:{user_id}:{chat_id}:{uuid.uuid4().hex[:12]}"
        with self._connection() as db:
            db.execute(
                "INSERT INTO gateway_sessions "
                "(user_id, chat_id, session_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, chat_id) DO UPDATE SET "
                "session_id=excluded.session_id, updated_at=excluded.updated_at",
                (scope_key(platform, user_id), scope_key(platform, chat_id), session_id, _now()),
            )
        return session_id

    def set_chat_locked(
        self, user_id: str, chat_id: str, locked: bool, platform: str = DEFAULT_PLATFORM
    ) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO gateway_chat_locks "
                "(user_id, chat_id, locked, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, chat_id) DO UPDATE SET "
                "locked=excluded.locked, updated_at=excluded.updated_at",
                (
                    scope_key(platform, user_id),
                    scope_key(platform, chat_id),
                    int(locked),
                    _now(),
                ),
            )

    def is_chat_locked(self, user_id: str, chat_id: str, platform: str = DEFAULT_PLATFORM) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT locked FROM gateway_chat_locks WHERE user_id=? AND chat_id=?",
                (scope_key(platform, user_id), scope_key(platform, chat_id)),
            ).fetchone()
        return bool(row["locked"]) if row else False

    def claim_update(self, platform: str, update_id: str) -> bool:
        try:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO gateway_updates(platform, update_id, received_at) "
                    "VALUES (?, ?, ?)",
                    (platform, update_id, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        self._claims_since_prune += 1
        if self._claims_since_prune >= PRUNE_EVERY_CLAIMS:
            self.prune_updates()
        return True

    def prune_updates(self) -> int:
        self._claims_since_prune = 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.update_retention)).isoformat()
        with self._write_connection() as db:
            cursor = db.execute("DELETE FROM gateway_updates WHERE received_at < ?", (cutoff,))
            return max(cursor.rowcount, 0)

    def prune_tasks(self, older_than_days: float, *, dry_run: bool = True) -> int:
        if older_than_days <= 0:
            raise ValueError("older_than_days must be positive")
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        statuses = tuple(sorted(TERMINAL_TASK_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        where = f"status IN ({placeholders}) AND updated_at < ?"
        with self._write_connection() as db:
            if dry_run:
                row = db.execute(
                    f"SELECT COUNT(*) AS total FROM gateway_tasks WHERE {where}",
                    (*statuses, cutoff),
                ).fetchone()
                return int(row["total"])
            cursor = db.execute(f"DELETE FROM gateway_tasks WHERE {where}", (*statuses, cutoff))
            return max(cursor.rowcount, 0)

    def count_by_status(self) -> dict[str, int]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS total FROM gateway_tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def list_by_status(self, status: str, limit: int = 20) -> list[DurableTask]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM gateway_tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def interrupt(self, task_id: str, error: str) -> DurableTask:
        with self._write_connection() as db:
            row = self._locked_row(db, task_id, None)
            current = str(row["status"])
            if current not in ORPHANABLE_TASK_STATUSES:
                raise InvalidTaskTransition(f"invalid task transition: {current} -> interrupted")
            self._mark_interrupted(db, row, error)
            updated = db.execute(
                "SELECT * FROM gateway_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row(updated)

    def interrupt_orphaned(self) -> int:
        placeholders = ",".join("?" for _ in ORPHANABLE_TASK_STATUSES)
        with self._write_connection() as db:
            rows = db.execute(
                f"SELECT * FROM gateway_tasks WHERE status IN ({placeholders})",
                tuple(sorted(ORPHANABLE_TASK_STATUSES)),
            ).fetchall()
            for row in rows:
                self._mark_interrupted(db, row, ORPHAN_ERROR)
        return len(rows)

    def _mark_interrupted(self, db: sqlite3.Connection, row: sqlite3.Row, error: str) -> None:
        state = json.loads(row["state_json"])
        state["recovery"] = {
            "previous_status": str(row["status"]),
            "action": self._recovery_action(row, state).value,
        }
        db.execute(
            "UPDATE gateway_tasks SET status='interrupted', state_json=?, updated_at=?, "
            "error=? WHERE task_id=?",
            (json.dumps(state, ensure_ascii=False), _now(), error, row["task_id"]),
        )

    def pause_interrupted(self) -> int:
        return self.interrupt_orphaned()

    def recoverable(self) -> list[DurableTask]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM gateway_tasks WHERE status='interrupted' ORDER BY created_at"
            ).fetchall()
        return [
            task
            for task in (self._row(row) for row in rows)
            if self.stored_action(task.state) in SAFE_RESUME_ACTIONS
        ]

    @staticmethod
    def stored_action(state: dict[str, Any]) -> RecoveryAction:
        recovery = state.get("recovery")
        if isinstance(recovery, dict):
            try:
                return RecoveryAction(str(recovery.get("action")))
            except ValueError:
                return RecoveryAction.MANUAL_REVIEW
        return RecoveryAction.MANUAL_REVIEW

    @staticmethod
    def _recovery_action(row: sqlite3.Row, state: dict[str, Any]) -> RecoveryAction:
        run = Run(
            owner_id=str(row["owner_id"]) or "unknown",
            objective=str(row["objective"]) or "unknown",
            status=RunStatus(str(row["status"])),
        )
        checkpoint = state.get("checkpoint")
        return classify_recovery(
            run, {"state": checkpoint} if isinstance(checkpoint, dict) else None
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> DurableTask:
        keys = set(row.keys())
        return DurableTask(
            task_id=str(row["task_id"]),
            owner_id=str(row["owner_id"]),
            session_id=str(row["session_id"]),
            objective=str(row["objective"]),
            status=row["status"],
            state=json.loads(row["state_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            error=str(row["error"]),
            run_id=str(row["run_id"]) if "run_id" in keys else "",
            max_attempts=int(row["max_attempts"]) if "max_attempts" in keys else 1,
            deadline_at=str(row["deadline_at"])
            if "deadline_at" in keys and row["deadline_at"]
            else None,
        )
