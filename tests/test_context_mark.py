from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from eurus.agent import Agent
from eurus.agent.context import ExtractiveContextEngine
from eurus.models import Message
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


class RecordingLearner:
    def __init__(self) -> None:
        self.transcripts: list[list[Message]] = []

    async def learn(self, task: str, transcript: Any, approver: Any) -> list[str]:
        self.transcripts.append(list(transcript))
        return []


class QueueTool:
    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.name = "probe"

    def feed(self, *values: str) -> None:
        self.outputs.extend(values)

    def tool(self) -> Tool:
        async def handler(arguments: dict[str, Any], ctx: ToolContext) -> str:
            if self.outputs:
                return self.outputs.pop(0)
            return "default"

        return Tool(self.name, "probe tool", EMPTY, handler)


def make_agent(router: Any, registry: ToolRegistry, learner: RecordingLearner) -> Agent:
    return Agent(
        router,
        registry,
        ToolContext(workspace=Path("."), max_output=4000, timeout=10.0),
        system_prompt="sys",
        max_steps=5,
        learner=learner,
        context_tokens=400,
        context_engine=ExtractiveContextEngine(keep_recent=2),
    )


def run_agent(agent: Agent, text: str) -> list[Any]:
    async def go() -> list[Any]:
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def test_learn_and_summary_use_current_run_transcript_after_compaction() -> None:
    probe = QueueTool()
    probe.feed(*[("run-one-" + str(i) + " ") + "x" * 20 for i in range(4)])
    registry = ToolRegistry([probe.tool()])
    learner = RecordingLearner()
    first_streams = [openai_stream("", [(f"c1{i}", "probe", {"n": i})]) for i in range(4)] + [
        openai_stream("first done")
    ]
    router, _ = scripted_router(streams=first_streams)
    agent = make_agent(router, registry, learner)
    run_agent(agent, "objective one")
    mark_two = len(agent.history)
    assert mark_two >= 10, f"run one left unexpectedly short history: {mark_two}"

    probe.feed(
        "phase-two-1 " + "y" * 600,
        "phase-two-2 " + "y" * 600,
        "phase-two-3 " + "y" * 600,
        "phase-two-4 " + "y" * 600,
        "phase-two-5 " + "y" * 600,
    )
    second_streams = [openai_stream("", [(f"c2{i}", "probe", {"n": i})]) for i in range(5)]
    router2, _ = scripted_router(streams=second_streams)
    agent.router = router2
    events = run_agent(agent, "objective two")

    finished = [event for event in events if event.kind == "done"]
    assert finished, "run two produced no terminal event"
    summary = finished[-1].text
    assert "phase-two-4" in summary, f"run transcript lost after compaction: {summary!r}"
    assert "phase-two-5" in summary

    assert learner.transcripts, "learner never ran"
    final_transcript = learner.transcripts[-1]
    contents = [message.content for message in final_transcript]
    assert any("phase-two-4" in content for content in contents)
    assert any("phase-two-5" in content for content in contents)
    assert not any("run-one" in content for content in contents)


def test_mark_never_points_past_history_after_compaction() -> None:
    probe = QueueTool()
    probe.feed(*[("filler-" + str(i) + " ") + "z" * 600 for i in range(8)])
    registry = ToolRegistry([probe.tool()])
    learner = RecordingLearner()
    streams = [openai_stream("", [(f"c{i}", "probe", {"n": i})]) for i in range(8)]
    router, _ = scripted_router(streams=streams)
    agent = make_agent(router, registry, learner)
    events = run_agent(agent, "long objective")

    finished = [event for event in events if event.kind == "done"]
    assert finished
    assert finished[-1].stop_reason == "max_steps"
    assert finished[-1].text, "work summary was empty"
    assert "No completed assistant response" not in finished[-1].text
    assert learner.transcripts
    assert learner.transcripts[-1], "learn transcript was empty after compaction"
