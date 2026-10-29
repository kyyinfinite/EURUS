from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.reminders import ReminderStore


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
    assert not store.cancel(item.reminder_id, "owner-b")
    reopened = ReminderStore(path)
    assert reopened.list("owner-a")[0].reminder_id == item.reminder_id
    assert reopened.cancel(item.reminder_id, "owner-a")


def test_due_claim_retry_and_restart_recovery(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "sessions.sqlite3")
    item = store.create("owner", "telegram", "123", "ping", due_at=future())
    claimed = store.claim_due(now=datetime.now(UTC) + timedelta(minutes=3))
    assert claimed and claimed.reminder_id == item.reminder_id
    assert store.claim_due(now=datetime.now(UTC) + timedelta(minutes=3)) is None
    store.finish_delivery(claimed, error="network", now=datetime.now(UTC))
    assert store.list("owner")[0].status == "retrying"
    claimed2 = store.claim_due(now=datetime.now(UTC) + timedelta(minutes=10))
    assert claimed2 is not None
    assert store.recover_sending() == 1
    assert store.list("owner")[0].status == "failed" or store.list("owner")[0].status == "retrying"


def test_rejects_ambiguous_or_invalid_schedule(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at="17")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at=future(), timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        store.create("o", "telegram", "1", "x", due_at=future(), interval_seconds=1)


def test_dispatcher_delivers_due_reminder_and_reschedules_interval(tmp_path: Path) -> None:
    import asyncio

    from eurus.reminders import ReminderDispatcher

    async def scenario() -> None:
        store = ReminderStore(tmp_path / "dispatch.sqlite3")
        now = datetime.now(UTC)
        store.create(
            "owner",
            "telegram",
            "123",
            "WOEE INGET",
            due_at=(now + timedelta(seconds=1)).isoformat(),
            interval_seconds=120,
        )
        delivered: list[tuple[str, str]] = []

        async def deliver(chat_id: str, text: str) -> None:
            delivered.append((chat_id, text))

        dispatcher = ReminderDispatcher(store, deliver, poll_seconds=0.1)
        task = asyncio.create_task(dispatcher.run())
        try:
            for _ in range(30):
                if delivered:
                    break
                await asyncio.sleep(0.1)
            assert delivered == [("123", "⏰ EURUS REMINDER\n\nWOEE INGET")]
            updated = store.list("owner")[0]
            assert updated.status == "pending"
            assert updated.interval_seconds == 120
            assert datetime.fromisoformat(updated.due_at) > datetime.now(UTC)
        finally:
            await dispatcher.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_reminder_tools_are_bound_to_current_gateway_owner(tmp_path: Path) -> None:
    import asyncio
    from pathlib import Path

    from eurus.models import ToolCall
    from eurus.reminders import reminder_tools, reset_reminder_context, set_reminder_context
    from eurus.tools.base import ToolContext, ToolRegistry

    async def scenario() -> None:
        store = ReminderStore(tmp_path / "tools.sqlite3")
        registry = ToolRegistry(reminder_tools(store))
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
            assert store.list("telegram:owner-a")[0].conversation_id == "123"
            assert store.list("telegram:owner-b") == []
        finally:
            reset_reminder_context(token)

    asyncio.run(scenario())


def test_cancel_for_conversation_stops_recurring_reminders_and_isolates_scope(
    tmp_path: Path,
) -> None:
    store = ReminderStore(tmp_path / "cancel.sqlite3")
    first = store.create(
        "owner-a",
        "telegram",
        "chat-a",
        "WOEE INGET",
        due_at=future(),
        interval_seconds=120,
    )
    second = store.create("owner-a", "telegram", "chat-a", "Submit assignment", due_at=future())
    other_chat = store.create("owner-a", "telegram", "chat-b", "Private reminder", due_at=future())
    other_owner = store.create(
        "owner-b", "telegram", "chat-a", "Other user reminder", due_at=future()
    )

    assert store.cancel_for_conversation("owner-a", "telegram", "chat-a", message="WOEE INGET") == 1
    listing = store.list("owner-a", active_only=False)
    assert (
        next(item for item in listing if item.reminder_id == first.reminder_id).status
        == "cancelled"
    )
    assert (
        next(item for item in listing if item.reminder_id == second.reminder_id).status == "pending"
    )
    assert store.cancel_for_conversation("owner-a", "telegram", "chat-a") == 1
    assert (
        next(item for item in listing if item.reminder_id == other_chat.reminder_id).status
        == "pending"
    )
    assert (
        next(
            item
            for item in store.list("owner-b", active_only=False)
            if item.reminder_id == other_owner.reminder_id
        ).status
        == "pending"
    )


def test_cancel_reminder_tool_accepts_natural_language_without_id(tmp_path: Path) -> None:
    import asyncio
    from pathlib import Path

    from eurus.models import ToolCall
    from eurus.reminders import reminder_tools, reset_reminder_context, set_reminder_context
    from eurus.tools.base import ToolContext, ToolRegistry

    async def scenario() -> None:
        store = ReminderStore(tmp_path / "cancel-tool.sqlite3")
        store.create(
            "telegram:owner-a",
            "telegram",
            "123",
            "WOEE INGET",
            due_at=future(),
            interval_seconds=120,
        )
        store.create("telegram:owner-a", "telegram", "123", "Review tasks", due_at=future())
        store.create("telegram:owner-a", "telegram", "456", "Different chat", due_at=future())
        registry = ToolRegistry(reminder_tools(store))
        token = set_reminder_context("telegram:owner-a", "123", "telegram")
        try:
            call = ToolCall(id="cancel-1", name="cancel_reminder", arguments={})
            result = await registry.execute(call, ToolContext(workspace=Path(".")))
            assert not result.is_error
            assert "Cancelled 2 active reminder(s)" in result.output
            assert store.list("telegram:owner-a", active_only=True) == [
                item
                for item in store.list("telegram:owner-a", active_only=True)
                if item.conversation_id == "456"
            ]
        finally:
            reset_reminder_context(token)

    asyncio.run(scenario())
