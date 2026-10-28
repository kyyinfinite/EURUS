from pathlib import Path

import pytest

from eurus.agent.loop import Agent
from eurus.errors import RateLimitError
from eurus.providers.testing import openai_message, openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus
from eurus.storage.database import Database
from eurus.storage.runs import RunStore
from eurus.tools.base import ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def make_agent(router, registry=None, **kwargs):
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)
    return Agent(
        router,
        registry or ToolRegistry([]),
        context,
        system_prompt="sys",
        max_steps=kwargs.pop("max_steps", 10),
        stream=kwargs.pop("stream", True),
        **kwargs,
    )


def allow_all(call, tool):
    return True


@pytest.mark.asyncio
async def test_engine_records_model_call_events_with_tokens():
    router, _ = scripted_router(streams=[openai_stream("hello")])
    engine = AgentEngine(make_agent(router))
    result = await engine.run("obj")
    types = [(event.event_type, event.payload) for event in result.runtime_events]
    model_events = [item for item in types if item[0] == "model.call.completed"]
    assert len(model_events) == 1
    assert model_events[0][1]["total_tokens"] == 15
    assert model_events[0][1]["finish"] == "stop"


@pytest.mark.asyncio
async def test_engine_records_plan_events_with_replan_flag():
    from eurus.agent.planner import TaskPlanner

    router, _ = scripted_router(streams=[openai_stream("done")])
    engine = AgentEngine(make_agent(router, planner=TaskPlanner()))
    result = await engine.run("fix it")
    plan_events = [event for event in result.runtime_events if event.event_type == "plan.updated"]
    assert len(plan_events) == 1
    assert plan_events[0].payload["is_replan"] is False
    assert plan_events[0].payload["sequence"] == 1


@pytest.mark.asyncio
async def test_engine_records_recovery_attempted():
    streams = [[RateLimitError("slow down", retryable=True)], openai_stream("final")]
    router, _ = scripted_router(streams=streams)
    engine = AgentEngine(make_agent(router, model_retry_limit=2))
    result = await engine.run("obj")
    recovery_events = [
        event for event in result.runtime_events if event.event_type == "recovery.attempted"
    ]
    assert len(recovery_events) == 1
    assert "retrying" in recovery_events[0].payload["reason"]


@pytest.mark.asyncio
async def test_engine_records_checkpoint_saved_events(tmp_path):
    run_store = RunStore(Database(tmp_path / "r.sqlite3"))
    router, _ = scripted_router(streams=[openai_stream("done")])
    engine = AgentEngine(make_agent(router), run_store=run_store, checkpoint_interval_steps=1)
    result = await engine.run("obj")
    checkpoint_events = [
        event for event in result.runtime_events if event.event_type == "checkpoint.saved"
    ]
    assert checkpoint_events
    assert checkpoint_events[-1].payload["side_effect_status"] == "settled"
    assert run_store.latest_checkpoint(result.run.run_id) is not None


@pytest.mark.asyncio
async def test_engine_events_carry_sequential_correlation():
    router, _ = scripted_router(streams=[openai_stream("a"), openai_stream("b")])
    engine = AgentEngine(make_agent(router))
    result = await engine.run("obj")
    sequences = [event.sequence_no for event in result.runtime_events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert result.runtime_events[0].event_type == "run.started"


@pytest.mark.asyncio
async def test_delegation_events_flow_through_context_sink(tmp_path):
    run_store = RunStore(Database(tmp_path / "r.sqlite3"))
    router, _ = scripted_router(
        streams=[
            openai_stream("", calls=[("c1", "delegate_task", {"task": "look around"})]),
            openai_stream("parent summary"),
        ],
        json_responses=[openai_message("child findings")],
    )
    registry = ToolRegistry([])
    from eurus.agent.delegation import delegation_tools

    tools = ToolRegistry(delegation_tools(router, registry))
    engine = AgentEngine(
        make_agent(router, registry=tools, approver=allow_all),
        run_store=run_store,
    )
    result = await engine.run(
        "delegate something",
        budget=BudgetSpec(max_child_runs=3),
    )
    types = [event.event_type for event in result.runtime_events]
    assert "delegation.child_registered" in types
    assert "delegation.child_completed" in types
    children = run_store.list_runs(limit=10)
    child = [run for run in children if run.parent_run_id == result.run.run_id]
    assert len(child) == 1
    assert child[0].status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_crashing_child_leaves_durable_failed_record(tmp_path):
    class ExplodingRouter:
        def stream(self, messages, tools=None):
            raise RuntimeError("provider down")

        async def chat(self, messages, tools=None, **kwargs):
            raise RuntimeError("provider down")

    run_store = RunStore(Database(tmp_path / "r.sqlite3"))
    parent_router, _ = scripted_router(
        streams=[
            openai_stream("", calls=[("c1", "delegate_task", {"task": "work"})]),
            openai_stream("child failed, reporting"),
        ]
    )
    from eurus.agent.delegation import delegation_tools

    tools = ToolRegistry(delegation_tools(ExplodingRouter(), ToolRegistry([])))
    agent = make_agent(parent_router, registry=tools, approver=allow_all)
    engine = AgentEngine(agent, run_store=run_store)
    result = await engine.run("delegate and crash", budget=BudgetSpec(max_child_runs=3))
    children = [
        run for run in run_store.list_runs(limit=10) if run.parent_run_id == result.run.run_id
    ]
    assert len(children) == 1
    assert children[0].status is RunStatus.FAILED
    assert children[0].stop_reason is not None
    assert children[0].stop_reason.startswith("engine_error")
