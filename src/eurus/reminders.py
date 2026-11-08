from __future__ import annotations

import contextvars
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eurus.errors import ToolError
from eurus.owner import OWNER_ID
from eurus.scheduler import ScheduleStore, validate_deliver
from eurus.storage.migrations import migrate_owner_once
from eurus.storage.sqlite import connect
from eurus.tools.base import Tool

log = logging.getLogger(__name__)

REMINDER_NAME_PREFIX = "reminder"
REMINDER_OBJECTIVE_PREFIX = "⏰ EURUS REMINDER\n\n"
DEFAULT_TIMEZONE = "Asia/Jakarta"

_reminder_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "eurus_reminder_context", default=None
)


def set_reminder_context(
    owner_id: str, conversation_id: str, gateway: str = "telegram"
) -> contextvars.Token[dict[str, str] | None]:
    return _reminder_context.set(
        {"owner_id": owner_id, "conversation_id": conversation_id, "gateway": gateway}
    )


def reset_reminder_context(token: contextvars.Token[dict[str, str] | None]) -> None:
    _reminder_context.reset(token)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Reminder:
    reminder_id: str
    owner_id: str
    gateway: str
    conversation_id: str
    message: str
    due_at: str
    timezone: str
    interval_seconds: int | None
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


def _from_row(row: sqlite3.Row) -> Reminder:
    return Reminder(**{key: row[key] for key in Reminder.__dataclass_fields__})


class ReminderStore:
    def __init__(self, path: str | Path, *, max_attempts: int = 3) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS eurus_reminders (
                reminder_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, gateway TEXT NOT NULL,
                conversation_id TEXT NOT NULL, message TEXT NOT NULL, due_at TEXT NOT NULL,
                timezone TEXT NOT NULL, interval_seconds INTEGER, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS eurus_reminders_due ON eurus_reminders(status, due_at)"
            )
            migrate_owner_once(db, "eurus_reminders")
        self.max_attempts = max_attempts

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path, rows=True, autocommit=True)

    def create(
        self,
        owner_id: str,
        gateway: str,
        conversation_id: str,
        message: str,
        *,
        due_at: str,
        timezone: str = DEFAULT_TIMEZONE,
        interval_seconds: int | None = None,
        now: datetime | None = None,
    ) -> Reminder:
        if not owner_id or not gateway or not conversation_id:
            raise ValueError("owner, gateway, and conversation are required")
        message = message.strip()
        if not message or len(message) > 3000:
            raise ValueError("reminder message must be 1 to 3000 characters")
        try:
            zone = ZoneInfo(timezone)
            due = datetime.fromisoformat(due_at)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid due_at or IANA timezone") from exc
        if due.tzinfo is None:
            due = due.replace(tzinfo=zone)
        due = due.astimezone(UTC)
        current = now or _utcnow()
        if current.tzinfo is None or due <= current.astimezone(UTC):
            raise ValueError("due_at must be a future timezone-aware timestamp")
        if interval_seconds is not None and not 60 <= interval_seconds <= 31_536_000:
            raise ValueError("interval_seconds must be between 60 seconds and one year")
        stamp = _iso(current)
        reminder = Reminder(
            reminder_id=uuid.uuid4().hex,
            owner_id=owner_id,
            gateway=gateway,
            conversation_id=conversation_id,
            message=message,
            due_at=_iso(due),
            timezone=timezone,
            interval_seconds=interval_seconds,
            status="pending",
            attempts=0,
            max_attempts=self.max_attempts,
            last_error=None,
            created_at=stamp,
            updated_at=stamp,
        )
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO eurus_reminders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(getattr(reminder, field) for field in Reminder.__dataclass_fields__),
            )
        return reminder

    def pending(self, *, limit: int = 500) -> Sequence[Reminder]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM eurus_reminders WHERE status IN ('pending','retrying') "
                "ORDER BY due_at LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list(self, owner_id: str, *, active_only: bool = True, limit: int = 100) -> list[Reminder]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with closing(self._connect()) as db:
            if active_only:
                rows = db.execute(
                    "SELECT * FROM eurus_reminders WHERE owner_id=? "
                    "AND status IN ('pending','retrying') ORDER BY due_at LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM eurus_reminders WHERE owner_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
        return [_from_row(row) for row in rows]

    def mark_migrated(self, reminder_id: str, schedule_id: str) -> bool:
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE eurus_reminders SET status='migrated', last_error=?, updated_at=? "
                "WHERE reminder_id=? AND status IN ('pending','retrying')",
                (f"migrated:schedule:{schedule_id}", _iso(_utcnow()), reminder_id),
            )
            return cursor.rowcount == 1


