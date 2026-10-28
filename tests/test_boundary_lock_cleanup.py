"""C6a regression tests: boundary lock maps must not grow without bound.

Before the fix, ``_inflight`` and ``_path_locks`` kept one asyncio.Lock per
distinct tool call key forever, so a long-lived gateway agent leaked lock
objects for every unique command it ever ran.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

MUTATING_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def _mutating_tool(handler) -> Tool:
    return Tool(
        "write_file",
        "write a file",
        MUTATING_SCHEMA,
        handler,
        mutates=True,
        path_keys=("path",),
    )


@pytest.mark.asyncio
async def test_inflight_and_path_locks_are_cleaned_up_after_dispatch():
    async def handler(arguments, context):
        return "ok"

    boundary = ToolExecutionBoundary(
        ToolRegistry([_mutating_tool(handler)]),
        ToolContext(workspace=Path(".")),
        approver=lambda call, tool: True,
    )
    for index in range(25):
        outcome = await boundary.execute(
            ToolCall(f"call-{index}", "write_file", {"path": f"file-{index}.txt"}),
            run_id="run-c6",
        )
        assert outcome.output == "ok"

    assert boundary._inflight == {}, "inflight locks must be released after each dispatch"
    assert boundary._path_locks == {}, "path locks must be released after each dispatch"


@pytest.mark.asyncio
async def test_same_key_executions_stay_serialized_and_cleanup_afterwards():
    active = 0
    max_active = 0

    async def handler(arguments, context):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    boundary = ToolExecutionBoundary(
        ToolRegistry([_mutating_tool(handler)]),
        ToolContext(workspace=Path(".")),
        approver=lambda call, tool: True,
    )
    results = await asyncio.gather(
        *(
            boundary.execute(
                ToolCall(f"call-{index}", "write_file", {"path": "same.txt"}),
                run_id="run-c6",
            )
            for index in range(6)
        )
    )
    assert all(result.output == "ok" for result in results)
    assert max_active == 1, "identical mutating calls must be serialized"
    assert boundary._inflight == {}
    assert boundary._path_locks == {}


@pytest.mark.asyncio
async def test_readonly_tools_also_release_inflight_locks():
    async def handler(arguments, context):
        return "value"

    tool = Tool(
        "read_value",
        "read a value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )
    boundary = ToolExecutionBoundary(ToolRegistry([tool]), ToolContext(workspace=Path(".")))
    for index in range(10):
        await boundary.execute(
            ToolCall(f"call-{index}", "read_value", {"value": f"v{index}"}), run_id="run-c6"
        )
    assert boundary._inflight == {}
