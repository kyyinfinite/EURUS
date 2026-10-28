from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from eurus.agent import Agent
from eurus.errors import RateLimitError
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.tools.base import ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def text_chunk(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def make_agent(router: Any) -> Agent:
    return Agent(
        router,
        ToolRegistry(),
        ToolContext(workspace=Path("."), max_output=1000, timeout=10.0),
        system_prompt="sys",
        max_steps=3,
        stream=True,
    )


def test_retry_emits_text_reset_and_clears_partial_text() -> None:
    router, _ = scripted_router(
        streams=[
            [text_chunk("partial answer"), RateLimitError("limit", retryable=True)],
            openai_stream("final answer"),
        ]
    )
    agent = make_agent(router)

    async def go() -> list[Any]:
        return [event async for event in agent.run("go")]

    events = asyncio.run(go())

    kinds = [event.kind for event in events]
    assert "text_reset" in kinds, f"no reset event between attempts: {kinds}"
    reset_index = kinds.index("text_reset")
    text_events = [event for event in events if event.kind == "text"]
    assert [event.text for event in text_events] == ["partial answer", "final answer"]
    after_reset = [event.text for event in events[reset_index + 1 :] if event.kind == "text"]
    assert "".join(after_reset) == "final answer"


def test_engine_output_has_no_duplicate_text_after_retry() -> None:
    router, _ = scripted_router(
        streams=[
            [text_chunk("partial answer"), RateLimitError("limit", retryable=True)],
            openai_stream("final answer"),
        ]
    )
    agent = make_agent(router)
    engine = AgentEngine(agent)

    async def go() -> Any:
        return await engine.run("go", budget=BudgetSpec(max_runtime_seconds=30.0))

    result = asyncio.run(go())

    assert result.output == "final answer"
    assert "partial answer" not in result.output


def test_engine_output_without_retry_is_unchanged() -> None:
    router, _ = scripted_router(streams=[openai_stream("clean answer")])
    agent = make_agent(router)
    engine = AgentEngine(agent)

    async def go() -> Any:
        return await engine.run("go", budget=BudgetSpec(max_runtime_seconds=30.0))

    result = asyncio.run(go())

    assert result.output == "clean answer"
    kinds = [event.kind for event in result.events]
    assert "text_reset" not in kinds
    assert result.run.stop_reason == "completed_unverified"
