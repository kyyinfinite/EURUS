from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eurus.agent.child_runtime import child_budget_spec
from eurus.agent.loop import Agent, IterationBudget
from eurus.agent.structured_planner import StructuredPlanner
from eurus.models import ToolCall, ToolResult
from eurus.providers.testing import openai_message, openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec, BudgetTracker, ChildBudgetCaps
from eurus.tools.base import Tool, ToolContext, ToolRegistry


def _registry() -> ToolRegistry:
    async def ok(arguments: dict[str, Any], context: ToolContext) -> str:
        return "ok"

    return ToolRegistry([Tool("calculate", "Calculate", {"type": "object"}, ok)])


def _agent(objective: str, streams: int = 1) -> Agent:
    router, _ = scripted_router(streams=[openai_stream("done") for _ in range(streams)])
    context = ToolContext(workspace=Path("."))
    agent = Agent(
        router,
        _registry(),
        context,
        system_prompt="test",
        iteration_budget=IterationBudget(limit=8),
    )
    return agent


def test_child_budget_spec_inherits_parent_caps() -> None:
    caps = ChildBudgetCaps(max_tokens=500, max_cost=0.75, max_model_calls=9)
    spec = child_budget_spec(max_iterations=10, timeout_seconds=60.0, parent_caps=caps)
    assert spec.max_tokens == 500
    assert spec.max_cost == 0.75
    assert spec.max_model_calls == 9
    assert spec.max_child_runs == 0


def test_child_budget_spec_without_caps() -> None:
    spec = child_budget_spec(max_iterations=10, timeout_seconds=60.0)
    assert spec.max_tokens is None
    assert spec.max_cost is None
    assert spec.max_model_calls is None


def test_child_budget_caps_from_tracker() -> None:
    spec = BudgetSpec(max_tokens=1000, max_cost=2.0, max_model_calls=10)
    tracker = BudgetTracker(spec)
    tracker.add_usage(tokens=300, cost=0.5)
    tracker.model_calls_used = 3
    caps = ChildBudgetCaps.from_tracker(tracker)
    assert caps.max_tokens == 700
    assert caps.max_cost == pytest.approx(1.5)
    assert caps.max_model_calls == 7
    assert not caps.exhausted()


def test_child_budget_caps_exhausted() -> None:
    assert ChildBudgetCaps(max_tokens=0).exhausted() is True
    assert ChildBudgetCaps(max_cost=0.0).exhausted() is True
    assert ChildBudgetCaps(max_model_calls=0).exhausted() is True
    assert ChildBudgetCaps(max_tokens=5).exhausted() is False
    assert ChildBudgetCaps().exhausted() is False


async def test_delegation_refused_when_parent_budget_exhausted(tmp_path: Path) -> None:
    from eurus.agent.delegation import PARENT_BUDGET_EXHAUSTED, run_subagent

    context = ToolContext(
        workspace=tmp_path,
        child_budget_caps=ChildBudgetCaps(max_tokens=0),
        timeout=5.0,
    )
    router, _ = scripted_router()
    with pytest.raises(ValueError, match=PARENT_BUDGET_EXHAUSTED):
        await run_subagent(router, _registry(), context, "do something")


async def test_tool_verb_objective_without_evidence_fails_verification() -> None:

    agent = _agent("create report.txt with the results", streams=4)

    class FixedVerifier:
        async def __call__(self, objective: str, content: str) -> Any:
            class Outcome:
                passed = False
                applies = False
                summary = "no deterministic check applies"

            return Outcome()

    agent.completion_verifier = FixedVerifier()
    events = [event async for event in agent.run("create report.txt with the results")]
    finishes = [event for event in events if event.kind == "done"]
    assert finishes
    assert finishes[-1].stop_reason == "verification_failed"


async def test_conversational_objective_completes_unverified() -> None:

    agent = _agent("say hi")

    class FixedVerifier:
        async def __call__(self, objective: str, content: str) -> Any:
            class Outcome:
                passed = False
                applies = False
                summary = "no deterministic check applies"

            return Outcome()

    agent.completion_verifier = FixedVerifier()
    events = [event async for event in agent.run("say hi")]
    finishes = [event for event in events if event.kind == "done"]
    assert finishes
    assert finishes[-1].stop_reason == "completed"


async def test_failure_counters_are_scoped_per_call() -> None:
    from eurus.tools.boundary import call_key

    agent = _agent("test")
    call_ok = ToolCall("c1", "calculate", {"expression": "1+1"})
    call_bad = ToolCall("c2", "calculate", {"expression": "nope"})
    tool = agent.registry.get("calculate")

    agent._track(call_ok, tool, None, ToolResult(call_ok.id, call_ok.name, "error: bad", True))
    agent._track(call_bad, tool, None, ToolResult(call_bad.id, call_bad.name, "error: nope", True))
    assert set(agent.failure_counts) == {call_key(call_ok), call_key(call_bad)}

    agent._track(call_ok, tool, None, ToolResult(call_ok.id, call_ok.name, "2", False))
    assert set(agent.failure_counts) == {call_key(call_bad)}


def test_recent_keys_bounded() -> None:
    agent = _agent("test")
    recent: list[str] = []
    for index in range(100):
        agent._prepare(ToolCall(f"c{index}", "calculate", {"expression": str(index)}), recent)
        recent = agent._recent_keys
    assert len(agent._recent_keys) <= 64


async def test_structured_replan_uses_model_with_failures() -> None:
    plan_json = (
        '{"objective": "goal", "assumptions": [], "open_questions": [], '
        '"steps": [{"step_id": "s1", "title": "New approach", "description": "", '
        '"depends_on": [], "expected_outputs": [], "success_criteria": "it works", '
        '"tool_categories": ["read"], "risk": "low", "verification": "check"}], '
        '"replan_conditions": []}'
    )
    router, transport = scripted_router(json_responses=[openai_message(plan_json)])
    planner = StructuredPlanner(router)
    plan = await planner.replan_plan(
        "goal",
        replan_number=1,
        failures=("search failed 2x: timeout",),
        completed_steps=("Old approach",),
    )
    assert planner.last_source == "model"
    assert plan.steps[0].title.startswith("New approach")
    assert transport.calls
    payload = transport.calls[0].payload
    prompt = payload["messages"][-1]["content"]
    assert "search failed 2x: timeout" in prompt
    assert "Old approach" in prompt


async def test_structured_replan_falls_back_on_bad_model_output() -> None:
    router, _ = scripted_router(json_responses=[openai_message("not json at all")])
    planner = StructuredPlanner(router)
    plan = await planner.replan_plan("write a report", replan_number=1, failures=("boom",))
    assert planner.last_source == "deterministic"
    assert plan.steps


def test_team_coordinator_falls_back_to_general_role(tmp_path: Path) -> None:
    from eurus.agent.team_coordinator import TeamCoordinator
    from eurus.multiagent.contracts import TaskSpec

    class FakeRegistry:
        def select(self, *, capability: str | None = None, role: str | None = None):
            if role == "analyst":
                return ["analyst-specialist"]
            return []

    class FakeOrchestrator:
        pass

    coordinator = TeamCoordinator.__new__(TeamCoordinator)
    coordinator.registry = FakeRegistry()
    coordinator.orchestrator = FakeOrchestrator()
    task = TaskSpec(
        task_id="t1",
        mission_id="m1",
        objective="do an unusual thing",
        task_key="report",
        required_capabilities=("unobtainium",),
    )
    assert coordinator.select_role(task) == "analyst"
