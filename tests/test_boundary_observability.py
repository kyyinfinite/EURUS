from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eurus.errors import ToolError
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext, ToolOutput
from eurus.tools.boundary import ToolExecutionBoundary


def _events() -> tuple[list[tuple[str, dict]], Callable]:
    recorded: list[tuple[str, dict]] = []

    async def recorder(event_type: str, payload: dict) -> None:
        recorded.append((event_type, payload))

    return recorded, recorder


def _context(tmp_path: Path, **overrides: Any) -> ToolContext:
    return ToolContext(workspace=tmp_path, timeout=2.0, **overrides)


def _tool(name: str, handler: Any, **flags: Any) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        **flags,
    )


async def _ok(arguments: dict, context: ToolContext) -> str:
    await asyncio.sleep(0.02)
    return "done"


async def _boom(arguments: dict, context: ToolContext) -> str:
    raise ToolError("planned failure")


async def _hangs(arguments: dict, context: ToolContext) -> str:
    await asyncio.sleep(5)


def _boundary(
    registry: Any,
    context: ToolContext,
    recorder: Any,
    approver: Any = None,
) -> ToolExecutionBoundary:
    return ToolExecutionBoundary(
        registry,
        context,
        approver=approver,
        event_recorder=recorder,
    )


@pytest.mark.asyncio
async def test_finished_event_carries_duration_and_error_code(tmp_path: Path) -> None:
    from eurus.tools.base import ToolRegistry

    registry = ToolRegistry([_tool("fast", _ok), _tool("broken", _boom), _tool("slow", _hangs)])
    recorded, recorder = _events()
    boundary = _boundary(registry, _context(tmp_path), recorder)
    ok_outcome = await boundary.execute(ToolCall("c1", "fast", {}), run_id="r1")
    err_outcome = await boundary.execute(ToolCall("c2", "broken", {}), run_id="r1")
    timeout_outcome = await boundary.execute(ToolCall("c3", "slow", {}), run_id="r1")
    finished = [payload for name, payload in recorded if name == "tool.finished"]
    ok_event = next(payload for payload in finished if payload["tool"] == "fast")
    err_event = next(payload for payload in finished if payload["tool"] == "broken")
    timeout_event = next(payload for payload in finished if payload["tool"] == "slow")
    assert ok_event["duration_ms"] >= 15
    assert ok_event["is_error"] is False
    assert ok_event["status"] == "success"
    assert err_event["is_error"] is True
    assert err_event.get("error_code", "") == ""
    assert err_event["duration_ms"] is not None
    assert timeout_event["is_error"] is True
    assert timeout_event["error_code"] == "timeout"
    assert timeout_event["duration_ms"] >= 1900
    assert ok_outcome.status.value == "success"
    assert err_outcome.status.value == "failed"
    assert timeout_outcome.status.value == "timeout"


@pytest.mark.asyncio
async def test_denied_outcome_carries_approval_error_code(tmp_path: Path) -> None:
    from eurus.tools.base import ToolRegistry
    from eurus.tools.boundary import OUTCOME_ERROR_CODES

    registry = ToolRegistry([_tool("guarded", _ok, mutates=True)])

    async def deny(call: ToolCall, tool: Tool) -> bool:
        return False

    recorded, recorder = _events()
    boundary = _boundary(registry, _context(tmp_path), recorder, approver=deny)
    outcome = await boundary.execute(ToolCall("c1", "guarded", {}), run_id="r1")
    assert outcome.is_error is True
    assert outcome.status.value == "denied"
    assert OUTCOME_ERROR_CODES[outcome.status.value] == "approval_denied"
    types = [name for name, _ in recorded]
    assert "approval.requested" in types
    assert "approval.denied" in types


@pytest.mark.asyncio
async def test_unavailable_approver_denies(tmp_path: Path) -> None:
    from eurus.tools.base import ToolRegistry

    registry = ToolRegistry([_tool("guarded", _ok, requires_approval=True)])
    recorded, recorder = _events()
    boundary = _boundary(registry, _context(tmp_path), recorder)
    outcome = await boundary.execute(ToolCall("c1", "guarded", {}), run_id="r1")
    assert outcome.is_error is True
    assert outcome.status.value == "denied"
    types = [name for name, _ in recorded]
    assert "approval.unavailable" in types


@pytest.mark.asyncio
async def test_artifact_event_reaches_context_sink(tmp_path: Path) -> None:
    from eurus.tools.artifacts import artifact_provider, register_tool_artifact

    provider = artifact_provider(tmp_path / "g.db", tmp_path / "artifacts")
    target = tmp_path / "shot.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    events: list[tuple[str, dict]] = []

    def sink(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    context = ToolContext(
        workspace=tmp_path,
        run_id="r-art",
        owner_id="eurus",
        artifacts=provider,
        event_sink=sink,
    )
    view = register_tool_artifact(
        context, target, source_tool="browser_screenshot", artifact_type="design"
    )
    assert view is not None
    assert events[0][0] == "artifact.registered"
    assert events[0][1]["source_tool"] == "browser_screenshot"
    assert events[0][1]["run_id"] == "r-art"


@pytest.mark.asyncio
async def test_tool_output_timeout_status_maps_to_error_code(tmp_path: Path) -> None:
    from eurus.tools.base import ToolRegistry

    async def reports_timeout(arguments: dict, context: ToolContext) -> ToolOutput:
        return ToolOutput("partial", is_error=True, status="timeout")

    async def approve(call: ToolCall, tool: Tool) -> bool:
        return True

    registry = ToolRegistry([_tool("selftimeout", reports_timeout, mutates=True)])
    recorded, recorder = _events()
    boundary = _boundary(registry, _context(tmp_path), recorder, approver=approve)
    outcome = await boundary.execute(ToolCall("c1", "selftimeout", {}), run_id="r1")
    assert outcome.status.value == "timeout"
    finished = [payload for name, payload in recorded if name == "tool.finished"]
    assert finished[0]["error_code"] == "timeout"
    assert finished[0]["uncertain"] is True
