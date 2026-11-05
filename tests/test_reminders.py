import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.owner import OWNER_ID
from eurus.reminders import (
    REMINDER_NAME_PREFIX,
    REMINDER_OBJECTIVE_PREFIX,
    ReminderStore,
    migrate_reminders_to_schedules,
)
from eurus.scheduler import ScheduleStore


def future(minutes: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def test_persistent_interval_reminder_and_owner_scope(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = ReminderStore(path)
    item = store.create(
        "owner-a",
        "telegram",
        "123",
        "WOEE INGET",
        due_at=future(),
        interval_seconds=120,
    )
    assert item.message == "WOEE INGET"
    assert item.interval_seconds == 120
    assert len(store.list("owner-a")) == 1
    assert store.list("owner-b") == []
    reopened = ReminderStore(path)
    assert reopened.list("owner-a")[0].reminder_id == item.reminder_id
    assert reopened.list("owner-a")[0].interval_seconds == 120


def test_rejects_ambiguous_or_invalid_schedule(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at="17")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at=future(), timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at=future(), interval_seconds=1)


def test_reminder_tools_are_bound_to_current_gateway_owner(tmp_path: Path) -> None:
    import asyncio
    from pathlib import Path

    from eurus.models import ToolCall
    from eurus.reminders import reminder_tools, reset_reminder_context, set_reminder_context
    from eurus.tools.base import ToolContext, ToolRegistry

    async def scenario() -> None:
        path = tmp_path / "tools.sqlite3"
        store = ReminderStore(path)
        schedules = ScheduleStore(path)
        registry = ToolRegistry(reminder_tools(store, schedules))
        token = set_reminder_context("telegram:owner-a", "123", "telegram")
        try:
            due = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
            call = ToolCall(
                id="c1",
                name="create_reminder",
                arguments={
                    "message": "WOEE INGET",
                    "due_at": due,
                    "interval_seconds": 120,
                    "timezone": "Asia/Jakarta",
                },
            )
            result = await registry.execute(call, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "Reminder created" in result.output
            created = schedules.list_owner(OWNER_ID)[0]
            assert created.deliver == "telegram:123"
            assert created.origin_gateway == "telegram"
            assert created.origin_conversation == "123"
            assert created.kind == "interval"
            assert created.interval_seconds == 120
            assert created.verbatim is True
        finally:
            reset_reminder_context(token)
        orphan = ToolCall(
            id="c2",
            name="create_reminder",
            arguments={
                "message": "x",
                "due_at": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            },
        )
        result = await registry.execute(orphan, ToolContext(workspace=Path(".")))
        assert result.is_error
        assert "gateway conversation" in result.output

    asyncio.run(scenario())


def test_cancel_for_conversation_stops_recurring_reminders_and_isolates_scope(
    tmp_path: Path,
) -> None:
    import asyncio
    from pathlib import Path

    from eurus.models import ToolCall
    from eurus.reminders import reminder_tools, reset_reminder_context, set_reminder_context
    from eurus.tools.base import ToolContext, ToolRegistry

    async def scenario() -> None:
        path = tmp_path / "cancel.sqlite3"
        store = ReminderStore(path)
        schedules = ScheduleStore(path)
        registry = ToolRegistry(reminder_tools(store, schedules))
        due = future()
        token_a = set_reminder_context("telegram:owner-a", "111", "telegram")
        try:
            for message in ("WOEE INGET", "Submit assignment"):
                call = ToolCall(
                    id=f"create-{message}",
                    name="create_reminder",
                    arguments={"message": message, "due_at": due, "interval_seconds": 120},
                )
                result = await registry.execute(call, ToolContext(workspace=Path(".")))
                assert not result.is_error
        finally:
            reset_reminder_context(token_a)
        token_b = set_reminder_context("telegram:owner-a", "222", "telegram")
        try:
            call = ToolCall(
                id="create-other",
                name="create_reminder",
                arguments={"message": "Private reminder", "due_at": due},
            )
            result = await registry.execute(call, ToolContext(workspace=Path(".")))
            assert not result.is_error
        finally:
            reset_reminder_context(token_b)

        token_a = set_reminder_context("telegram:owner-a", "111", "telegram")
        try:
            cancel = ToolCall(id="cancel-a", name="cancel_reminder", arguments={})
            result = await registry.execute(cancel, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "Cancelled 2 active reminder(s)" in result.output
        finally:
            reset_reminder_context(token_a)

        remaining = schedules.list_owner(OWNER_ID, limit=100)
        assert len(remaining) == 1
        assert remaining[0].origin_conversation == "222"
        assert remaining[0].enabled is True

        token_b = set_reminder_context("telegram:owner-a", "222", "telegram")
        try:
            cancel = ToolCall(id="cancel-b", name="cancel_reminder", arguments={})
            result = await registry.execute(cancel, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "Cancelled 1 active reminder(s)" in result.output
            assert schedules.list_owner(OWNER_ID, limit=100) == []
        finally:
            reset_reminder_context(token_b)

    asyncio.run(scenario())


def test_cancel_reminder_tool_accepts_natural_language_without_id(tmp_path: Path) -> None:
    import asyncio
    from pathlib import Path

    from eurus.models import ToolCall
    from eurus.reminders import reminder_tools, reset_reminder_context, set_reminder_context
    from eurus.tools.base import ToolContext, ToolRegistry

    async def scenario() -> None:
        path = tmp_path / "cancel-tool.sqlite3"
        store = ReminderStore(path)
        schedules = ScheduleStore(path)
        registry = ToolRegistry(reminder_tools(store, schedules))
        token = set_reminder_context("telegram:owner-a", "123", "telegram")
        try:
            empty = ToolCall(id="cancel-0", name="cancel_reminder", arguments={})
            result = await registry.execute(empty, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "No matching active reminder" in result.output
            for index, message in enumerate(("WOEE INGET", "Review tasks")):
                call = ToolCall(
                    id=f"create-{index}",
                    name="create_reminder",
                    arguments={"message": message, "due_at": future()},
                )
                result = await registry.execute(call, ToolContext(workspace=Path(".")))
                assert not result.is_error
            call = ToolCall(id="cancel-1", name="cancel_reminder", arguments={})
            result = await registry.execute(call, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "Cancelled 2 active reminder(s)" in result.output
            assert schedules.list_owner(OWNER_ID, limit=100) == []
        finally:
            reset_reminder_context(token)

    asyncio.run(scenario())


def test_migration_maps_gateways_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "eurus.sqlite3"
    reminders = ReminderStore(path)
    schedules = ScheduleStore(path)
    telegram = reminders.create("eurus", "telegram", "424242", "standup", due_at=future())
    web = reminders.create("eurus", "web", "chat-1", "check mail", due_at=future())
    tui = reminders.create("eurus", "tui", "local", "rotate logs", due_at=future())
    unknown = reminders.create("eurus", "carrier-pigeon", "9", "mystery", due_at=future())
    interval = reminders.create(
        "eurus", "telegram", "777", "stretch", due_at=future(), interval_seconds=300
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE eurus_reminders SET status='delivered' WHERE reminder_id=?",
            (unknown.reminder_id,),
        )

    assert migrate_reminders_to_schedules(reminders, schedules) == 4

    by_name = {s.name: s for s in schedules.list_owner(OWNER_ID, limit=100)}
    telegram_schedule = by_name[f"{REMINDER_NAME_PREFIX}:{telegram.reminder_id[:12]}"]
    assert telegram_schedule.deliver == "telegram:424242"
    assert telegram_schedule.kind == "once"
    assert telegram_schedule.verbatim is True
    assert telegram_schedule.origin_gateway == "telegram"
    assert telegram_schedule.origin_conversation == "424242"
    assert telegram_schedule.objective.startswith(REMINDER_OBJECTIVE_PREFIX)
    assert telegram_schedule.objective.endswith("standup")
    assert by_name[f"{REMINDER_NAME_PREFIX}:{web.reminder_id[:12]}"].deliver == "web"
    assert by_name[f"{REMINDER_NAME_PREFIX}:{tui.reminder_id[:12]}"].deliver == "tui"
    interval_schedule = by_name[f"{REMINDER_NAME_PREFIX}:{interval.reminder_id[:12]}"]
    assert interval_schedule.kind == "interval"
    assert interval_schedule.interval_seconds == 300

    rows = {r.reminder_id: r.status for r in reminders.list("eurus", active_only=False)}
    assert rows[unknown.reminder_id] == "delivered"
    for item in (telegram, web, tui, interval):
        assert rows[item.reminder_id] == "migrated"

    assert migrate_reminders_to_schedules(reminders, schedules) == 0
    assert len(schedules.list_owner(OWNER_ID, limit=100)) == 4


def test_migration_keeps_owner_rows_intact(tmp_path: Path) -> None:
    path = tmp_path / "eurus.sqlite3"
    reminders = ReminderStore(path)
    schedules = ScheduleStore(path)
    item = reminders.create("owner-a", "web", "chat-9", "daily digest", due_at=future())
    assert migrate_reminders_to_schedules(reminders, schedules) == 1
    reopened = ReminderStore(path)
    rows = reopened.list("owner-a", active_only=False)
    assert len(rows) == 1
    assert rows[0].reminder_id == item.reminder_id
    assert rows[0].message == "daily digest"
    assert rows[0].status == "migrated"
    assert "migrated:schedule:" in rows[0].last_error
    schedule = schedules.list_owner(OWNER_ID, limit=10)[0]
    assert schedule.owner_id == OWNER_ID
