import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eurus.agent.loop import Agent, IterationBudget
from eurus.errors import ToolError
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec, BudgetTracker
from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.run_state import render_run_state
from eurus.storage.database import Database
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry

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


def run_agent(agent, text="go"):
    async def go():
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def failing_tool():
    async def handler(arguments, context):
        raise ToolError("boom")

    return Tool(name="flaky", description="f", parameters=EMPTY, handler=handler)


def test_render_includes_identity_status_and_objective():
    run = Run(owner_id="local", objective="ship the release")
    from eurus.runtime.state_machine import transition_run

    transition_run(run, RunStatus.RUNNING)
    text = render_run_state(run, BudgetSpec(), make_agent(scripted_router()[0]))
    assert "Run " in text and "running" in text
    assert "ship the release" in text
    assert text.startswith("<eurus-tier:run-state>") and text.endswith("</eurus-tier:run-state>")


def test_render_includes_budget_lines_and_tracker():
    router, _ = scripted_router(streams=[openai_stream("hi")])
    agent = make_agent(router)
    agent.runtime_budget_tracker = BudgetTracker(BudgetSpec(max_tool_calls=9, max_tokens=1000))
    agent.runtime_budget_tracker.start()
    agent.runtime_budget_tracker.add_usage(tokens=42)
    agent.deadline_at = datetime.now(UTC) + timedelta(seconds=30)
    run = Run(owner_id="local", objective="obj")
    text = render_run_state(
        run,
        BudgetSpec(max_tool_calls=9, max_tokens=1000),
        agent,
        tracker=agent.runtime_budget_tracker,
    )
    assert "Iterations: 0/10" in text
    assert "Model calls: 0/" in text
    assert "Tokens: 42/1000" in text
    assert "Tool calls: 0/9" in text
    assert "Wall-clock remaining:" in text


def test_render_reflects_decisions_replans_and_failures():
    router, _ = scripted_router(
        streams=[openai_stream("", calls=[("c1", "flaky", {})]), openai_stream("giving up")]
    )
    agent = make_agent(router, registry=ToolRegistry([failing_tool()]))
    run_agent(agent, "go")
    agent._replan_count = 2
    run = Run(owner_id="local", objective="obj")
    text = render_run_state(run, BudgetSpec(), agent)
    assert "Recent loop decisions:" in text
    assert "replans: 2" in text
    assert "Recent tool failures:" in text
    assert "flaky" in text


def test_render_warns_on_uncertain_mutation(tmp_path):
    store = ToolAttemptStore(Database(tmp_path / "a.sqlite3"))
    run = Run(owner_id="local", objective="obj")
    store.record(
        ToolExecutionOutcome(
            call_id="c1",
            tool_name="write_file",
            status=ToolExecutionStatus.TIMEOUT,
            call_key="write_file:{path}",
            run_id=run.run_id,
            risk_level="high",
            uncertain=True,
        )
    )
    text = render_run_state(
        run, BudgetSpec(), make_agent(scripted_router()[0]), attempt_store=store
    )
    assert "unknown outcome" in text
    assert "write_file -> timeout" in text


def test_render_includes_checkpoint_summary(tmp_path):
    run_store = RunStore(Database(tmp_path / "r.sqlite3"))
    run = Run(owner_id="local", objective="obj")
    run_store.create(run)
    run_store.save_checkpoint(
        run.run_id,
        {"iteration": 4, "side_effect_status": "settled"},
        checkpoint_type="runtime",
    )
    text = render_run_state(
        run, BudgetSpec(), make_agent(scripted_router()[0]), run_store=run_store
    )
    assert "last checkpoint at iteration 4" in text
    assert "side-effect status: settled" in text


def test_render_survives_broken_stores(tmp_path):
    class Bad:
        def __getattr__(self, name):
            raise RuntimeError("nope")

    run = Run(owner_id="local", objective="obj")
    text = render_run_state(
        run, BudgetSpec(), make_agent(scripted_router()[0]), attempt_store=Bad(), run_store=Bad()
    )
    assert "Run " in text


def test_engine_injects_run_state_into_model_prompt(tmp_path):
    router, transport = scripted_router(streams=[openai_stream("done")])
    agent = make_agent(router)
    run_store = RunStore(Database(tmp_path / "r.sqlite3"))
    engine = AgentEngine(agent, run_store=run_store)
    result = asyncio.run(engine.run("build the thing"))
    assert result.run.status is RunStatus.COMPLETED
    request_payloads = [call.payload for call in transport.calls]
    assert request_payloads
    assert any(
        "eurus-tier:run-state" in message.get("content", "")
        for payload in request_payloads
        for message in payload["messages"]
    )


def test_run_state_block_is_not_persisted_into_history():
    router, _ = scripted_router(streams=[openai_stream("done")])
    agent = make_agent(router)
    engine = AgentEngine(agent)
    asyncio.run(engine.run("obj"))
    assert not any("eurus-tier:run-state" in message.content for message in agent.history)


def test_child_run_state_mentions_parent():
    run = Run(owner_id="local", objective="child objective", parent_run_id="parent-123")
    text = render_run_state(run, BudgetSpec(), make_agent(scripted_router()[0]))
    assert "Parent run: parent-123" in text


def test_render_handles_waiting_status():
    run = Run(owner_id="local", objective="obj")
    from eurus.runtime.state_machine import transition_run

    transition_run(run, RunStatus.RUNNING)
    transition_run(run, RunStatus.WAITING_APPROVAL, stop_reason="awaiting_approval")
    text = render_run_state(run, BudgetSpec(), make_agent(scripted_router()[0]))
    assert "waiting_approval" in text
    assert "awaiting_approval" in text


def test_agent_readonly_accessors():
    router, _ = scripted_router(
        streams=[openai_stream("", calls=[("c1", "flaky", {})]), openai_stream("done")]
    )
    agent = make_agent(router, registry=ToolRegistry([failing_tool()]))
    assert agent.replan_count == 0
    assert agent.model_calls_used == 0
    assert agent.failure_counts == {}
    run_agent(agent, "go")
    assert agent.model_calls_used == 2
    assert any(
        key.startswith("flaky") and value[0] == 1 for key, value in agent.failure_counts.items()
    )


def test_iteration_budget_property_view_matches_render():
    agent = make_agent(scripted_router()[0])
    agent.iteration_budget = IterationBudget(limit=7)
    run = Run(owner_id="local", objective="obj")
    text = render_run_state(run, BudgetSpec(), agent)
    assert "Iterations: 0/7" in text