def migrate_reminders_to_schedules(
    reminder_store: ReminderStore, schedule_store: ScheduleStore
) -> int:
    migrated = 0
    for reminder in reminder_store.pending():
        if reminder.gateway == "telegram" and reminder.conversation_id:
            deliver = f"telegram:{reminder.conversation_id}"
        elif reminder.gateway in ("web", "tui"):
            deliver = reminder.gateway
        else:
            deliver = "local"
        try:
            deliver = validate_deliver(deliver)
            schedule = schedule_store.create(
                OWNER_ID,
                f"{REMINDER_OBJECTIVE_PREFIX}{reminder.message}",
                kind="interval" if reminder.interval_seconds else "once",
                run_at=None if reminder.interval_seconds else reminder.due_at,
                interval_seconds=reminder.interval_seconds,
                timezone=reminder.timezone,
                name=f"{REMINDER_NAME_PREFIX}:{reminder.reminder_id[:12]}",
                deliver=deliver,
                origin_gateway=reminder.gateway,
                origin_conversation=reminder.conversation_id,
                verbatim=True,
                misfire_policy="run_once",
            )
        except ValueError as exc:
            log.warning("reminder migration skipped reminder_id=%s: %s", reminder.reminder_id, exc)
            continue
        if reminder_store.mark_migrated(reminder.reminder_id, schedule.schedule_id):
            migrated += 1
    return migrated


