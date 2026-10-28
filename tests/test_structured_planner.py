from __future__ import annotations

import asyncio
import json

import pytest

from eurus.agent.planner import PlanStep, TaskPlanner
from eurus.agent.structured_planner import (
    PlanValidationError,
    StructuredPlanner,
    parse_structured_plan,
    structured_to_task_plan,
    validate_plan,
)
from eurus.models import Message, Usage
from eurus.providers.base import ChatResponse


def _valid_payload() -> dict:
    return {
        "objective": "Fix the failing tests in window.py",
        "assumptions": ["tests run with pytest"],
        "open_questions": [],
        "steps": [
            {
                "step_id": "s1",
                "title": "Reproduce failures",
                "description": "Run the failing tests",
                "depends_on": [],
                "expected_outputs": ["failure output"],
                "success_criteria": "exact failing assertions are visible",
                "tool_categories": ["test"],
                "risk": "low",
                "verification": "inspect test output",
            },
            {
                "step_id": "s2",
                "title": "Apply fix",
                "description": "Edit window.py",
                "depends_on": ["s1"],
                "expected_outputs": ["patched window.py"],
                "success_criteria": "the failing assertions pass",
                "tool_categories": ["edit", "test"],
                "risk": "medium",
                "verification": "run tests again",
            },
        ],
        "replan_conditions": ["tests still fail after the fix"],
    }


def test_valid_model_plan_parses_and_converts() -> None:
    plan = parse_structured_plan("fix tests", _valid_payload())
    assert plan.objective == "Fix the failing tests in window.py"
    assert [step.step_id for step in plan.steps] == ["s1", "s2"]
    task_plan = structured_to_task_plan(plan)
    assert task_plan.steps[0].number == 1
    assert "s1" in task_plan.steps[0].title
    assert task_plan.steps[1].success_criteria == "the failing assertions pass"


def test_unknown_top_level_keys_are_rejected() -> None:
    payload = _valid_payload() | {"grant_permissions": ["shell"]}
    with pytest.raises(PlanValidationError, match="unknown key"):
        parse_structured_plan("goal", payload)


def test_unknown_step_keys_are_rejected() -> None:
    payload = _valid_payload()
    payload["steps"][0]["approved"] = True
    with pytest.raises(PlanValidationError, match="unknown key"):
        parse_structured_plan("goal", payload)


def test_dependency_cycles_are_rejected() -> None:
    payload = _valid_payload()
    payload["steps"][0]["depends_on"] = ["s2"]
    with pytest.raises(PlanValidationError, match="cycle"):
        parse_structured_plan("goal", payload)


def test_unknown_dependency_is_rejected() -> None:
    payload = _valid_payload()
    payload["steps"][1]["depends_on"] = ["s9"]
    with pytest.raises(PlanValidationError, match="unknown step"):
        parse_structured_plan("goal", payload)


def test_self_dependency_is_rejected() -> None:
    payload = _valid_payload()
    payload["steps"][0]["depends_on"] = ["s1"]
    with pytest.raises(PlanValidationError, match="itself"):
        parse_structured_plan("goal", payload)


def test_empty_and_oversized_step_lists_are_rejected() -> None:
    payload = _valid_payload()
    payload["steps"] = []
    with pytest.raises(PlanValidationError, match="1 to 8 steps"):
        parse_structured_plan("goal", payload)
    payload["steps"] = [
        {"step_id": f"s{i}", "title": f"t{i}", "success_criteria": "c"} for i in range(9)
    ]
    with pytest.raises(PlanValidationError, match="1 to 8 steps"):
        parse_structured_plan("goal", payload)


def test_invalid_risk_and_category_are_rejected() -> None:
    payload = _valid_payload()
    payload["steps"][0]["risk"] = "extreme"
    with pytest.raises(PlanValidationError, match="invalid risk"):
        parse_structured_plan("goal", payload)
    payload = _valid_payload()
    payload["steps"][0]["tool_categories"] = ["grant_root"]
    with pytest.raises(PlanValidationError, match="unknown tool category"):
        parse_structured_plan("goal", payload)


def test_missing_success_criteria_is_rejected() -> None:
    payload = _valid_payload()
    del payload["steps"][0]["success_criteria"]
    with pytest.raises(PlanValidationError, match="success_criteria"):
        parse_structured_plan("goal", payload)


class StubRouter:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def chat(self, messages, tools=(), **kwargs):
        self.calls += 1
        return ChatResponse(
            message=Message.assistant(self.content), finish_reason="stop", usage=Usage()
        )


def test_model_planner_uses_validated_model_output() -> None:
    router = StubRouter(json.dumps(_valid_payload()))
    planner = StructuredPlanner(router, fallback=TaskPlanner())
    plan = asyncio.run(planner.create_plan_async("fix the failing tests"))
    assert planner.last_source == "model"
    assert any("Reproduce failures" in step.title for step in plan.steps)


def test_model_planner_falls_back_on_invalid_json() -> None:
    router = StubRouter("I will fix the tests, no JSON here")
    planner = StructuredPlanner(router, fallback=TaskPlanner())
    plan = asyncio.run(planner.create_plan_async("fix the bug in cart.py"))
    assert planner.last_source == "deterministic"
    assert plan.steps
    assert all("Reproduce failures" not in step.title for step in plan.steps)


def test_model_planner_falls_back_on_router_failure() -> None:
    class BrokenRouter:
        async def chat(self, messages, tools=(), **kwargs):
            raise RuntimeError("provider down")

    planner = StructuredPlanner(BrokenRouter(), fallback=TaskPlanner())
    plan = asyncio.run(planner.create_plan_async("fix the bug in cart.py"))
    assert planner.last_source == "deterministic"
    assert plan.steps


def test_model_planner_falls_back_on_schema_violation() -> None:
    payload = _valid_payload()
    payload["steps"][0]["depends_on"] = ["missing"]
    router = StubRouter(json.dumps(payload))
    planner = StructuredPlanner(router, fallback=TaskPlanner())
    plan = asyncio.run(planner.create_plan_async("fix the bug"))
    assert planner.last_source == "deterministic"
    assert plan.steps


def test_deterministic_planner_untouched_for_simple_fallback() -> None:
    planner = TaskPlanner()
    plan = planner.create_plan("fix the bug in the payment module")
    assert plan.steps
    assert all(isinstance(step, PlanStep) for step in plan.steps)


def test_topological_order_handles_unordered_dependencies() -> None:
    payload = _valid_payload()
    payload["steps"] = [
        {
            "step_id": "s3",
            "title": "Report",
            "success_criteria": "summary written",
            "depends_on": ["s2"],
        },
        {
            "step_id": "s1",
            "title": "Investigate",
            "success_criteria": "cause identified",
            "depends_on": [],
        },
        {
            "step_id": "s2",
            "title": "Fix",
            "success_criteria": "bug fixed",
            "depends_on": ["s1"],
        },
    ]
    plan = parse_structured_plan("goal", payload)
    task_plan = structured_to_task_plan(plan)
    order = [step.title.split("[")[-1].rstrip("]") for step in task_plan.steps]
    assert order.index("s1") < order.index("s2") < order.index("s3")


def test_validate_plan_flags_duplicate_ids() -> None:
    from eurus.agent.structured_planner import PlannedStep, StructuredPlan

    plan = StructuredPlan(
        objective="goal",
        steps=(
            PlannedStep(step_id="s1", title="a", success_criteria="c"),
            PlannedStep(step_id="s1", title="b", success_criteria="c"),
        ),
    )
    violations = validate_plan(plan)
    assert any("duplicate" in item for item in violations)
