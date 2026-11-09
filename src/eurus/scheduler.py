from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eurus.cron import CronExpression, CronParseError
from eurus.storage.migrations import migrate_owner_once
from eurus.storage.sqlite import connect

ScheduleKind = Literal["once", "interval", "cron", "event", "chain"]
MisfirePolicy = Literal["skip", "run_once", "catch_up", "coalesce"]

DELIVER_MODES = ("origin", "local", "web", "tui")
SILENT_PREFIX = "[SILENT]"

MISFIRE_POLICIES: tuple[str, ...] = ("skip", "run_once", "catch_up", "coalesce")
SCHEDULED_BLOCKED_TOOLS = (
    "schedule",
    "goal_submit_plan",
    "goal_submit_evaluation",
    "goal_report_progress",
)
DEFAULT_MISFIRE_GRACE_SECONDS = 300
DEFAULT_CATCH_UP_LIMIT = 5
SCHEDULED_JOB_MAX_ATTEMPTS = 3
CHAIN_IDLE_NEXT_RUN_YEARS = 100
OCCURRENCE_STATUSES = frozenset(
    {"pending", "dispatched", "completed", "failed", "cancelled", "skipped", "coalesced", "unknown"}
)


class ScheduleConcurrentUpdate(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


_SCHEDULE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cron_expr", "TEXT"),
    ("trigger_event", "TEXT"),
    ("trigger_subject", "TEXT"),
    ("trigger_schedule", "TEXT"),
    ("misfire_policy", "TEXT NOT NULL DEFAULT 'coalesce'"),
    ("catch_up_limit", "INTEGER NOT NULL DEFAULT 5"),
    ("max_occurrences", "INTEGER"),
    ("occurrences_count", "INTEGER NOT NULL DEFAULT 0"),
    ("concurrency_limit", "INTEGER NOT NULL DEFAULT 1"),
    ("priority", "INTEGER NOT NULL DEFAULT 5"),
    ("expires_at", "TEXT"),
    ("execution_window_start", "TEXT"),
    ("execution_window_end", "TEXT"),
    ("revision", "INTEGER NOT NULL DEFAULT 1"),
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("deliver", "TEXT NOT NULL DEFAULT 'local'"),
    ("origin_gateway", "TEXT NOT NULL DEFAULT ''"),
    ("origin_conversation", "TEXT NOT NULL DEFAULT ''"),
    ("skills_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("pre_script", "TEXT NOT NULL DEFAULT ''"),
    ("silent", "INTEGER NOT NULL DEFAULT 0"),
    ("verbatim", "INTEGER NOT NULL DEFAULT 0"),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def validate_deliver(value: str) -> str:
    text = str(value or "local").strip()
    if text in DELIVER_MODES:
        return text
    if text.startswith("telegram:") and len(text) > len("telegram:"):
        target = text.split(":", 1)[1]
        if target.isdigit():
            return text
    raise ValueError("deliver must be origin, local, web, tui, or telegram:<chat_id>")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str, zone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("run time must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def _parse_stored(value: str | None) -> datetime | None:
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
class TaskProfile:
    name: str
    timezone: str = "UTC"
    max_runtime_seconds: int = 1800
    max_steps: int = 20
    notifications: bool = True
    allowed_tools: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name is required")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA timezone") from exc
        if not 1 <= self.max_runtime_seconds <= 86400:
            raise ValueError("max_runtime_seconds must be between 1 and 86400")
        if not 1 <= self.max_steps <= 1000:
            raise ValueError("max_steps must be between 1 and 1000")
        if any(not item or len(item) > 128 for item in self.allowed_tools):
            raise ValueError("invalid allowed tool name")


@dataclass(frozen=True, slots=True)
class Schedule:
    schedule_id: str
    owner_id: str
    objective: str
    kind: ScheduleKind
    timezone: str
    next_run_at: str
    interval_seconds: int | None
    enabled: bool
    profile: dict[str, Any]
    created_at: str
    updated_at: str
    cron_expr: str | None = None
    trigger_event: str | None = None
    trigger_subject: str | None = None
    trigger_schedule: str | None = None
    misfire_policy: str = "coalesce"
    catch_up_limit: int = DEFAULT_CATCH_UP_LIMIT
    max_occurrences: int | None = None
    occurrences_count: int = 0
    concurrency_limit: int = 1
    priority: int = 5
    expires_at: str | None = None
    execution_window_start: str | None = None
    execution_window_end: str | None = None
    revision: int = 1
    name: str = ""
    deliver: str = "local"
    origin_gateway: str = ""
    origin_conversation: str = ""
    skills: tuple[str, ...] = ()
    pre_script: str = ""
    silent: bool = False
    verbatim: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleRun:
    run_id: str
    schedule_id: str
    scheduled_at: str
    status: str
    started_at: str
    finished_at: str | None
    result: str


@dataclass(frozen=True, slots=True)
class Occurrence:
    occurrence_id: str
    schedule_id: str
    scheduled_at: str
    status: str
    job_id: str | None
    attempts: int
    error: str | None
    dispatched_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str

    @property
    def is_dispatched(self) -> bool:
        return self.status == "dispatched"


@dataclass(frozen=True, slots=True)
class ScheduleRevision:
    revision: int
    schedule_id: str
    definition_json: str
    created_at: str


class ScheduleStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS eurus_schedules (
                    schedule_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    objective TEXT NOT NULL, kind TEXT NOT NULL, timezone TEXT NOT NULL,
                    next_run_at TEXT NOT NULL, interval_seconds INTEGER,
                    enabled INTEGER NOT NULL, profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS eurus_schedules_due
                    ON eurus_schedules(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS eurus_schedule_runs (
                    run_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, result TEXT NOT NULL DEFAULT '',
                    UNIQUE(schedule_id, scheduled_at),
                    FOREIGN KEY(schedule_id) REFERENCES eurus_schedules(schedule_id)
                );
                CREATE INDEX IF NOT EXISTS eurus_schedule_runs_schedule
                    ON eurus_schedule_runs(schedule_id, started_at);
                CREATE TABLE IF NOT EXISTS eurus_task_profiles (
                    owner_id TEXT NOT NULL, name TEXT NOT NULL, profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(owner_id, name)
                );
                CREATE TABLE IF NOT EXISTS eurus_schedule_occurrences (
                    occurrence_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    job_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                    dispatched_at TEXT, finished_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(schedule_id, scheduled_at)
                );
                CREATE INDEX IF NOT EXISTS eurus_schedule_occurrences_due
                    ON eurus_schedule_occurrences(status, scheduled_at);
                CREATE TABLE IF NOT EXISTS eurus_schedule_revisions (
                    schedule_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    definition_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (schedule_id, revision)
                );
                """
            )
            existing = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(eurus_schedules)").fetchall()
            }
            for column, declaration in _SCHEDULE_COLUMNS:
                if column not in existing:
                    db.execute(f"ALTER TABLE eurus_schedules ADD COLUMN {column} {declaration}")
            migrate_owner_once(db, "eurus_schedules")
            migrate_owner_once(db, "eurus_task_profiles")

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path, rows=True, foreign_keys=True, autocommit=True)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def save_profile(self, owner_id: str, profile: TaskProfile) -> None:
        profile.validate()
        payload = json.dumps(
            {
                "timezone": profile.timezone,
                "max_runtime_seconds": profile.max_runtime_seconds,
                "max_steps": profile.max_steps,
                "notifications": profile.notifications,
                "allowed_tools": list(profile.allowed_tools),
            }
        )
        with self._connection() as db:
            db.execute(
                "INSERT INTO eurus_task_profiles VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_id, name) DO UPDATE SET "
                "profile_json=excluded.profile_json, updated_at=excluded.updated_at",
                (owner_id, profile.name, payload, _iso(_utcnow())),
            )

    def get_profile(self, owner_id: str, name: str) -> TaskProfile | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT profile_json FROM eurus_task_profiles WHERE owner_id=? AND name=?",
                (owner_id, name),
            ).fetchone()
        if row is None:
            return None
        values = json.loads(row["profile_json"])
        values["allowed_tools"] = tuple(values.get("allowed_tools", ()))
        profile = TaskProfile(name=name, **values)
        profile.validate()
        return profile

    def create(
        self,
        owner_id: str,
        objective: str,
        *,
        kind: ScheduleKind,
        run_at: str | None = None,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        trigger_event: str | None = None,
        trigger_subject: str | None = None,
        trigger_schedule: str | None = None,
        timezone: str = "UTC",
        profile: TaskProfile | None = None,
        misfire_policy: MisfirePolicy | str | None = None,
        catch_up_limit: int = DEFAULT_CATCH_UP_LIMIT,
        max_occurrences: int | None = None,
        concurrency_limit: int = 1,
        priority: int = 5,
        expires_at: str | None = None,
        execution_window_start: str | None = None,
        execution_window_end: str | None = None,
        name: str = "",
        deliver: str = "local",
        origin_gateway: str = "",
        origin_conversation: str = "",
        skills: Sequence[str] = (),
        pre_script: str = "",
        silent: bool = False,
        verbatim: bool = False,
        now: datetime | None = None,
    ) -> Schedule:
        if not owner_id.strip() or not objective.strip():
            raise ValueError("owner and objective are required")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA timezone") from exc
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        if misfire_policy is None:
            misfire_policy = "run_once" if kind in ("once", "event") else "coalesce"
        if misfire_policy not in MISFIRE_POLICIES:
            raise ValueError("misfire_policy must be one of: " + ", ".join(MISFIRE_POLICIES))
        if not 1 <= catch_up_limit <= 100:
            raise ValueError("catch_up_limit must be between 1 and 100")
        if not 1 <= concurrency_limit <= 64:
            raise ValueError("concurrency_limit must be between 1 and 64")
        if not 1 <= priority <= 9:
            raise ValueError("priority must be between 1 and 9")
        if max_occurrences is not None and max_occurrences < 1:
            raise ValueError("max_occurrences must be positive when provided")
        if (execution_window_start is None) != (execution_window_end is None):
            raise ValueError("execution window requires both start and end")
        _validate_window(execution_window_start)
        _validate_window(execution_window_end)
        deliver_text = validate_deliver(deliver)
        skill_names = tuple(
            dict.fromkeys(str(item).strip() for item in skills if str(item).strip())
        )
        if len(pre_script) > 8000:
            raise ValueError("pre_script must not exceed 8000 characters")
        interval: int | None = None
        next_run: datetime
        if kind == "once":
            if run_at is None:
                raise ValueError("one-time schedule requires run_at")
            next_run = _parse_time(run_at, zone)
            if next_run <= current.astimezone(UTC):
                raise ValueError("run_at must be in the future")
        elif kind == "interval":
            if interval_seconds is None or not 60 <= interval_seconds <= 31_536_000:
                raise ValueError("interval must be between 60 seconds and one year")
            interval = interval_seconds
            next_run = current.astimezone(UTC) + timedelta(seconds=interval)
        elif kind == "cron":
            if cron_expr is None:
                raise ValueError("cron schedule requires cron_expr")
            try:
                expression = CronExpression.parse(cron_expr)
            except CronParseError as exc:
                raise ValueError(f"invalid cron expression: {exc}") from exc
            next_run = expression.next_after(current, zone=zone)
        elif kind == "event":
            if trigger_event is None or not trigger_event.strip():
                raise ValueError("event schedule requires trigger_event")
            next_run = current.astimezone(UTC)
        elif kind == "chain":
            if trigger_schedule is None or not trigger_schedule.strip():
                raise ValueError("chain schedule requires trigger_schedule")
            dependency = self.get(trigger_schedule.strip(), owner_id=owner_id)
            if dependency is None:
                raise ValueError(
                    "trigger_schedule must reference an existing schedule of the same owner"
                )
            next_run = current.astimezone(UTC) + timedelta(days=365 * CHAIN_IDLE_NEXT_RUN_YEARS)
        else:
            raise ValueError("unsupported schedule kind")
        expires_text: str | None = None
        if expires_at is not None:
            expires = _parse_time(expires_at, zone)
            if expires <= current:
                raise ValueError("expires_at must be in the future")
            expires_text = _iso(expires)
        selected_profile = profile or TaskProfile(name="default", timezone=timezone)
        selected_profile.validate()
        now_text = _iso(current)
        schedule = Schedule(
            uuid.uuid4().hex,
            owner_id,
            objective,
            kind,
            timezone,
            _iso(next_run),
            interval,
            True,
            {
                "name": selected_profile.name,
                "timezone": selected_profile.timezone,
                "max_runtime_seconds": selected_profile.max_runtime_seconds,
                "max_steps": selected_profile.max_steps,
                "notifications": selected_profile.notifications,
                "allowed_tools": list(selected_profile.allowed_tools),
            },
            now_text,
            now_text,
            cron_expr=cron_expr if kind == "cron" else None,
            trigger_event=trigger_event if kind == "event" else None,
            trigger_subject=trigger_subject if kind == "event" else None,
            trigger_schedule=trigger_schedule if kind == "chain" else None,
            misfire_policy=misfire_policy,
            catch_up_limit=catch_up_limit,
            max_occurrences=max_occurrences,
            occurrences_count=0,
            concurrency_limit=concurrency_limit,
            priority=priority,
            expires_at=expires_text,
            execution_window_start=execution_window_start,
            execution_window_end=execution_window_end,
            revision=1,
            name=name.strip()[:200],
            deliver=deliver_text,
            origin_gateway=origin_gateway.strip()[:40],
            origin_conversation=origin_conversation.strip()[:200],
            skills=skill_names,
            pre_script=pre_script,
            silent=bool(silent),
            verbatim=bool(verbatim),
        )
        with self._connection() as db:
            db.execute(
                "INSERT INTO eurus_schedules ("
                "schedule_id, owner_id, objective, kind, timezone, next_run_at, "
                "interval_seconds, enabled, profile_json, created_at, updated_at, "
                "cron_expr, trigger_event, trigger_subject, trigger_schedule, misfire_policy, "
                "catch_up_limit, max_occurrences, occurrences_count, concurrency_limit, "
                "priority, expires_at, execution_window_start, execution_window_end, revision, "
                "name, deliver, origin_gateway, origin_conversation, skills_json, pre_script, "
                "silent, verbatim"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _schedule_values(schedule),
            )
            self._store_revision(db, schedule)
        return schedule

    def get(self, schedule_id: str, *, owner_id: str | None = None) -> Schedule | None:
        with self._connection() as db:
            if owner_id is None:
                row = db.execute(
                    "SELECT * FROM eurus_schedules WHERE schedule_id=?", (schedule_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM eurus_schedules WHERE schedule_id=? AND owner_id=?",
                    (schedule_id, owner_id),
                ).fetchone()
        return self._schedule(row) if row else None

    def list_owner(self, owner_id: str, limit: int = 100) -> list[Schedule]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM eurus_schedules WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [self._schedule(row) for row in rows]

    def set_enabled(self, schedule_id: str, owner_id: str, enabled: bool) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE eurus_schedules SET enabled=?, updated_at=? "
                "WHERE schedule_id=? AND owner_id=?",
                (int(enabled), _iso(_utcnow()), schedule_id, owner_id),
            )
            return cursor.rowcount == 1

    def pause(self, schedule_id: str, owner_id: str) -> bool:
        return self.set_enabled(schedule_id, owner_id, enabled=False)

    def resume(self, schedule_id: str, owner_id: str) -> bool:
        return self.set_enabled(schedule_id, owner_id, enabled=True)

    def remove(self, schedule_id: str, owner_id: str) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM eurus_schedules WHERE schedule_id=? AND owner_id=?",
                (schedule_id, owner_id),
            ).fetchone()
            if row is None:
                db.commit()
                return False
            db.execute("DELETE FROM eurus_schedule_runs WHERE schedule_id=?", (schedule_id,))
            db.execute("DELETE FROM eurus_schedule_occurrences WHERE schedule_id=?", (schedule_id,))
            db.execute("DELETE FROM eurus_schedule_revisions WHERE schedule_id=?", (schedule_id,))
            cursor = db.execute(
                "DELETE FROM eurus_schedules WHERE schedule_id=? AND owner_id=?",
                (schedule_id, owner_id),
            )
            db.commit()
            return cursor.rowcount == 1

    def update(
        self,
        schedule_id: str,
        owner_id: str,
        *,
        objective: str | None = None,
        cron_expr: str | None = None,
        interval_seconds: int | None = None,
        misfire_policy: str | None = None,
        catch_up_limit: int | None = None,
        max_occurrences: int | None = None,
        concurrency_limit: int | None = None,
        priority: int | None = None,
        execution_window_start: str | None = None,
        execution_window_end: str | None = None,
        expires_at: str | None = None,
        name: str | None = None,
        deliver: str | None = None,
        skills: Sequence[str] | None = None,
        pre_script: str | None = None,
        silent: bool | None = None,
        verbatim: bool | None = None,
        run_at: str | None = None,
    ) -> Schedule | None:
        current = self.get(schedule_id, owner_id=owner_id)
        if current is None:
            return None
        zone = ZoneInfo(current.timezone)
        updated = current
        if objective is not None and objective.strip():
            updated = _replace_field(updated, "objective", objective.strip())
        if cron_expr is not None:
            if current.kind != "cron":
                raise ValueError("cron_expr can only be updated on cron schedules")
            try:
                expression = CronExpression.parse(cron_expr)
            except CronParseError as exc:
                raise ValueError(f"invalid cron expression: {exc}") from exc
            updated = _replace_field(updated, "cron_expr", cron_expr)
            updated = _replace_field(
                updated, "next_run_at", _iso(expression.next_after(_utcnow(), zone=zone))
            )
        if interval_seconds is not None:
            if current.kind != "interval":
                raise ValueError("interval_seconds can only be updated on interval schedules")
            if not 60 <= interval_seconds <= 31_536_000:
                raise ValueError("interval must be between 60 seconds and one year")
            updated = _replace_field(updated, "interval_seconds", interval_seconds)
        if misfire_policy is not None:
            if misfire_policy not in MISFIRE_POLICIES:
                raise ValueError("unknown misfire policy")
            updated = _replace_field(updated, "misfire_policy", misfire_policy)
        if catch_up_limit is not None:
            if not 1 <= catch_up_limit <= 100:
                raise ValueError("catch_up_limit must be between 1 and 100")
            updated = _replace_field(updated, "catch_up_limit", catch_up_limit)
        if max_occurrences is not None:
            if max_occurrences < 1:
                raise ValueError("max_occurrences must be positive")
            updated = _replace_field(updated, "max_occurrences", max_occurrences)
        if concurrency_limit is not None:
            if not 1 <= concurrency_limit <= 64:
                raise ValueError("concurrency_limit must be between 1 and 64")
            updated = _replace_field(updated, "concurrency_limit", concurrency_limit)
        if priority is not None:
            if not 1 <= priority <= 9:
                raise ValueError("priority must be between 1 and 9")
            updated = _replace_field(updated, "priority", priority)
        if execution_window_start is not None or execution_window_end is not None:
            start = execution_window_start or current.execution_window_start
            end = execution_window_end or current.execution_window_end
            _validate_window(start)
            _validate_window(end)
            updated = _replace_field(updated, "execution_window_start", start)
            updated = _replace_field(updated, "execution_window_end", end)
        if expires_at is not None:
            expires = _parse_time(expires_at, zone)
            updated = _replace_field(updated, "expires_at", _iso(expires))
        if name is not None:
            updated = _replace_field(updated, "name", name.strip()[:200])
        if deliver is not None:
            updated = _replace_field(updated, "deliver", validate_deliver(deliver))
        if skills is not None:
            skill_names = tuple(
                dict.fromkeys(str(item).strip() for item in skills if str(item).strip())
            )
            updated = _replace_field(updated, "skills", skill_names)
        if pre_script is not None:
            if len(pre_script) > 8000:
                raise ValueError("pre_script must not exceed 8000 characters")
            updated = _replace_field(updated, "pre_script", pre_script)
        if silent is not None:
            updated = _replace_field(updated, "silent", bool(silent))
        if verbatim is not None:
            updated = _replace_field(updated, "verbatim", bool(verbatim))
        if run_at is not None:
            if current.kind not in ("once", "interval"):
                raise ValueError("run_at can only be updated on once or interval schedules")
            next_run = _parse_time(run_at, zone)
            if next_run <= _utcnow():
                raise ValueError("run_at must be in the future")
            updated = _replace_field(updated, "next_run_at", _iso(next_run))
            updated = _replace_field(updated, "enabled", True)
        revision = current.revision + 1
        updated = _replace_field(updated, "revision", revision)
        updated = _replace_field(updated, "updated_at", _iso(_utcnow()))
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE eurus_schedules SET next_run_at=?, interval_seconds=?, objective=?, "
                "cron_expr=?, misfire_policy=?, catch_up_limit=?, max_occurrences=?, "
                "concurrency_limit=?, priority=?, expires_at=?, execution_window_start=?, "
                "execution_window_end=?, revision=?, updated_at=?, name=?, deliver=?, "
                "skills_json=?, pre_script=?, silent=?, verbatim=? WHERE schedule_id=? "
                "AND owner_id=? AND revision=?",
                (
                    updated.next_run_at,
                    updated.interval_seconds,
                    updated.objective,
                    updated.cron_expr,
                    updated.misfire_policy,
                    updated.catch_up_limit,
                    updated.max_occurrences,
                    updated.concurrency_limit,
                    updated.priority,
                    updated.expires_at,
                    updated.execution_window_start,
                    updated.execution_window_end,
                    updated.revision,
                    updated.updated_at,
                    updated.name,
                    updated.deliver,
                    json.dumps(list(updated.skills), ensure_ascii=False),
                    updated.pre_script,
                    int(updated.silent),
                    int(updated.verbatim),
                    schedule_id,
                    owner_id,
                    current.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ScheduleConcurrentUpdate(
                    f"schedule {schedule_id} was modified concurrently; reload and retry"
                )
            self._store_revision(db, updated)
        return updated

    def revisions(self, schedule_id: str, *, limit: int = 20) -> list[ScheduleRevision]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM eurus_schedule_revisions WHERE schedule_id=? "
                "ORDER BY revision DESC LIMIT ?",
                (schedule_id, limit),
            ).fetchall()
        return [
            ScheduleRevision(
                int(row["revision"]),
                str(row["schedule_id"]),
                str(row["definition_json"]),
                str(row["created_at"]),
            )
            for row in rows
        ]

    def trigger_now(
        self,
        schedule_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        current = self.get(schedule_id, owner_id=owner_id)
        if current is None:
            return None
        moment = now or _utcnow()
        moment_text = _iso(moment)
        occurrence_id = uuid.uuid4().hex
        with self._connection() as db:
            try:
                db.execute(
                    "INSERT INTO eurus_schedule_occurrences ("
                    "occurrence_id, schedule_id, scheduled_at, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'pending', ?, ?)",
                    (occurrence_id, schedule_id, moment_text, moment_text, moment_text),
                )
            except sqlite3.IntegrityError:
                return None
            row = db.execute(
                "SELECT * FROM eurus_schedule_occurrences WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
        if row is None:
            return None
        return self._dispatch_single(schedule_id, _occurrence(row), moment)

    def occurrences(self, schedule_id: str, *, limit: int = 50) -> list[Occurrence]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM eurus_schedule_occurrences WHERE schedule_id=? "
                "ORDER BY scheduled_at DESC LIMIT ?",
                (schedule_id, limit),
            ).fetchall()
        return [_occurrence(row) for row in rows]

    def pending_occurrences(
        self, *, owner_id: str | None = None, limit: int = 100
    ) -> list[Occurrence]:
        with self._connection() as db:
            if owner_id is None:
                rows = db.execute(
                    "SELECT * FROM eurus_schedule_occurrences WHERE status='pending' "
                    "ORDER BY scheduled_at LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT o.* FROM eurus_schedule_occurrences o JOIN eurus_schedules s "
                    "ON o.schedule_id=s.schedule_id WHERE o.status='pending' AND s.owner_id=? "
                    "ORDER BY o.scheduled_at LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
        return [_occurrence(row) for row in rows]

    def claim_due(
        self, *, now: datetime | None = None, limit: int = 20, owner_id: str | None = None
    ) -> list[ScheduleRun]:
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        now_text = _iso(current)
        claimed: list[ScheduleRun] = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM eurus_schedules WHERE enabled=1 AND next_run_at<=?"
            args: tuple[Any, ...] = (now_text,)
            if owner_id is not None:
                query += " AND owner_id=?"
                args += (owner_id,)
            query += " ORDER BY next_run_at LIMIT ?"
            rows = db.execute(query, args + (limit,)).fetchall()
            for row in rows:
                scheduled_at = str(row["next_run_at"])
                run_id = uuid.uuid4().hex
                started = now_text
                try:
                    db.execute(
                        "INSERT INTO eurus_schedule_runs "
                        "(run_id, schedule_id, scheduled_at, status, started_at) "
                        "VALUES (?, ?, ?, 'running', ?)",
                        (run_id, row["schedule_id"], scheduled_at, started),
                    )
                except sqlite3.IntegrityError:
                    continue
                next_run = None
                if row["kind"] == "interval":
                    interval = int(row["interval_seconds"])
                    scheduled = datetime.fromisoformat(scheduled_at)
                    next_run = _iso(scheduled + timedelta(seconds=interval))
                    if datetime.fromisoformat(next_run) <= current.astimezone(UTC):
                        next_run = _iso(current.astimezone(UTC) + timedelta(seconds=interval))
                db.execute(
                    "UPDATE eurus_schedules SET enabled=?, next_run_at=COALESCE(?, next_run_at), "
                    "updated_at=? WHERE schedule_id=?",
                    (int(row["kind"] != "once"), next_run, now_text, row["schedule_id"]),
                )
                claimed.append(
                    ScheduleRun(
                        run_id, str(row["schedule_id"]), scheduled_at, "running", started, None, ""
                    )
                )
            db.commit()
        return claimed

    def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
        owner_id: str | None = None,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> list[str]:
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must not be negative")
        now_text = _iso(current)
        job_ids: list[str] = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM eurus_schedules WHERE enabled=1 AND next_run_at<=?"
            values: tuple[Any, ...] = (now_text,)
            if owner_id is not None:
                query += " AND owner_id=?"
                values += (owner_id,)
            query += " ORDER BY priority, next_run_at LIMIT ?"
            rows = db.execute(query, values + (limit,)).fetchall()
            for row in rows:
                schedule = self._schedule(row)
                if self._is_expired(schedule, current):
                    self._disable_schedule(db, schedule, now_text)
                    self._skip_pending(db, schedule, "expired", now_text)
                    continue
                if not self._within_window(schedule, current):
                    continue
                if self._concurrency_blocked(db, schedule):
                    continue
                due_times = _due_occurrence_times(schedule, current)
                if not due_times:
                    continue
                for moment in due_times:
                    try:
                        db.execute(
                            "INSERT INTO eurus_schedule_occurrences ("
                            "occurrence_id, schedule_id, scheduled_at, status, created_at, "
                            "updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                            (
                                uuid.uuid4().hex,
                                schedule.schedule_id,
                                _iso(moment),
                                now_text,
                                now_text,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                pending = db.execute(
                    "SELECT * FROM eurus_schedule_occurrences WHERE schedule_id=? "
                    "AND status='pending' AND scheduled_at<=? ORDER BY scheduled_at",
                    (schedule.schedule_id, now_text),
                ).fetchall()
                if not pending:
                    continue
                selected = self._select_for_policy(
                    schedule,
                    [_occurrence(item) for item in pending],
                    current,
                    misfire_grace_seconds,
                )
                for occurrence, action in selected:
                    if action == "skip":
                        db.execute(
                            "UPDATE eurus_schedule_occurrences SET status='skipped', "
                            "error='misfire', finished_at=?, updated_at=? WHERE occurrence_id=?",
                            (now_text, now_text, occurrence.occurrence_id),
                        )
                        continue
                    if action == "coalesce":
                        db.execute(
                            "UPDATE eurus_schedule_occurrences SET status='coalesced', "
                            "updated_at=? WHERE occurrence_id=?",
                            (now_text, occurrence.occurrence_id),
                        )
                        continue
                    job_id = self._create_job_for(db, schedule, occurrence, now_text)
                    if job_id is None:
                        continue
                    job_ids.append(job_id)
                advanced = self._advance_next_run(db, schedule, current)
                if advanced is not None:
                    db.execute(
                        "UPDATE eurus_schedules SET next_run_at=?, occurrences_count=?, "
                        "enabled=?, updated_at=? WHERE schedule_id=?",
                        (
                            advanced.next_run_at,
                            advanced.occurrences_count,
                            int(advanced.enabled),
                            now_text,
                            schedule.schedule_id,
                        ),
                    )
            db.commit()
        return job_ids

    def trigger_event_schedules(
        self,
        event_type: str,
        *,
        subject_id: str = "",
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        current = now or _utcnow()
        current_text = _iso(current)
        job_ids: list[str] = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT * FROM eurus_schedules WHERE enabled=1 AND kind='event' AND trigger_event=?"
            )
            values: tuple[Any, ...] = (event_type,)
            if owner_id is not None:
                query += " AND owner_id=?"
                values += (owner_id,)
            rows = db.execute(query, values).fetchall()
            for row in rows:
                schedule = self._schedule(row)
                if schedule.trigger_subject and schedule.trigger_subject != subject_id:
                    continue
                if self._is_expired(schedule, current):
                    self._disable_schedule(db, schedule, current_text)
                    continue
                if self._concurrency_blocked(db, schedule):
                    continue
                occurrence_id = uuid.uuid4().hex
                try:
                    db.execute(
                        "INSERT INTO eurus_schedule_occurrences ("
                        "occurrence_id, schedule_id, scheduled_at, status, created_at, updated_at"
                        ") VALUES (?, ?, ?, 'pending', ?, ?)",
                        (
                            occurrence_id,
                            schedule.schedule_id,
                            current_text,
                            current_text,
                            current_text,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                occurrence = _occurrence(
                    db.execute(
                        "SELECT * FROM eurus_schedule_occurrences WHERE occurrence_id=?",
                        (occurrence_id,),
                    ).fetchone()
                )
                job_id = self._create_job_for(db, schedule, occurrence, current_text)
                if job_id is not None:
                    job_ids.append(job_id)
                    db.execute(
                        "UPDATE eurus_schedules SET occurrences_count=occurrences_count+1, "
                        "updated_at=? WHERE schedule_id=?",
                        (current_text, schedule.schedule_id),
                    )
            db.commit()
        return job_ids

    def mark_run_started(self, run_id: str, *, now: datetime | None = None) -> bool:
        current = _iso(now or _utcnow())
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE eurus_schedule_runs SET status='running', started_at=? "
                "WHERE run_id=? AND status='queued'",
                (current, run_id),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "UPDATE eurus_schedule_occurrences SET status='dispatched' "
                    "WHERE job_id=? AND status='pending'",
                    (run_id,),
                )
            return cursor.rowcount == 1

    def finish_run(self, run_id: str, *, status: str, result: str = "") -> bool:
        if status not in {
            "completed",
            "failed",
            "waiting_input",
            "waiting_approval",
            "skipped",
            "cancelled",
            "unknown_outcome",
        }:
            raise ValueError("invalid schedule run status")
        updated = False
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE eurus_schedule_runs SET status=?, finished_at=?, result=? "
                "WHERE run_id=? AND status IN ('queued', 'running')",
                (status, _iso(_utcnow()), result[:4000], run_id),
            )
            updated = cursor.rowcount == 1
            occurrence_status = {
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "unknown_outcome": "unknown",
                "waiting_input": "dispatched",
                "waiting_approval": "dispatched",
                "skipped": "skipped",
            }.get(status)
            if occurrence_status is not None:
                db.execute(
                    "UPDATE eurus_schedule_occurrences SET status=?, finished_at=?, "
                    "error=?, updated_at=? WHERE job_id=? AND status IN ('pending', 'dispatched')",
                    (
                        occurrence_status,
                        _iso(_utcnow()),
                        result[:1000] if status in {"failed", "unknown_outcome"} else None,
                        _iso(_utcnow()),
                        run_id,
                    ),
                )
            if updated and status == "completed":
                row = db.execute(
                    "SELECT schedule_id FROM eurus_schedule_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is not None:
                    self._process_chain_triggers(db, str(row["schedule_id"]))
        return updated

    def _process_chain_triggers(self, db: sqlite3.Connection, finished_schedule_id: str) -> int:
        current = _utcnow()
        now_text = _iso(current)
        rows = db.execute(
            "SELECT * FROM eurus_schedules WHERE enabled=1 AND kind='chain' AND trigger_schedule=?",
            (finished_schedule_id,),
        ).fetchall()
        triggered = 0
        for row in rows:
            schedule = self._schedule(row)
            if self._is_expired(schedule, current):
                self._disable_schedule(db, schedule, now_text)
                continue
            if self._concurrency_blocked(db, schedule):
                continue
            occurrence_id = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO eurus_schedule_occurrences ("
                    "occurrence_id, schedule_id, scheduled_at, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'pending', ?, ?)",
                    (occurrence_id, schedule.schedule_id, now_text, now_text, now_text),
                )
            except sqlite3.IntegrityError:
                continue
            occurrence = _occurrence(
                db.execute(
                    "SELECT * FROM eurus_schedule_occurrences WHERE occurrence_id=?",
                    (occurrence_id,),
                ).fetchone()
            )
            job_id = self._create_job_for(db, schedule, occurrence, now_text)
            if job_id is None:
                continue
            db.execute(
                "UPDATE eurus_schedules SET occurrences_count=occurrences_count+1, "
                "updated_at=? WHERE schedule_id=?",
                (now_text, schedule.schedule_id),
            )
            triggered += 1
        return triggered

    def finalize_occurrence(self, job_id: str, status: str, detail: str = "") -> bool:
        mapped = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "unknown_outcome": "unknown",
        }.get(status)
        if mapped is None:
            return False
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE eurus_schedule_occurrences SET status=?, finished_at=?, error=?, "
                "updated_at=? WHERE job_id=? AND status IN ('pending', 'dispatched')",
                (mapped, _iso(_utcnow()), detail[:1000] or None, _iso(_utcnow()), job_id),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "UPDATE eurus_schedule_runs SET status=?, finished_at=?, result=? "
                    "WHERE run_id=? AND status IN ('queued', 'running')",
                    (status, _iso(_utcnow()), detail[:4000], job_id),
                )
            return cursor.rowcount == 1

    def history(self, schedule_id: str, *, limit: int = 20) -> list[ScheduleRun]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM eurus_schedule_runs WHERE schedule_id=? "
                "ORDER BY started_at DESC LIMIT ?",
                (schedule_id, limit),
            ).fetchall()
        return [
            ScheduleRun(
                str(row["run_id"]),
                str(row["schedule_id"]),
                str(row["scheduled_at"]),
                str(row["status"]),
                str(row["started_at"]),
                row["finished_at"],
                str(row["result"]),
            )
            for row in rows
        ]

    def _dispatch_single(
        self, schedule_id: str, occurrence: Occurrence, now: datetime
    ) -> str | None:
        schedule = self.get(schedule_id)
        if schedule is None:
            return None
        with self._connection() as db:
            job_id = self._create_job_for(db, schedule, occurrence, _iso(now))
            if job_id is not None:
                db.execute(
                    "UPDATE eurus_schedules SET occurrences_count=occurrences_count+1, "
                    "updated_at=? WHERE schedule_id=?",
                    (_iso(now), schedule_id),
                )
        return job_id

    def _create_job_for(
        self, db: sqlite3.Connection, schedule: Schedule, occurrence: Occurrence, now_text: str
    ) -> str | None:
        job_id = uuid.uuid4().hex
        payload = json.dumps(
            {
                "profile": schedule.profile,
                "timezone": schedule.timezone,
                "occurrence_id": occurrence.occurrence_id,
                "schedule_name": schedule.name,
                "deliver": schedule.deliver,
                "origin_gateway": schedule.origin_gateway,
                "origin_conversation": schedule.origin_conversation,
                "skills": list(schedule.skills),
                "pre_script": schedule.pre_script,
                "silent": schedule.silent,
                "verbatim": schedule.verbatim,
                "blocked_tools": SCHEDULED_BLOCKED_TOOLS,
                "run_mode": "scheduled",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cursor = db.execute(
            """INSERT OR IGNORE INTO eurus_jobs(
                job_id, schedule_id, scheduled_at, owner_id, objective, payload_json,
                status, attempts, max_attempts, available_at, lease_version,
                side_effect_started, cancel_requested, result, created_at, updated_at,
                priority, source, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, 0, 0, 0, '', ?, ?, ?, 'schedule', ?)""",
            (
                job_id,
                schedule.schedule_id,
                occurrence.scheduled_at,
                schedule.owner_id,
                schedule.objective,
                payload,
                SCHEDULED_JOB_MAX_ATTEMPTS,
                now_text,
                now_text,
                now_text,
                schedule.priority,
                schedule.schedule_id,
            ),
        )
        if cursor.rowcount != 1:
            return None
        db.execute(
            "INSERT INTO eurus_schedule_runs "
            "(run_id, schedule_id, scheduled_at, status, started_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (job_id, schedule.schedule_id, occurrence.scheduled_at, now_text),
        )
        db.execute(
            "UPDATE eurus_schedule_occurrences SET status='dispatched', job_id=?, "
            "dispatched_at=?, attempts=attempts+1, updated_at=? WHERE occurrence_id=?",
            (job_id, now_text, now_text, occurrence.occurrence_id),
        )
        return job_id

    def _select_for_policy(
        self,
        schedule: Schedule,
        pending: Sequence[Occurrence],
        current: datetime,
        grace_seconds: int,
    ) -> list[tuple[Occurrence, str]]:
        policy = schedule.misfire_policy
        if policy == "coalesce":
            ordered = sorted(pending, key=lambda item: item.scheduled_at)
            decisions: list[tuple[Occurrence, str]] = [(ordered[-1], "dispatch")]
            decisions.extend((item, "coalesce") for item in ordered[:-1])
            return decisions
        grace = timedelta(seconds=grace_seconds)
        late = [
            item
            for item in pending
            if (parsed := _parse_stored(item.scheduled_at)) is not None and current - parsed > grace
        ]
        on_time = [item for item in pending if item not in late]
        decisions = [(item, "dispatch") for item in on_time]
        if policy == "skip":
            decisions.extend((item, "skip") for item in late)
        elif policy == "run_once":
            if late:
                decisions.append((late[-1], "dispatch"))
                decisions.extend((item, "coalesce") for item in late[:-1])
        elif policy == "catch_up":
            allowed = late[-schedule.catch_up_limit :]
            decisions.extend((item, "dispatch") for item in allowed)
            decisions.extend(
                (item, "coalesce") for item in late[: max(0, len(late) - schedule.catch_up_limit)]
            )
        return decisions

    def _advance_next_run(
        self, db: sqlite3.Connection, schedule: Schedule, current: datetime
    ) -> Schedule | None:
        updated = schedule
        if schedule.kind == "once":
            return _replace_field(
                _replace_field(schedule, "enabled", False),
                "occurrences_count",
                schedule.occurrences_count + 1,
            )
        if schedule.kind == "interval" and schedule.interval_seconds:
            interval = timedelta(seconds=schedule.interval_seconds)
            anchor = _parse_stored(schedule.next_run_at) or current
            following = anchor + interval
            if following <= current:
                following = current + interval
            updated = _replace_field(updated, "next_run_at", _iso(following))
        elif schedule.kind == "cron" and schedule.cron_expr:
            try:
                expression = CronExpression.parse(schedule.cron_expr)
                zone = ZoneInfo(schedule.timezone)
            except (CronParseError, ZoneInfoNotFoundError):
                return None
            latest = db.execute(
                "SELECT MAX(scheduled_at) AS latest FROM eurus_schedule_occurrences "
                "WHERE schedule_id=?",
                (schedule.schedule_id,),
            ).fetchone()
            latest_anchor = (
                _parse_stored(str(latest["latest"])) if latest and latest["latest"] else None
            )
            next_time = expression.next_after(latest_anchor or current, zone=zone)
            if next_time is None or next_time <= current:
                next_time = expression.next_after(current, zone=zone)
            if next_time is None:
                return None
            updated = _replace_field(updated, "next_run_at", _iso(next_time))
        dispatched_count = schedule.occurrences_count + 1
        updated = _replace_field(updated, "occurrences_count", dispatched_count)
        if schedule.max_occurrences is not None and dispatched_count >= schedule.max_occurrences:
            updated = _replace_field(updated, "enabled", False)
        return updated

    def _is_expired(self, schedule: Schedule, current: datetime) -> bool:
        expires = _parse_stored(schedule.expires_at)
        return expires is not None and expires <= current

    def _within_window(self, schedule: Schedule, current: datetime) -> bool:
        if not schedule.execution_window_start or not schedule.execution_window_end:
            return True
        try:
            zone = ZoneInfo(schedule.timezone)
        except ZoneInfoNotFoundError:
            return True
        local = current.astimezone(zone).time()
        start = _parse_window(schedule.execution_window_start)
        end = _parse_window(schedule.execution_window_end)
        if start <= end:
            return start <= local <= end
        return local >= start or local <= end

    def _concurrency_blocked(self, db: sqlite3.Connection, schedule: Schedule) -> bool:
        if schedule.concurrency_limit <= 0:
            return False
        row = db.execute(
            "SELECT COUNT(*) AS running FROM eurus_jobs WHERE schedule_id=? AND status='running'",
            (schedule.schedule_id,),
        ).fetchone()
        return int(row["running"]) >= schedule.concurrency_limit

    def _disable_schedule(self, db: sqlite3.Connection, schedule: Schedule, now_text: str) -> None:
        db.execute(
            "UPDATE eurus_schedules SET enabled=0, updated_at=? WHERE schedule_id=?",
            (now_text, schedule.schedule_id),
        )

    def _skip_pending(
        self, db: sqlite3.Connection, schedule: Schedule, reason: str, now_text: str
    ) -> None:
        db.execute(
            "UPDATE eurus_schedule_occurrences SET status='skipped', error=?, finished_at=?, "
            "updated_at=? WHERE schedule_id=? AND status='pending'",
            (reason, now_text, now_text, schedule.schedule_id),
        )

    def _store_revision(self, db: sqlite3.Connection, schedule: Schedule) -> None:
        db.execute(
            "INSERT OR REPLACE INTO eurus_schedule_revisions "
            "(schedule_id, revision, definition_json, created_at) VALUES (?, ?, ?, ?)",
            (
                schedule.schedule_id,
                schedule.revision,
                json.dumps(_schedule_snapshot(schedule), ensure_ascii=False),
                _iso(_utcnow()),
            ),
        )

    @staticmethod
    def _schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            str(row["schedule_id"]),
            str(row["owner_id"]),
            str(row["objective"]),
            row["kind"],
            str(row["timezone"]),
            str(row["next_run_at"]),
            row["interval_seconds"],
            bool(row["enabled"]),
            json.loads(row["profile_json"]),
            str(row["created_at"]),
            str(row["updated_at"]),
            cron_expr=row["cron_expr"],
            trigger_event=row["trigger_event"],
            trigger_subject=row["trigger_subject"],
            trigger_schedule=(
                row["trigger_schedule"] if "trigger_schedule" in set(row.keys()) else None
            ),
            misfire_policy=str(row["misfire_policy"]),
            catch_up_limit=int(row["catch_up_limit"]),
            max_occurrences=row["max_occurrences"],
            occurrences_count=int(row["occurrences_count"]),
            concurrency_limit=int(row["concurrency_limit"]),
            priority=int(row["priority"]),
            expires_at=row["expires_at"],
            execution_window_start=row["execution_window_start"],
            execution_window_end=row["execution_window_end"],
            revision=int(row["revision"]),
            name=str(row["name"]) if "name" in set(row.keys()) else "",
            deliver=str(row["deliver"]) if "deliver" in set(row.keys()) else "local",
            origin_gateway=(
                str(row["origin_gateway"]) if "origin_gateway" in set(row.keys()) else ""
            ),
            origin_conversation=(
                str(row["origin_conversation"]) if "origin_conversation" in set(row.keys()) else ""
            ),
            skills=(
                tuple(json.loads(row["skills_json"]))
                if "skills_json" in set(row.keys()) and row["skills_json"]
                else ()
            ),
            pre_script=str(row["pre_script"]) if "pre_script" in set(row.keys()) else "",
            silent=bool(row["silent"]) if "silent" in set(row.keys()) else False,
            verbatim=bool(row["verbatim"]) if "verbatim" in set(row.keys()) else False,
        )


def _schedule_values(schedule: Schedule) -> tuple[Any, ...]:
    return (
        schedule.schedule_id,
        schedule.owner_id,
        schedule.objective,
        schedule.kind,
        schedule.timezone,
        schedule.next_run_at,
        schedule.interval_seconds,
        int(schedule.enabled),
        json.dumps(schedule.profile, ensure_ascii=False),
        schedule.created_at,
        schedule.updated_at,
        schedule.cron_expr,
        schedule.trigger_event,
        schedule.trigger_subject,
        schedule.trigger_schedule,
        schedule.misfire_policy,
        schedule.catch_up_limit,
        schedule.max_occurrences,
        schedule.occurrences_count,
        schedule.concurrency_limit,
        schedule.priority,
        schedule.expires_at,
        schedule.execution_window_start,
        schedule.execution_window_end,
        schedule.revision,
        schedule.name,
        schedule.deliver,
        schedule.origin_gateway,
        schedule.origin_conversation,
        json.dumps(list(schedule.skills), ensure_ascii=False),
        schedule.pre_script,
        int(schedule.silent),
        int(schedule.verbatim),
    )


def _schedule_snapshot(schedule: Schedule) -> dict[str, Any]:
    return {
        "objective": schedule.objective,
        "kind": schedule.kind,
        "timezone": schedule.timezone,
        "next_run_at": schedule.next_run_at,
        "interval_seconds": schedule.interval_seconds,
        "cron_expr": schedule.cron_expr,
        "trigger_event": schedule.trigger_event,
        "trigger_subject": schedule.trigger_subject,
        "trigger_schedule": schedule.trigger_schedule,
        "misfire_policy": schedule.misfire_policy,
        "catch_up_limit": schedule.catch_up_limit,
        "max_occurrences": schedule.max_occurrences,
        "concurrency_limit": schedule.concurrency_limit,
        "priority": schedule.priority,
        "expires_at": schedule.expires_at,
        "execution_window_start": schedule.execution_window_start,
        "execution_window_end": schedule.execution_window_end,
        "profile": schedule.profile,
        "name": schedule.name,
        "deliver": schedule.deliver,
        "origin_gateway": schedule.origin_gateway,
        "origin_conversation": schedule.origin_conversation,
        "skills": list(schedule.skills),
        "pre_script": schedule.pre_script,
        "silent": schedule.silent,
        "verbatim": schedule.verbatim,
    }


def _occurrence(row: sqlite3.Row) -> Occurrence:
    return Occurrence(
        str(row["occurrence_id"]),
        str(row["schedule_id"]),
        str(row["scheduled_at"]),
        str(row["status"]),
        row["job_id"],
        int(row["attempts"]),
        row["error"],
        row["dispatched_at"],
        row["finished_at"],
        str(row["created_at"]),
        str(row["updated_at"]),
    )


def _replace_field(schedule: Schedule, field_name: str, value: Any) -> Schedule:
    from dataclasses import replace

    return replace(schedule, **{field_name: value})


def _validate_window(value: str | None) -> None:
    if value is None:
        return
    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError
        time(hour=int(parts[0]), minute=int(parts[1]))
    except ValueError as exc:
        raise ValueError("execution window must be HH:MM") from exc


def _parse_window(value: str) -> time:
    parts = value.split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


def _due_occurrence_times(
    schedule: Schedule, current: datetime, *, cap: int = 200
) -> list[datetime]:
    anchor = _parse_stored(schedule.next_run_at)
    if anchor is None or anchor > current:
        return []
    if schedule.kind == "once":
        return [anchor]
    if schedule.kind == "interval" and schedule.interval_seconds:
        interval = timedelta(seconds=schedule.interval_seconds)
        times: list[datetime] = []
        cursor_time = anchor
        while cursor_time <= current and len(times) < cap:
            times.append(cursor_time)
            cursor_time = cursor_time + interval
        return times[-(schedule.catch_up_limit + 1) :]
    if schedule.kind == "cron" and schedule.cron_expr:
        try:
            expression = CronExpression.parse(schedule.cron_expr)
            zone = ZoneInfo(schedule.timezone)
        except (CronParseError, ZoneInfoNotFoundError):
            return []
        times = []
        cursor_time = anchor
        while cursor_time <= current and len(times) < cap:
            times.append(cursor_time)
            try:
                cursor_time = expression.next_after(cursor_time, zone=zone)
            except CronParseError:
                break
        return times[-(schedule.catch_up_limit + 1) :]
    return []
