"""Phase 1.4: run-state rendered once per step from in-memory evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path

from eurus.agent.loop import Agent
from eurus.models import Usage
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.runtime.run_state import render_run_state
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def _noop_registry():
    async def handler(arguments, context):
        return "ok"

    return ToolRegistry([Tool("noop", "n", {"type": "object", "properties": {}}, handler)])


def _multi_step(count: int):
    streams = [openai_stream("", calls=[(f"c{i}", "noop", {"n": i})]) for i in range(count - 1)]
    streams.append(openai_stream("final answer"))
    return scripted_router(streams=streams)


def make_agent(router, registry=None, **kwargs):
    return Agent(
        router,
        registry or ToolRegistry([]),
        ToolContext(workspace=Path("."), max_output=1000, timeout=5.0),
        system_prompt="sys",
        max_steps=10,
        stream=True,
        **kwargs,
    )


def failing_tool():
    async def handler(arguments, context):
        from eurus.errors import ToolError

        raise ToolError("boom")

    return Tool(name="flaky", description="f", parameters=EMPTY, handler=handler)


def test_engine_renders_run_state_once_per_step(monkeypatch):
    router, _ = _multi_step(3)
    agent = make_agent(router, registry=_noop_registry())
    calls = {"count": 0}
    original = render_run_state

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("eurus.runtime.engine.render_run_state", counting)
    engine = AgentEngine(agent)
    asyncio.run(engine.run("multi step"))
    assert agent._last_decisions  # sanity: the loop ran
    # 3 steps -> 3 renders (one per step), not 6 (preflight + request each step)
    assert calls["count"] == 3


def test_step_invalidation_updates_budget_numbers():
    router, transport = _multi_step(2)
    agent = make_agent(router, registry=_noop_registry())
    engine = AgentEngine(agent)
    asyncio.run(engine.run("obj"))
    payloads = [call.payload for call in transport.calls]
    assert len(payloads) == 2
    first_state = next(
        message["content"]
        for message in payloads[0]["messages"]
        if "eurus-tier:run-state" in (message.get("content") or "")
    )
    second_state = next(
        message["content"]
        for message in payloads[1]["messages"]
        if "eurus-tier:run-state" in (message.get("content") or "")
    )
    # iteration_budget.consume() runs before the prompt is built, so the
    # first request already renders the step as used.
    assert "Iterations: 1/" in first_state
    assert "Iterations: 2/" in second_state


def test_failure_evidence_from_memory_reaches_prompt():
    router, transport = scripted_router(
        streams=[openai_stream("", calls=[("c1", "flaky", {})]), openai_stream("giving up")]
    )
    agent = make_agent(router, registry=ToolRegistry([failing_tool()]))
    engine = AgentEngine(agent)
    asyncio.run(engine.run("obj"))
    second = transport.calls[1].payload
    run_state = next(
        message["content"]
        for message in second["messages"]
        if "eurus-tier:run-state" in (message.get("content") or "")
    )
    assert "flaky -> failed" in run_state


def test_attempt_evidence_renders_without_store():
    from eurus.runtime.models import Run
    from eurus.runtime.run_state import AttemptEvidence

    run = Run(owner_id="local", objective="obj")
    text = render_run_state(
        Run.__new__(Run) if False else run,
        BudgetSpec(),
        make_agent(scripted_router()[0]),
        attempt_evidence=AttemptEvidence(
            uncertain_mutation=True,
            failures=(("write_file", "timeout"),),
        ),
    )
    assert "unknown outcome" in text
    assert "write_file -> timeout" in text


def test_checkpoint_info_renders_without_store():
    from eurus.runtime.models import Run
    from eurus.runtime.run_state import CheckpointInfo

    run = Run(owner_id="local", objective="obj")
    text = render_run_state(
        run,
        BudgetSpec(),
        make_agent(scripted_router()[0]),
        checkpoint_info=CheckpointInfo(iteration=7, side_effect_status="settled"),
    )
    assert "last checkpoint at iteration 7" in text
    assert "side-effect status: settled" in text


def test_usage_property_view_still_renders():
    router, _ = scripted_router(streams=[openai_stream("hi")])
    agent = make_agent(router)
    agent.usage = Usage(3, 4)
    from eurus.runtime.models import Run

    text = render_run_state(Run(owner_id="l", objective="o"), BudgetSpec(), agent)
    assert text.startswith("<eurus-tier:run-state>")
