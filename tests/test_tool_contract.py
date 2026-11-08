from __future__ import annotations

from pathlib import Path

import pytest

from eurus.models import ToolCall
from eurus.runtime.contracts import ToolExecutionStatus
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer"],
    "additionalProperties": False,
}


def _output_tool(payload: str) -> Tool:
    async def handler(arguments, context):
        return payload

    return Tool(
        "answer_query",
        "answer a query",
        INPUT_SCHEMA,
        handler,
        output_schema=OUTPUT_SCHEMA,
    )


@pytest.mark.asyncio
async def test_registry_validates_declared_output_schema():
    registry = ToolRegistry([_output_tool('{"answer": "ok", "confidence": 0.9}')])
    result = await registry.execute(
        ToolCall("c1", "answer_query", {"query": "hi"}), ToolContext(workspace=Path("."))
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_registry_rejects_output_that_is_not_json():
    registry = ToolRegistry([_output_tool("plain text, not json")])
    result = await registry.execute(
        ToolCall("c2", "answer_query", {"query": "hi"}), ToolContext(workspace=Path("."))
    )
    assert result.is_error
    assert "output must be valid JSON" in result.output


@pytest.mark.asyncio
async def test_registry_rejects_output_that_violates_schema():
    registry = ToolRegistry([_output_tool('{"answer": "ok", "confidence": 5}')])
    result = await registry.execute(
        ToolCall("c3", "answer_query", {"query": "hi"}), ToolContext(workspace=Path("."))
    )
    assert result.is_error
    assert "violated its declared schema" in result.output
    assert "output.confidence must be at most 1" in result.output


@pytest.mark.asyncio
async def test_boundary_enforces_output_schema_on_success():
    boundary = ToolExecutionBoundary(
        ToolRegistry([_output_tool('{"answer": "ok", "confidence": 5}')]),
        ToolContext(workspace=Path(".")),
    )
    outcome = await boundary.execute(ToolCall("c4", "answer_query", {"query": "hi"}))
    assert outcome.status is ToolExecutionStatus.FAILED
    assert outcome.is_error
    assert "output.confidence must be at most 1" in outcome.output


@pytest.mark.asyncio
async def test_boundary_skips_output_validation_for_tool_errors():
    async def handler(arguments, context):
        return "error: upstream refused"

    tool = Tool(
        "answer_query",
        "answer a query",
        INPUT_SCHEMA,
        handler,
        output_schema=OUTPUT_SCHEMA,
    )
    boundary = ToolExecutionBoundary(ToolRegistry([tool]), ToolContext(workspace=Path(".")))
    outcome = await boundary.execute(ToolCall("c5", "answer_query", {"query": "hi"}))
    assert outcome.status is ToolExecutionStatus.FAILED
    assert "violated its declared schema" not in outcome.output


@pytest.mark.asyncio
async def test_tools_without_output_schema_are_unchanged():
    async def handler(arguments, context):
        return "free-form text"

    tool = Tool("free_form", "no declared output schema", INPUT_SCHEMA, handler)
    boundary = ToolExecutionBoundary(ToolRegistry([tool]), ToolContext(workspace=Path(".")))
    outcome = await boundary.execute(ToolCall("c6", "free_form", {"query": "hi"}))
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert outcome.output == "free-form text"


def test_registry_decorator_wires_output_schema():
    calls: list[str] = []
    registry = ToolRegistry()

    @registry.tool(
        "decorated",
        "decorated tool",
        INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
    )
    async def handler(arguments, context):
        calls.append(arguments["query"])
        return '{"answer": "ok"}'

    tool = registry.get("decorated")
    assert tool is not None
    assert tool.output_schema == OUTPUT_SCHEMA