def reminder_tools(store: ReminderStore, schedules: ScheduleStore | None = None) -> list[Tool]:
    schedule_store = schedules if schedules is not None else _default_schedule_store()

    def _require_context() -> dict[str, str]:
        context = _reminder_context.get()
        if not context:
            raise ToolError("reminders require a supported gateway conversation")
        return context

    def _reminder_rows(owner_id: str, active_only: bool, limit: int) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for schedule in schedule_store.list_owner(owner_id, limit=limit):
            if not schedule.name.startswith(REMINDER_NAME_PREFIX):
                continue
            if active_only and not schedule.enabled:
                continue
            rows.append(
                {
                    "schedule_id": schedule.schedule_id,
                    "message": schedule.objective.removeprefix(REMINDER_OBJECTIVE_PREFIX),
                    "due_at": schedule.next_run_at,
                    "timezone": schedule.timezone,
                    "conversation": schedule.origin_conversation,
                    "enabled": "aktif" if schedule.enabled else "jeda",
                    "repeat": str(schedule.interval_seconds or ""),
                }
            )
        return rows

    async def create_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _require_context()
        message = str(args.get("message", "")).strip()
        due_at = str(args.get("due_at", "")).strip()
        timezone = str(args.get("timezone") or DEFAULT_TIMEZONE)
        interval_seconds = args.get("interval_seconds")
        if not message:
            raise ToolError("reminder message is required")
        try:
            schedule = schedule_store.create(
                OWNER_ID,
                f"{REMINDER_OBJECTIVE_PREFIX}{message}",
                kind="interval" if interval_seconds else "once",
                run_at=None if interval_seconds else due_at,
                interval_seconds=interval_seconds,
                timezone=timezone,
                name=f"{REMINDER_NAME_PREFIX}:{uuid.uuid4().hex[:12]}",
                deliver=f"telegram:{ctx['conversation_id']}"
                if ctx["gateway"] == "telegram"
                else ctx["gateway"],
                origin_gateway=ctx["gateway"],
                origin_conversation=ctx["conversation_id"],
                verbatim=True,
                misfire_policy="run_once",
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        repeat = f"; repeats every {interval_seconds} seconds" if interval_seconds else ""
        return (
            f"Reminder created (ID {schedule.schedule_id[:12]}); "
            f"next delivery {schedule.next_run_at} {schedule.timezone}{repeat}."
        )

    async def list_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _require_context()
        rows = _reminder_rows(
            OWNER_ID,
            active_only=bool(args.get("active_only", True)),
            limit=int(args.get("limit", 50)),
        )
        if not rows:
            legacy = store.list(
                ctx["owner_id"], active_only=bool(args.get("active_only", True)), limit=50
            )
            if not legacy:
                return "No reminders found."
            return "\n".join(
                f"{item.reminder_id[:12]} | {item.status} | {item.due_at} | {item.message[:180]}"
                for item in legacy
            )
        return "\n".join(
            f"{row['schedule_id'][:12]} | {row['enabled']} | {row['due_at']} "
            f"| {row['message'][:180]}"
            for row in rows
        )

    async def cancel_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _require_context()
        reminder_id = str(args.get("reminder_id", "")).strip()
        if reminder_id:
            removed = schedule_store.remove(reminder_id, OWNER_ID)
            return "Reminder cancelled." if removed else "No matching reminder was found."
        rows = _reminder_rows(OWNER_ID, active_only=True, limit=200)
        cancelled = 0
        for row in rows:
            if row["conversation"] != ctx["conversation_id"]:
                continue
            if schedule_store.remove(row["schedule_id"], OWNER_ID):
                cancelled += 1
        if cancelled == 0:
            return "No matching active reminder was found in this conversation."
        return f"Cancelled {cancelled} active reminder(s) in this conversation."

    async def reschedule_handler(args: dict[str, Any], context: Any) -> str:
        _ = context
        reminder_id = str(args.get("reminder_id", "")).strip()
        due_at = str(args.get("due_at", "")).strip()
        try:
            updated = schedule_store.update(reminder_id, OWNER_ID, run_at=due_at)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if updated is None:
            return "Reminder not found or no longer reschedulable."
        return "Reminder rescheduled."

    create_description = (
        "Create a persistent reminder delivered to the current conversation. Convert the "
        "user's requested date/time to an explicit ISO-8601 timestamp. For recurring "
        "reminders set interval_seconds (minimum 60). Ask for clarification if the "
        "date/time is ambiguous. Use the configured default timezone."
    )
    create_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1, "maxLength": 3000},
            "due_at": {
                "type": "string",
                "description": "Future ISO-8601 datetime; include timezone offset when possible.",
            },
            "timezone": {"type": "string", "description": "IANA timezone, e.g. Asia/Jakarta."},
            "interval_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000},
        },
        "required": ["message", "due_at"],
        "additionalProperties": False,
    }
    list_schema = {
        "type": "object",
        "properties": {
            "active_only": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    }
    cancel_description = (
        "Cancel reminder(s) when the user says to stop reminding them, e.g. 'stop "
        "reminding me' or 'cancel that reminder'. If a reminder_id is known, cancel that "
        "reminder. Otherwise cancel all active reminders belonging to the current "
        "conversation. Never cancel reminders of another conversation."
    )
    cancel_schema = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "string", "minLength": 8, "maxLength": 64},
            "message": {"type": "string", "minLength": 1, "maxLength": 3000},
        },
        "additionalProperties": False,
    }
    reschedule_schema = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "string", "minLength": 8, "maxLength": 64},
            "due_at": {"type": "string"},
            "timezone": {"type": "string"},
        },
        "required": ["reminder_id", "due_at"],
        "additionalProperties": False,
    }
    return [
        Tool("create_reminder", create_description, create_schema, create_handler),
        Tool(
            "list_reminders",
            "List the current user's reminders, optionally including completed and "
            "failed reminders.",
            list_schema,
            list_handler,
        ),
        Tool("cancel_reminder", cancel_description, cancel_schema, cancel_handler),
        Tool(
            "reschedule_reminder",
            "Change the time of a pending reminder owned by the current user.",
            reschedule_schema,
            reschedule_handler,
        ),
    ]


def _default_schedule_store() -> ScheduleStore:
    from eurus import paths

    return ScheduleStore(paths.database_path())


__all__ = [
    "Reminder",
    "ReminderStore",
    "migrate_reminders_to_schedules",
    "reminder_tools",
    "reset_reminder_context",
    "set_reminder_context",
]
