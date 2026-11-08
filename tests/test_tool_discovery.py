from __future__ import annotations

import asyncio
from pathlib import Path

from eurus.agent.structured_planner import parse_structured_plan
from eurus.agent.task_state import TaskState
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.builtin import builtin_tools, default_registry
from eurus.tools.discovery import (
    BASELINE_TOOL_NAMES,
    ToolExposure,
    ToolSelector,
    categories_for_tool,
    describe_tool,
)


def _tool(name: str, *, mutates: bool = False) -> Tool:
    async def handler(arguments: dict[str, object], context: ToolContext) -> str:
        return "ok"

    return Tool(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        mutates=mutates,
    )


def _state_with_categories(categories: tuple[str, ...]) -> TaskState:
    from eurus.agent.structured_planner import structured_to_task_plan

    plan = parse_structured_plan(
        "produce a report",
        {
            "objective": "produce a report",
            "assumptions": [],
            "open_questions": [],
            "steps": [
                {
                    "step_id": "s1",
                    "title": "research the topic",
                    "description": "gather sources",
                    "depends_on": [],
                    "expected_outputs": ["notes"],
                    "success_criteria": "notes exist",
                    "tool_categories": list(categories),
                    "risk": "low",
                    "verification": "check notes",
                }
            ],
            "replan_conditions": [],
        },
    )
    return TaskState.from_plan(structured_to_task_plan(plan))


def _registry() -> ToolRegistry:
    registry = ToolRegistry(builtin_tools())
    registry.register(_tool("web_search", mutates=False))
    registry.register(_tool("read_file", mutates=False))
    registry.register(_tool("edit_file", mutates=True))
    registry.register(_tool("shell", mutates=True))
    registry.register(_tool("custom_probe"))
    return registry


def test_category_map_covers_first_party_tools() -> None:
    assert categories_for_tool("web_search") == frozenset({"search", "web"})
    assert categories_for_tool("git_status") == frozenset({"git", "read"})
    assert categories_for_tool("office_specialist") == frozenset({"write", "analysis"})
    assert categories_for_tool("nonexistent_tool") == frozenset()


def test_describe_tool_exposes_contract_fields() -> None:
    tool = _tool("read_file", mutates=True)
    view = describe_tool(tool)
    assert view["name"] == "read_file"
    assert view["mutates"] is True
    assert view["requires_approval"] is False
    assert view["input_schema"]["type"] == "object"
    assert isinstance(view["categories"], list)


def test_selector_without_plan_returns_registry() -> None:
    registry = _registry()
    selector = ToolSelector()
    exposure = selector.exposure(registry, None)
    assert exposure is registry


def test_selector_filters_by_plan_categories() -> None:
    registry = _registry()
    selector = ToolSelector()
    state = _state_with_categories(("read", "web"))
    exposure = selector.exposure(registry, state)
    assert isinstance(exposure, ToolExposure)
    names = set(exposure.names())
    assert "read_file" in names
    assert "web_search" in names
    for name in BASELINE_TOOL_NAMES:
        if name in {tool.name for tool in registry.all()}:
            assert name in names
    assert "custom_probe" in names
    assert "edit_file" not in names
    assert "shell" not in names
    assert "calculate" in names


def test_selector_exposes_uncategorized_tools_always() -> None:
    registry = _registry()
    selector = ToolSelector()
    state = _state_with_categories(("git",))
    exposure = selector.exposure(registry, state)
    assert "custom_probe" in exposure
    assert exposure.get("custom_probe") is not None
    assert exposure.get("edit_file") is None


def test_exposure_view_enforces_execution_boundary() -> None:
    registry = _registry()
    selector = ToolSelector()
    state = _state_with_categories(("read",))
    exposure = selector.exposure(registry, state)
    assert exposure.get("shell") is None
    assert "shell" not in exposure
    assert len(exposure) == len(exposure.all())


def test_exposure_view_registration_passthrough() -> None:
    registry = _registry()
    selector = ToolSelector()
    exposure = selector.exposure(registry, _state_with_categories(("read",)))
    exposure.register(_tool("late_tool"))
    assert registry.get("late_tool") is not None
    assert exposure.unregister("late_tool") is not None


def test_selection_note_reports_counts() -> None:
    registry = _registry()
    selector = ToolSelector()
    state = _state_with_categories(("read", "web"))
    exposure = selector.exposure(registry, state)
    note = selector.selection_note(state, exposure)
    assert "tool selection" in note
    assert "read" in note and "web" in note
    assert selector.selection_note(None, registry) == ""


def test_default_registry_tools_have_consistent_contracts() -> None:
    registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
    for tool in registry.all():
        view = describe_tool(tool)
        assert view["name"] == tool.name
        assert isinstance(view["categories"], list)
        assert view["mutates"] == tool.mutates
        assert view["required_capabilities"] == list(tool.required_capabilities)


def test_boundary_rejects_tool_hidden_from_exposure() -> None:
    from eurus.tools.boundary import ToolExecutionBoundary

    async def approve(call: ToolCall, tool: Tool) -> bool:
        return True

    registry = _registry()
    context = ToolContext(workspace=Path.cwd())
    boundary = ToolExecutionBoundary(registry, context, approver=approve)
    selector = ToolSelector()
    exposure = selector.exposure(registry, _state_with_categories(("read",)))
    boundary.registry = exposure
    outcome = asyncio.run(
        boundary.execute(ToolCall("call-1", "shell", {"command": "ls"}), run_id="run-1")
    )
    assert outcome.is_error is True
    assert "unknown tool" in outcome.output
