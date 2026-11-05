from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from eurus.storage.database import Database, DatabasePath

MESSAGE_TYPES = frozenset(
    {
        "task_request",
        "progress_update",
        "result_delivery",
        "failure_report",
        "clarification_request",
        "cancellation_signal",
        "artifact_published",
        "review_request",
        "review_result",
        "revision_request",
        "blocker_report",
        "decision_record",
        "task_dependency_ready",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    assignment_id: str
    sender: str
    message_type: str
    body: str
    created_at: datetime


class MessageStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def record(
        self,
        assignment_id: str,
        sender: str,
        message_type: str,
        body: str,
    ) -> AgentMessage:
        if message_type not in MESSAGE_TYPES:
            raise ValueError(f"unknown message type: {message_type}")
        if not assignment_id.strip() or not sender.strip():
            raise ValueError("assignment_id and sender are required")
        current = _now()
        message = AgentMessage(
            message_id=uuid.uuid4().hex,
            assignment_id=assignment_id,
            sender=sender,
            message_type=message_type,
            body=body[:8000],
            created_at=current,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_messages(
                    message_id, assignment_id, sender, message_type, body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    message.message_id,
                    message.assignment_id,
                    message.sender,
                    message.message_type,
                    message.body,
                    _time(current),
                ),
            )
        return message

    def list_for_assignment(self, assignment_id: str, *, limit: int = 100) -> list[AgentMessage]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_messages WHERE assignment_id=? "
                "ORDER BY created_at LIMIT ?",
                (assignment_id, limit),
            ).fetchall()
        return [self._message(row) for row in rows]

    def _message(self, row: sqlite3.Row) -> AgentMessage:
        return AgentMessage(
            message_id=row["message_id"],
            assignment_id=row["assignment_id"],
            sender=row["sender"],
            message_type=row["message_type"],
            body=row["body"] or "",
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
        )


class AssignmentMailbox:
    def __init__(self, store: MessageStore) -> None:
        self.store = store

    def task_request(self, assignment_id: str, objective: str, *, parent: str) -> AgentMessage:
        return self.store.record(assignment_id, parent, "task_request", objective[:8000])

    def progress(self, assignment_id: str, sender: str, update: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "progress_update", update[:8000])

    def result(self, assignment_id: str, sender: str, result: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "result_delivery", result[:8000])

    def failure(self, assignment_id: str, sender: str, error: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "failure_report", error[:8000])

    def clarification(self, assignment_id: str, sender: str, question: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "clarification_request", question[:8000])

    def cancellation(self, assignment_id: str, sender: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "cancellation_signal", "cancelled")

    def artifact_published(self, assignment_id: str, sender: str, description: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "artifact_published", description[:8000])

    def review_request(self, assignment_id: str, sender: str, scope: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "review_request", scope[:8000])

    def review_result(self, assignment_id: str, sender: str, outcome: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "review_result", outcome[:8000])

    def revision_request(self, assignment_id: str, sender: str, findings: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "revision_request", findings[:8000])

    def blocker_report(self, assignment_id: str, sender: str, blocker: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "blocker_report", blocker[:8000])

    def decision_record(self, assignment_id: str, sender: str, decision: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "decision_record", decision[:8000])

    def dependency_ready(self, assignment_id: str, sender: str, task_key: str) -> AgentMessage:
        return self.store.record(assignment_id, sender, "task_dependency_ready", task_key[:8000])

    def conversation(self, assignment_id: str) -> Sequence[AgentMessage]:
        return self.store.list_for_assignment(assignment_id)
