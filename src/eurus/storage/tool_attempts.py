from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any

from eurus.runtime.contracts import ToolExecutionOutcome
from eurus.storage.database import Database, DatabasePath

PENDING_STATUS = "pending"
PENDING_DETAIL = "write-ahead: execution started, outcome not yet recorded"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ToolAttemptStore:
    """Durable attempt journal with write-ahead pending rows.

    Mutating tool executions first persist a ``pending`` row (uncertain) before
    the tool runs; the final outcome upserts that row in place. A crash between
    the two leaves the pending row behind, which recovery treats as an
    unresolved side effect requiring reconciliation.
    """

    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def record_pending(
        self,
        *,
        run_id: str | None,
        tool_name: str,
        call_key: str,
        attempt_id: str,
        risk_level: str = "high",
    ) -> None:
        if not attempt_id.strip():
            raise ValueError("attempt_id must be non-empty")
        if not tool_name.strip():
            raise ValueError("tool_name must be non-empty")
        now = datetime.now(UTC)
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_tool_attempts (
                    attempt_id, run_id, tool_name, call_key, status, is_error,
                    uncertain, risk_level, started_at, finished_at, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, NULL, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    status = excluded.status,
                    uncertain = 1,
                    started_at = excluded.started_at,
                    finished_at = NULL,
                    detail = excluded.detail""",
                (
                    attempt_id,
                    run_id,
                    tool_name,
                    call_key,
                    PENDING_STATUS,
                    risk_level,
                    _iso(now),
                    PENDING_DETAIL,
                    now.isoformat(),
                ),
            )

    def record(self, outcome: ToolExecutionOutcome) -> str:
        created_at = datetime.now(UTC)
        attempt_id = outcome.attempt_id or created_at.isoformat()
        finished_at = outcome.finished_at or created_at
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_tool_attempts (
                    attempt_id, run_id, tool_name, call_key, status, is_error,
                    uncertain, risk_level, started_at, finished_at, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    status = excluded.status,
                    is_error = excluded.is_error,
                    uncertain = excluded.uncertain,
                    risk_level = excluded.risk_level,
                    started_at = COALESCE(excluded.started_at, eurus_tool_attempts.started_at),
                    finished_at = excluded.finished_at,
                    detail = excluded.detail""",
                (
                    attempt_id,
                    outcome.run_id,
                    outcome.tool_name,
                    outcome.call_key,
                    outcome.status.value,
                    int(outcome.is_error),
                    int(outcome.uncertain),
                    outcome.risk_level,
                    _iso(outcome.started_at),
                    _iso(finished_at),
                    outcome.detail[:2000],
                    created_at.isoformat(),
                ),
            )
        return attempt_id

    async def record_async(self, outcome: ToolExecutionOutcome) -> str:
        return await asyncio.to_thread(self.record, outcome)

    async def record_pending_async(
        self,
        *,
        run_id: str | None,
        tool_name: str,
        call_key: str,
        attempt_id: str,
        risk_level: str = "high",
    ) -> None:
        await asyncio.to_thread(
            self.record_pending,
            run_id=run_id,
            tool_name=tool_name,
            call_key=call_key,
            attempt_id=attempt_id,
            risk_level=risk_level,
        )

    def list_for_run(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM eurus_tool_attempts WHERE run_id = ?
                ORDER BY created_at LIMIT ?""",
                (run_id, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def uncertain_keys(self, run_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM eurus_tool_attempts WHERE uncertain = 1"
            if run_id is None
            else "SELECT * FROM eurus_tool_attempts WHERE uncertain = 1 AND run_id = ?"
        )
        with self.database.connection() as connection:
            rows = connection.execute(query, (run_id,) if run_id is not None else ()).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def has_uncertain_mutation(self, run_id: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM eurus_tool_attempts WHERE run_id = ? AND uncertain = 1 LIMIT 1",
                (run_id,),
            ).fetchone()
        return row is not None

    def resolve_uncertain(self, attempt_id: str, resolution: str) -> bool:
        if not resolution.strip():
            raise ValueError("resolution must be non-empty")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_tool_attempts SET uncertain = 0, detail = ? WHERE attempt_id = ?",
                (f"resolved: {resolution}"[:2000], attempt_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "run_id": row["run_id"],
            "tool_name": row["tool_name"],
            "call_key": row["call_key"],
            "status": row["status"],
            "is_error": bool(row["is_error"]),
            "uncertain": bool(row["uncertain"]),
            "risk_level": row["risk_level"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }
