from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from eurus.agent import Agent
from eurus.models import Message, ToolCall
from eurus.providers.anthropic import convert_messages as anthropic_messages
from eurus.providers.gemini import convert_messages as gemini_messages
from eurus.providers.openai_compat import message_to_wire
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}
CANCELLED = "error: cancelled before finishing"


def make_context() -> ToolContext:
    return ToolContext(workspace=Path("."), max_output=2000, timeout=10.0)


def slow_registry(seconds: float = 30.0) -> tuple[ToolRegistry, list[str]]:
    started: list[str] = []

    async def slow(arguments: dict[str, Any], ctx: ToolContext) -> str:
        started.append("go")
        await asyncio.sleep(seconds)
        return "late"

    return ToolRegistry([Tool("slow", "slow tool", EMPTY, slow)]), started


def make_agent(router: Any, registry: ToolRegistry, **overrides: Any) -> Agent:
    params: dict[str, Any] = {
        "system_prompt": "sys",
        "max_steps": 5,
        "request_timeout": 10.0,
    }
    params.update(overrides)
    return Agent(router, registry, make_context(), **params)


def assert_pairing(history: list[Message]) -> None:
    owned: set[str] = set()
    for message in history:
        if message.role == "assistant":
            owned.update(call.id for call in message.tool_calls)
    answered: set[str] = set()
    for message in history:
        if message.role == "tool" and message.tool_call_id:
            assert message.tool_call_id in owned, f"orphan tool result {message.tool_call_id}"
            answered.add(message.tool_call_id)
    missing = owned - answered
    assert not missing, f"tool calls without results: {sorted(missing)}"


def assert_openai_wire_valid(history: list[Message]) -> None:
    wire = [message_to_wire(message) for message in history]
    calls: set[str] = set()
    results: set[str] = set()
    for item in wire:
        for call in item.get("tool_calls", []):
            calls.add(call["id"])
        if item["role"] == "tool":
            results.add(item["tool_call_id"])
    assert results <= calls
    assert calls == results


def assert_anthropic_wire_valid(history: list[Message]) -> None:
    _, blocks = anthropic_messages(history)
    use_ids: set[str] = set()
    result_ids: set[str] = set()
    for block in blocks:
        if block["role"] != "assistant":
            continue
        for item in block["content"]:
            if item.get("type") == "tool_use":
                use_ids.add(item["id"])
    for block in blocks:
        if block["role"] != "user":
            continue
        content = block["content"]
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "tool_result":
                    result_ids.add(item["tool_use_id"])
    assert result_ids <= use_ids
    assert use_ids == result_ids


def assert_gemini_wire_valid(history: list[Message]) -> None:
    _, contents = gemini_messages(history)
    call_names: dict[str, str] = {}
    for content in contents:
        if content["role"] != "model":
            continue
        for part in content["parts"]:
            if "functionCall" in part:
                call_names[part["functionCall"]["name"]] = part["functionCall"]["name"]
    assert contents, "gemini conversion produced no contents"
    responses = [
        part
        for content in contents
        if content["role"] == "user"
        for part in content["parts"]
        if "functionResponse" in part
    ]
    for part in responses:
        name = part["functionResponse"]["name"]
        assert name in call_names, f"gemini functionResponse without call: {name}"


async def cancel_mid_tool(agent: Agent) -> None:
    events: list[str] = []

    async def consume() -> None:
        async for event in agent.run("go"):
            events.append(event.kind)

    task = asyncio.create_task(consume())
    for _ in range(500):
        if "tool_call" in events:
            break
        await asyncio.sleep(0.01)
    assert "tool_call" in events, "tool never started"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_parallel_cancel_still_records_tool_results() -> None:
    registry, _ = slow_registry()
    router, _ = scripted_router(
        streams=[openai_stream("", [("c1", "slow", {})]), openai_stream("done")]
    )
    agent = make_agent(router, registry)

    asyncio.run(cancel_mid_tool(agent))

    assert_pairing(agent.history)


def test_repair_history_synthesizes_missing_results() -> None:
    from eurus.agent.loop import repair_history

    history = [
        Message.system("sys"),
        Message.user("go"),
        Message.assistant("", (ToolCall("c1", "slow", {}), ToolCall("c2", "slow", {}))),
        Message.tool("c1", "ok", "slow"),
        Message.assistant("", (ToolCall("c3", "slow", {}),)),
        Message.tool("c9", "orphan", "slow"),
    ]

    changed = repair_history(history)

    assert changed is True
    assert_pairing(history)
    synthesized = [m for m in history if m.role == "tool" and m.content == CANCELLED]
    assert {m.tool_call_id for m in synthesized} == {"c2", "c3"}
    assert all(m.name == "slow" for m in synthesized)
    assert not any(m.tool_call_id == "c9" for m in history)


def test_repair_history_is_noop_on_valid_history() -> None:
    from eurus.agent.loop import repair_history

    history = [
        Message.system("sys"),
        Message.user("go"),
        Message.assistant("", (ToolCall("c1", "slow", {}),)),
        Message.tool("c1", "ok", "slow"),
        Message.assistant("done"),
    ]
    before = list(history)

    changed = repair_history(history)

    assert changed is False
    assert history == before


def test_next_run_after_cancel_builds_valid_provider_payloads() -> None:
    registry, _ = slow_registry()
    router, transport = scripted_router(
        streams=[openai_stream("", [("c1", "slow", {})]), openai_stream("all set")]
    )
    agent = make_agent(router, registry)

    async def scenario() -> None:
        await cancel_mid_tool(agent)
        async for _ in agent.run("again"):
            pass

    asyncio.run(scenario())

    assert_pairing(agent.history)
    assert_openai_wire_valid(agent.history)
    assert_anthropic_wire_valid(agent.history)
    assert_gemini_wire_valid(agent.history)
    last_payload = transport.calls[-1].payload
    tool_ids = {
        call["id"] for message in last_payload["messages"] for call in message.get("tool_calls", [])
    }
    result_ids = {
        message["tool_call_id"] for message in last_payload["messages"] if message["role"] == "tool"
    }
    assert tool_ids
    assert tool_ids == result_ids


@pytest.mark.asyncio
async def test_cancel_during_gather_leaves_no_dangling_call() -> None:
    registry, _ = slow_registry()
    router, _ = scripted_router(
        streams=[openai_stream("", [("c1", "slow", {}), ("c2", "slow", {})]), openai_stream("d")]
    )
    agent = make_agent(router, registry, max_parallel_tools=2)

    await cancel_mid_tool(agent)

    assert_pairing(agent.history)
    dangling = [
        call.id
        for message in agent.history
        if message.role == "assistant"
        for call in message.tool_calls
        if call.id
        not in {m.tool_call_id for m in agent.history if m.role == "tool" and m.tool_call_id}
    ]
    assert dangling == []
