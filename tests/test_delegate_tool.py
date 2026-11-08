from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from eurus.agent.delegate import DELEGATE_MODES, MAX_BATCH_TASKS, delegate_tool
from eurus.config import Config
from eurus.multiagent.registry import SpecialistRegistry, seed_builtin_specialists
from eurus.office.config import OfficeConfig
from eurus.office.errors import OfficeError
from eurus.providers.testing import openai_message, scripted_router
from eurus.tools.base import ToolContext, ToolRegistry


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path / "home"))
    return tmp_path


def run_tool(tool, arguments: dict, workspace: Path) -> str:
    return asyncio.run(tool.handler(arguments, ToolContext(workspace=workspace)))


def build(config: Config | None = None, responses=None):
    router, transport = scripted_router(json_responses=responses or [])
    tool = delegate_tool(config or Config(), router, ToolRegistry())
    return tool, transport


def test_delegate_tool_contract() -> None:
    tool, _ = build()
    assert tool.name == "delegate"
    assert tool.requires_approval is True
    assert tool.parallel_safe is False
    assert tool.interactive is True
    assert tool.parameters["required"] == ["mode"]
    assert tool.parameters["properties"]["mode"]["enum"] == list(DELEGATE_MODES)
    assert tool.parameters["properties"]["tasks"]["maxItems"] == MAX_BATCH_TASKS


def test_delegate_rejects_unknown_mode(home: Path) -> None:
    tool, _ = build()
    with pytest.raises(ValueError, match="mode must be one of"):
        run_tool(tool, {"mode": "magic"}, home)


def test_delegate_batch_requires_non_empty_task_array(home: Path) -> None:
    tool, _ = build()
    with pytest.raises(ValueError, match="non-empty array"):
        run_tool(tool, {"mode": "subagent"}, home)
    with pytest.raises(ValueError, match="non-empty array"):
        run_tool(tool, {"mode": "subagent", "tasks": []}, home)


def test_delegate_batch_caps_task_count(home: Path) -> None:
    tool, _ = build()
    tasks = [{"task": f"task-{index}"} for index in range(MAX_BATCH_TASKS + 1)]
    with pytest.raises(ValueError, match="at most"):
        run_tool(tool, {"mode": "subagent", "tasks": tasks}, home)


def test_delegate_batch_completes_and_isolates_failures(home: Path) -> None:
    tool, transport = build(responses=[openai_message("alpha done")])
    outcome = run_tool(
        tool,
        {"mode": "subagent", "tasks": [{"task": "alpha"}, {"task": "   "}]},
        home,
    )
    payload = json.loads(outcome)
    assert payload["results"][0]["result"] == "alpha done"
    assert payload["results"][0]["role"] == "general"
    assert payload["results"][1]["status"] == "failed"
    assert "task" in payload["results"][1]["error"]
    assert len(transport.calls) == 1


def test_delegate_child_registry_excludes_delegate(home: Path) -> None:
    parent = ToolRegistry()
    router, _ = scripted_router(json_responses=[openai_message("done")])
    tool = delegate_tool(Config(), router, parent)
    parent.register(tool)
    outcome = run_tool(tool, {"mode": "subagent", "tasks": [{"task": "inspect"}]}, home)
    payload = json.loads(outcome)
    assert payload["results"][0]["result"] == "done"
    assert "delegate" not in payload["results"][0]["available_tools"]


def test_delegate_role_profile_reaches_subagent_prompt(home: Path) -> None:
    seed_builtin_specialists(SpecialistRegistry(home / "home" / "data" / "eurus.sqlite3"))
    tool, transport = build(responses=[openai_message("research done")])
    outcome = run_tool(
        tool,
        {"mode": "subagent", "tasks": [{"task": "find evidence", "role": "researcher"}]},
        home,
    )
    payload = json.loads(outcome)
    assert payload["results"][0]["result"] == "research done"
    assert payload["results"][0]["role"] == "researcher"
    system = transport.calls[0].payload["messages"][0]["content"]
    assert "researcher" in system
    assert "Read-only research specialist" in system


def test_delegate_coder_mode_requires_task(home: Path) -> None:
    tool, _ = build()
    with pytest.raises(ValueError, match="task must not be empty"):
        run_tool(tool, {"mode": "coder"}, home)


def test_delegate_document_review_requires_content(home: Path) -> None:
    tool, _ = build()
    with pytest.raises(ValueError, match="content is required"):
        run_tool(tool, {"mode": "document_review", "content": "  "}, home)


def test_delegate_office_mode_routes_to_office_tool(home: Path) -> None:
    config = replace(Config(), office=OfficeConfig(enabled=False))
    tool, _ = build(config)
    with pytest.raises(OfficeError, match="disabled"):
        run_tool(tool, {"mode": "office", "format": "word", "objective": "Make a report"}, home)
