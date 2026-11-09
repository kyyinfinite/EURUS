from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eurus.agent.loop import Agent
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry

TIMING_STAGES = {
    "prepare_run",
    "recall",
    "preflight_context",
    "plan_initially",
    "model_first_token",
    "model_total",
    "judge_completion",
    "learn_tail",
}


def make_agent(router, **kwargs):
    return Agent(
        router,
        kwargs.pop("registry", ToolRegistry([])),
        ToolContext(workspace=Path(".")),
        system_prompt="sys prompt body",
        max_steps=4,
        **kwargs,
    )


def run_agent(agent, text):
    async def go():
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def timing_events(events):
    return [event for event in events if event.kind == "timing"]


def test_simple_run_emits_all_timing_stages():
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, timing_events=True)
    events = run_agent(agent, "hallo")
    observed = [event.stage for event in timing_events(events)]
    assert TIMING_STAGES.issubset(set(observed))
    assert "recall" in observed and "prepare_run" in observed


def test_tool_run_emits_tools_step_timing():
    async def handler(arguments, context):
        return "ok"

    registry = ToolRegistry([Tool("noop", "no-op", {"type": "object"}, handler)])
    router, _ = scripted_router(
        streams=[openai_stream("", calls=[("c1", "noop", {})]), openai_stream("done")]
    )
    agent = make_agent(router, registry=registry, timing_events=True)
    events = run_agent(agent, "run the noop tool now")
    stages = [event.stage for event in timing_events(events)]
    assert "tools_step" in stages
    assert TIMING_STAGES.issubset(set(stages))


def test_timing_events_carry_numeric_durations_without_prompt_text():
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, timing_events=True)
    events = run_agent(agent, "hallo secr3t-marker")
    timings = timing_events(events)
    assert timings
    for event in timings:
        assert event.stage
        assert event.duration_ms >= 0.0
        assert event.text == ""
        assert event.tool_call is None
        assert event.tool_result is None
        assert "secr3t-marker" not in event.stage
        assert str(event.duration_ms) not in event.stage


def test_timing_event_ordering_recall_before_prepare_run():
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, timing_events=True)
    events = run_agent(agent, "hallo")
    stages = [event.stage for event in timing_events(events)]
    assert stages.index("recall") < stages.index("prepare_run")
    assert stages.index("model_first_token") < stages.index("model_total")


def test_timing_events_absent_by_default():
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router)
    events = run_agent(agent, "hallo")
    assert timing_events(events) == []
    assert "timing" not in {event.kind for event in events}


@pytest.mark.asyncio
async def test_engine_persists_timing_stage_events(tmp_path: Path):
    router, _ = scripted_router(streams=[openai_stream("hallo!")])
    agent = make_agent(router, timing_events=True)
    store_database = Database(tmp_path / "runtime.sqlite3")
    run_store = RunStore(store_database)
    engine = AgentEngine(agent, run_store=run_store, event_store=EventStore(store_database))
    result = await engine.run("hallo secr3t-marker")
    persisted = [event for event in result.runtime_events if event.event_type == "timing.stage"]
    assert persisted
    stages = {event.payload["stage"] for event in persisted}
    assert "prepare_run" in stages
    assert "model_total" in stages
    for event in persisted:
        assert isinstance(event.payload["duration_ms"], float)
        assert event.payload["duration_ms"] >= 0.0
        assert "secr3t-marker" not in str(event.payload)
        assert "sys prompt body" not in str(event.payload)
    stored = EventStore(store_database).list_for_run(result.run.run_id)
    assert any(event.event_type == "timing.stage" for event in stored)
