from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.errors import ToolError
from eurus.schedule_tools import (
    reset_schedule_context,
    schedule_tool,
    set_schedule_context,
)
from eurus.scheduler import ScheduleStore
from eurus.tools.base import ToolContext


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScheduleStore:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    from eurus.storage.database import Database

    Database(tmp_path / "data" / "eurus.sqlite3")
    return ScheduleStore(tmp_path / "data" / "eurus.sqlite3")


def make_context() -> ToolContext:
    return ToolContext(workspace=Path("."), run_id="run-test", owner_id="eurus")


def _run(tool, arguments):
    return asyncio.run(tool.handler(arguments, make_context()))


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def test_schedule_tool_requires_conversation_context(store: ScheduleStore) -> None:
    tool = schedule_tool(store)
    with pytest.raises(ToolError):
        _run(tool, {"action": "create", "objective": "x", "kind": "once", "run_at": _future()})


def test_create_list_update_pause_resume_run_remove(store: ScheduleStore) -> None:
    tool = schedule_tool(store)
    token = set_schedule_context("eurus", "web", "session-1")
    try:
        created = _run(
            tool,
            {
                "action": "create",
                "objective": "daily digest",
                "kind": "interval",
                "interval_seconds": 3600,
                "timezone": "Asia/Jakarta",
                "name": "digest",
                "skills": ["antislop"],
            },
        )
        assert "created" in created
        listing = _run(tool, {"action": "list"})
        assert "digest" in listing

        schedule = store.list_owner("eurus")[0]
        assert schedule.deliver == "origin"
        assert schedule.origin_gateway == "web"
        assert schedule.origin_conversation == "session-1"

        updated = _run(
            tool,
            {
                "action": "update",
                "schedule_id": schedule.schedule_id,
                "deliver": "local",
                "silent": True,
            },
        )
        assert "updated" in updated
        refreshed = store.get(schedule.schedule_id)
        assert refreshed is not None
        assert refreshed.deliver == "local"
        assert refreshed.silent is True

        assert "paused" in _run(tool, {"action": "pause", "schedule_id": schedule.schedule_id})
        assert store.get(schedule.schedule_id).enabled is False
        assert "resumed" in _run(tool, {"action": "resume", "schedule_id": schedule.schedule_id})
        assert store.get(schedule.schedule_id).enabled is True

        job_line = _run(tool, {"action": "run", "schedule_id": schedule.schedule_id})
        assert "Triggered" in job_line

        assert "removed" in _run(tool, {"action": "remove", "schedule_id": schedule.schedule_id})
        assert store.get(schedule.schedule_id) is None
    finally:
        reset_schedule_context(token)


def test_create_with_telegram_chat_id_target(store: ScheduleStore) -> None:
    tool = schedule_tool(store)
    token = set_schedule_context("eurus", "telegram", "777")
    try:
        _run(
            tool,
            {
                "action": "create",
                "objective": "ping me",
                "kind": "once",
                "run_at": _future(),
                "telegram_chat_id": "100200300",
            },
        )
        schedule = store.list_owner("eurus")[0]
        assert schedule.deliver == "telegram:100200300"
        assert schedule.origin_gateway == "telegram"
    finally:
        reset_schedule_context(token)


def test_unknown_action_and_bad_input_rejected(store: ScheduleStore) -> None:
    tool = schedule_tool(store)
    token = set_schedule_context("eurus", "web", "s")
    try:
        with pytest.raises(ToolError):
            _run(tool, {"action": "teleport"})
        with pytest.raises(ToolError):
            _run(tool, {"action": "create", "objective": "x", "kind": "yearly"})
        with pytest.raises(ToolError):
            _run(tool, {"action": "pause"})
    finally:
        reset_schedule_context(token)


def test_schedule_tool_schema_is_compact_single_surface(store: ScheduleStore) -> None:
    tool = schedule_tool(store)
    spec = tool.spec()
    payload = json.dumps(
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
    )
    assert len(payload) < 4000
    assert len(tool.spec().parameters["properties"]["action"]["enum"]) == 7


def test_scheduled_job_blocks_schedule_and_goal_tools() -> None:
    from eurus.automation.policies import ExecutionPolicy

    policy = ExecutionPolicy.from_payload(
        {"blocked_tools": ("schedule", "goal_submit_plan")},
        scope="scheduled",
    )
    assert not policy.allows_tool("schedule")
    assert not policy.allows_tool("goal_submit_plan")
    assert policy.allows_tool("read_file")
