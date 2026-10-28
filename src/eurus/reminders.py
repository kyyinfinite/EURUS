from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eurus.errors import ToolError
from eurus.storage.sqlite import connect
from eurus.tools.base import Tool

log = logging.getLogger(__name__)

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
        timezone: str = "Asia/Jakarta",
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

    def cancel(self, reminder_id: str, owner_id: str) -> bool:
        with closing(self._connect()) as db:
            result = db.execute(
                "UPDATE eurus_reminders SET status='cancelled', updated_at=? "
                "WHERE reminder_id=? AND owner_id=? AND status IN ('pending','retrying')",
                (_iso(_utcnow()), reminder_id, owner_id),
            )
        return result.rowcount == 1

    def cancel_in_conversation(
        self, reminder_id: str, owner_id: str, gateway: str, conversation_id: str
    ) -> bool:
        with closing(self._connect()) as db:
            result = db.execute(
                "UPDATE eurus_reminders SET status='cancelled', updated_at=? "
                "WHERE reminder_id=? AND owner_id=? AND gateway=? AND conversation_id=? "
                "AND status IN ('pending','retrying')",
                (_iso(_utcnow()), reminder_id, owner_id, gateway, conversation_id),
            )
        return result.rowcount == 1

    def cancel_for_conversation(
        self, owner_id: str, gateway: str, conversation_id: str, *, message: str | None = None
    ) -> int:
        if not owner_id or not gateway or not conversation_id:
            return 0
        query = re.sub(r"[^\w]+", " ", message.casefold()).strip() if message else ""
        now = _iso(_utcnow())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT reminder_id, message FROM eurus_reminders "
                "WHERE owner_id=? AND gateway=? AND conversation_id=? "
                "AND status IN ('pending','retrying')",
                (owner_id, gateway, conversation_id),
            ).fetchall()
            selected = []
            for row in rows:
                stored = re.sub(r"[^\w]+", " ", row["message"].casefold()).strip()
                if not query or query in stored or stored in query:
                    selected.append(row["reminder_id"])
            if not selected:
                db.commit()
                return 0
            placeholders = ",".join("?" for _ in selected)
            result = db.execute(
                f"UPDATE eurus_reminders SET status='cancelled', updated_at=? "
                f"WHERE reminder_id IN ({placeholders}) AND owner_id=? AND gateway=? "
                "AND conversation_id=? AND status IN ('pending','retrying')",
                (now, *selected, owner_id, gateway, conversation_id),
            )
            db.commit()
            return result.rowcount

    def reschedule(
        self, reminder_id: str, owner_id: str, due_at: str, timezone: str | None = None
    ) -> bool:
        try:
            zone_name = timezone or "UTC"
            zone = ZoneInfo(zone_name)
            due = datetime.fromisoformat(due_at)
            if due.tzinfo is None:
                due = due.replace(tzinfo=zone)
            due = due.astimezone(UTC)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid due_at or IANA timezone") from exc
        if due <= _utcnow():
            raise ValueError("due_at must be in the future")
        with closing(self._connect()) as db:
            result = db.execute(
                "UPDATE eurus_reminders SET due_at=?, timezone=?, status='pending', "
                "attempts=0, last_error=NULL, updated_at=? "
                "WHERE reminder_id=? AND owner_id=? AND status IN ('pending','retrying')",
                (_iso(due), zone_name, _iso(_utcnow()), reminder_id, owner_id),
            )
        return result.rowcount == 1

    def claim_due(self, now: datetime | None = None) -> Reminder | None:
        current = _iso(now or _utcnow())
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM eurus_reminders WHERE status IN ('pending','retrying') "
                "AND due_at<=? ORDER BY due_at LIMIT 1",
                (current,),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            result = db.execute(
                "UPDATE eurus_reminders SET status='sending', attempts=attempts+1, "
                "updated_at=? WHERE reminder_id=? AND status IN ('pending','retrying')",
                (current, row["reminder_id"]),
            )
            if result.rowcount != 1:
                db.rollback()
                return None
            updated = db.execute(
                "SELECT * FROM eurus_reminders WHERE reminder_id=?",
                (row["reminder_id"],),
            ).fetchone()
            db.commit()
        return _from_row(updated) if updated else None

    def finish_delivery(
        self, reminder: Reminder, *, error: str | None = None, now: datetime | None = None
    ) -> None:
        current = now or _utcnow()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM eurus_reminders WHERE reminder_id=?",
                (reminder.reminder_id,),
            ).fetchone()
            if row is None or row["status"] != "sending":
                return
            if error is None:
                if reminder.interval_seconds:
                    due = datetime.fromisoformat(reminder.due_at) + timedelta(
                        seconds=reminder.interval_seconds
                    )
                    while due <= current:
                        due += timedelta(seconds=reminder.interval_seconds)
                    status, due_at, last_error = "pending", _iso(due), None
                else:
                    status, due_at, last_error = "completed", reminder.due_at, None
            elif int(row["attempts"]) < int(row["max_attempts"]):
                delay = min(300, 5 * (2 ** max(0, int(row["attempts"]) - 1)))
                status, due_at, last_error = (
                    "retrying",
                    _iso(current + timedelta(seconds=delay)),
                    error[:120],
                )
            else:
                status, due_at, last_error = "failed", reminder.due_at, error[:120]
            db.execute(
                "UPDATE eurus_reminders SET status=?, due_at=?, last_error=?, updated_at=? "
                "WHERE reminder_id=? AND status='sending'",
                (status, due_at, last_error, _iso(current), reminder.reminder_id),
            )

    def recover_sending(self) -> int:
        with closing(self._connect()) as db:
            result = db.execute(
                "UPDATE eurus_reminders SET status=CASE WHEN attempts < max_attempts "
                "THEN 'retrying' ELSE 'failed' END, last_error='process_interrupted', "
                "updated_at=? WHERE status='sending'",
                (_iso(_utcnow()),),
            )
        return result.rowcount


