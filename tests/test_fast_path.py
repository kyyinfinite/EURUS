from __future__ import annotations

import asyncio
from pathlib import Path

from eurus.agent.loop import Agent
from eurus.agent.planner import assess_objective
from eurus.models import ToolResult
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def make_agent(router, registry=None, **kwargs):
    return Agent(
        router,
        registry or ToolRegistry([]),
        ToolContext(workspace=Path(".")),
        system_prompt="sys prompt body",
        max_steps=4,
        **kwargs,
    )


def run_agent(agent, text):
    async def go():
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def noop_registry():
    async def handler(arguments, context):
        return "ok"

    return ToolRegistry([Tool("noop", "no-op", EMPTY, handler)])


def test_greeting_sends_no_tool_schema():
    registry = noop_registry()
    router, transport = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, registry)
    run_agent(agent, "hallo")
    assert len(transport.calls) == 1
    payload = transport.calls[0].payload
    assert "tools" not in payload or not payload["tools"]


def test_tool_needing_message_sends_full_schema():
    registry = noop_registry()
    router, transport = scripted_router(streams=[openai_stream("done")])
    agent = make_agent(router, registry)
    run_agent(agent, "run the noop tool now")
    payload = transport.calls[0].payload
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert names == {"noop"}


def test_question_keeps_full_schema():
    registry = noop_registry()
    router, transport = scripted_router(streams=[openai_stream("It is 14")])
    agent = make_agent(router, registry)
    run_agent(agent, "what is 2+3*4")
    payload = transport.calls[0].payload
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert names == {"noop"}


def test_fast_path_falls_back_when_model_calls_tool():
    registry = noop_registry()
    router, transport = scripted_router(
        streams=[openai_stream("", calls=[("c1", "noop", {})]), openai_stream("hallo juga!")]
    )
    agent = make_agent(router, registry)
    events = run_agent(agent, "hallo")
    assert len(transport.calls) == 2
    first = transport.calls[0].payload
    second = transport.calls[1].payload
    assert "tools" not in first or not first["tools"]
    names = {tool["function"]["name"] for tool in second["tools"]}
    assert names == {"noop"}
    assert "tools" in second
    assert [message.role for message in agent.history] == ["system", "user", "assistant"]
    assert "hallo juga!" in agent.history[-1].content
    assert not [event for event in events if event.kind == "tool_call"]
    assert "text_reset" in {event.kind for event in events}
    assert agent.decisions[-2].name == "FAST_PATH_FALLBACK"
    assert events[-1].stop_reason == "completed"


def test_fast_path_disabled_sends_full_schema():
    registry = noop_registry()
    router, transport = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, registry, fast_path=False)
    run_agent(agent, "hallo")
    payload = transport.calls[0].payload
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert names == {"noop"}


def test_light_run_skips_recall_without_memory_cue():
    class RecordingLessons:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def relevant(self, task):
            self.asked.append(task)
            return []

    lessons = RecordingLessons()
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, lessons=lessons)
    run_agent(agent, "hallo")
    assert lessons.asked == []

    lessons2 = RecordingLessons()
    router2, _ = scripted_router(streams=[openai_stream("ok")])
    agent2 = make_agent(router2, lessons=lessons2)
    run_agent(agent2, "hallo, ingat namaku siapa?")
    assert lessons2.asked == ["hallo, ingat namaku siapa?"]

    lessons3 = RecordingLessons()
    router3, _ = scripted_router(streams=[openai_stream("Paris")])
    agent3 = make_agent(router3, lessons=lessons3)
    run_agent(agent3, "What is the capital of France?")
    assert lessons3.asked == ["What is the capital of France?"]


def test_light_run_learns_in_background():
    class SlowLearner:
        def __init__(self) -> None:
            self.calls = 0

        async def learn(self, task, transcript, approver):
            self.calls += 1
            await asyncio.sleep(0.05)
            return []

    learner = SlowLearner()
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, learner=learner)

    async def go():
        started = asyncio.get_running_loop().time()
        async for _ in agent.run("hallo"):
            pass
        done_time = asyncio.get_running_loop().time() - started
        assert agent._background_tasks
        await agent.drain_background()
        assert learner.calls == 1
        return done_time

    elapsed = asyncio.run(go())
    assert elapsed < 0.05


def test_conversational_profile_matches_greetings_only():
    assert assess_objective("hallo").conversational
    assert assess_objective("halo pagi!").conversational
    assert assess_objective("Selamat siang, makasih.").conversational
    assert not assess_objective("halo, buatkan file laporan").conversational
    assert not assess_objective("What is the capital of France?").conversational
    assert not assess_objective("fix the bug").conversational
    profile = assess_objective("halo, ingat namaku?")
    assert profile.is_simple and not profile.conversational
    assert not assess_objective("halo, carikan data terbaru").conversational


def test_greeting_result_still_reports_tool_results():
    registry = noop_registry()
    router, transport = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, registry)
    events = run_agent(agent, "hallo")
    assert [event.kind for event in events] == ["step", "text", "model_call", "done"]
    assert not [event for event in events if isinstance(event.tool_result, ToolResult)]
    assert len(transport.calls) == 1