class ReminderDispatcher:
    def __init__(
        self,
        store: ReminderStore,
        deliver: Callable[[str, str], Awaitable[None]],
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        self.store, self.deliver, self.poll_seconds = store, deliver, poll_seconds
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.store.recover_sending()
        while not self._stop.is_set():
            reminder = self.store.claim_due()
            if reminder is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                continue
            try:
                if reminder.gateway != "telegram":
                    raise RuntimeError("unsupported reminder gateway")
                await self.deliver(
                    reminder.conversation_id, f"⏰ EURUS REMINDER\n\n{reminder.message}"
                )
            except asyncio.CancelledError:
                self.store.finish_delivery(reminder, error="dispatcher_cancelled")
                raise
            except Exception as exc:
                self.store.finish_delivery(reminder, error=type(exc).__name__)
                log.warning(
                    "Reminder delivery failed reminder_id=%s error_type=%s",
                    reminder.reminder_id,
                    type(exc).__name__,
                )
            else:
                self.store.finish_delivery(reminder)

    async def stop(self) -> None:
        self._stop.set()


def reminder_tools(store: ReminderStore) -> list[Tool]:
    async def create_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _reminder_context.get()
        if not ctx:
            raise ToolError("reminders can only be created from a supported gateway conversation")
        try:
            item = store.create(
                ctx["owner_id"],
                ctx["gateway"],
                ctx["conversation_id"],
                args["message"],
                due_at=args["due_at"],
                timezone=args.get("timezone")
                or os.environ.get("EURUS_REMINDER_TIMEZONE", "Asia/Jakarta"),
                interval_seconds=args.get("interval_seconds"),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        next_time = (
            datetime.fromisoformat(item.due_at)
            .astimezone(ZoneInfo(item.timezone))
            .isoformat(timespec="minutes")
        )
        repeat = f"; repeats every {item.interval_seconds} seconds" if item.interval_seconds else ""
        return (
            f"Reminder created (ID {item.reminder_id[:12]}); "
            f"next delivery {next_time} {item.timezone}{repeat}."
        )

    async def list_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _reminder_context.get()
        if not ctx:
            raise ToolError("reminder listing requires a supported gateway conversation")
        items = store.list(
            ctx["owner_id"],
            active_only=args.get("active_only", True),
            limit=args.get("limit", 50),
        )
        if not items:
            return "No reminders found."
        return "\n".join(
            f"{item.reminder_id[:12]} | {item.status} | {item.due_at} | {item.message[:180]}"
            for item in items
        )

    async def cancel_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _reminder_context.get()
        if not ctx:
            raise ToolError("reminder cancellation requires a supported gateway conversation")
        reminder_id = args.get("reminder_id")
        if reminder_id:
            changed = int(
                store.cancel_in_conversation(
                    reminder_id, ctx["owner_id"], ctx["gateway"], ctx["conversation_id"]
                )
            )
        else:
            changed = store.cancel_for_conversation(
                ctx["owner_id"],
                ctx["gateway"],
                ctx["conversation_id"],
                message=args.get("message"),
            )
        if changed == 0:
            return "No matching active reminder was found in this conversation."
        if args.get("message"):
            return f"Cancelled {changed} matching reminder(s). They will not be sent again."
        return (
            f"Cancelled {changed} active reminder(s) in this conversation. "
            "They will not be sent again."
        )

    async def reschedule_handler(args: dict[str, Any], context: Any) -> str:
        ctx = _reminder_context.get()
        if not ctx:
            raise ToolError("reminder rescheduling requires a supported gateway conversation")
        try:
            changed = store.reschedule(
                args["reminder_id"], ctx["owner_id"], args["due_at"], args.get("timezone")
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return (
            "Reminder rescheduled." if changed else "Reminder not found or no longer reschedulable."
        )

    create_description = (
        "Create a persistent reminder delivered to the current Telegram conversation. "
        "Convert the user's requested date/time to an explicit ISO-8601 timestamp. "
        "For recurring reminders set interval_seconds (minimum 60). Ask for "
        "clarification if the date/time is ambiguous. Use the configured default "
        "timezone (EURUS_REMINDER_TIMEZONE, default Asia/Jakarta)."
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
        "Cancel reminder(s) when the user says to stop reminding them, e.g. "
        "'Eurus udah jangan ingetin gue', 'stop reminding me', or 'cancel that "
        "reminder'. If a reminder_id is known, cancel that reminder. Otherwise "
        "cancel all active reminders in the current owner+gateway+conversation; "
        "if the user names a reminder message, pass it as message to cancel only "
        "matching reminders. Never cancel reminders belonging to another owner "
        "or conversation."
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
