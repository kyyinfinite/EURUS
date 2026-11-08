from __future__ import annotations

from pathlib import Path

import pytest

from eurus.agent.loop import Agent, IterationBudget
from eurus.agent.planner import PlanStep, TaskPlan
from eurus.agent.structured_planner import PlannedStep, StructuredPlan, structured_to_task_plan
from eurus.agent.task_state import TaskState
from eurus.agent.verification import RuntimeVerificationService
from eurus.providers.testing import openai_message, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus
from eurus.storage.runs import RunStore
from eurus.tools.base import ToolContext, ToolRegistry


def _structured_plan() -> StructuredPlan:
    return StructuredPlan(
        objective="ship the feature",
        assumptions=(),
        open_questions=(),
        steps=(
            PlannedStep(
                step_id="s1",
                title="Inspect files",
                description="Look around",
                depends_on=(),
                expected_outputs=("app.py",),
                tool_categories=("read",),
                risk="low",
                success_criteria="app.py contents are known",
                verification="file read",
            ),
            PlannedStep(
                step_id="s2",
                title="Edit file",
                description="Change code",
                depends_on=("s1",),
                expected_outputs=("patch",),
                tool_categories=("edit",),
                risk="high",
                success_criteria="app.py modified with the feature",
                verification="diff review",
            ),
        ),
        replan_conditions=(),
    )


def test_structured_plan_fields_survive_conversion_to_task_plan():
    plan = structured_to_task_plan(_structured_plan())
    assert [step.risk for step in plan.steps] == ["low", "high"]
    assert plan.steps[0].expected_outputs == ("app.py",)
    assert plan.steps[0].tool_categories == ("read",)
    assert plan.steps[1].risk == "high"


def test_task_state_round_trips_rich_step_fields():
    plan = structured_to_task_plan(_structured_plan())
    state = TaskState.from_plan(plan)
    state.steps[0].status = "succeeded"
    restored = TaskState.from_dict(state.to_dict())
    assert restored.steps[0].status == "succeeded"
    assert restored.steps[0].expected_outputs == ("app.py",)
    assert restored.steps[0].risk == "low"
    assert restored.steps[1].tool_categories == ("edit",)


def test_task_state_rejects_invalid_risk_and_outputs():
    with pytest.raises(ValueError, match="risk"):
        TaskState.from_dict(
            {
                "goal": "g",
                "steps": [
                    {
                        "number": 1,
                        "title": "t",
                        "success_criteria": "c",
                        "risk": "extreme",
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="expected_outputs"):
        TaskState.from_dict(
            {
                "goal": "g",
                "steps": [
                    {
                        "number": 1,
                        "title": "t",
                        "success_criteria": "c",
                        "expected_outputs": "not-a-list",
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_step_verifier_fails_when_required_artifact_is_absent():
    service = RuntimeVerificationService(workspace=Path("."), transcript=lambda: ())
    result = await service.verify_step(
        "Edit file",
        "the file `app.py` must contain the new function",
        "I looked at the project and it seems fine overall.",
    )
    assert result.passed is False
    assert result.evidence[0].startswith("missing_step_requirement:app.py")


@pytest.mark.asyncio
async def test_step_verifier_passes_when_requirement_is_mentioned():
    service = RuntimeVerificationService(workspace=Path("."), transcript=lambda: ())
    result = await service.verify_step(
        "Edit file",
        "the file `app.py` must contain the new function",
        "Added add_feature() to app.py; the function is exported.",
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_step_verifier_ignores_criteria_without_concrete_artifacts():
    service = RuntimeVerificationService(workspace=Path("."), transcript=lambda: ())
    result = await service.verify_step(
        "Summarize findings",
        "the summary must be clear and concise",
        "The codebase uses two patterns for retries.",
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_plan_note_renders_step_progress(tmp_path):
    router, _ = scripted_router(json_responses=[openai_message("done")])
    agent = Agent(
        router,
        ToolRegistry([]),
        ToolContext(workspace=tmp_path),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=10),
        stream=False,
    )
    plan = TaskPlan(
        goal="g",
        steps=(
            PlanStep(1, "first", "criteria one"),
            PlanStep(2, "second", "criteria two"),
        ),
    )
    agent._active_plan = plan
    agent._task_state = TaskState.from_plan(plan)
    agent._task_state.steps[0].status = "succeeded"
    agent._task_state.steps[1].status = "running"
    rendered = agent._render_plan(plan)
    assert "1. first — criteria one [done]" in rendered
    assert "2. second — criteria two [current]" in rendered


@pytest.mark.asyncio
async def test_replan_preserves_completed_step_guidance(tmp_path):
    router, _ = scripted_router(json_responses=[openai_message("done")])
    agent = Agent(
        router,
        ToolRegistry([]),
        ToolContext(workspace=tmp_path),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=10),
        stream=False,
    )
    plan = TaskPlan(goal="g", steps=(PlanStep(1, "first", "criteria one"),))
    agent._active_plan = plan
    state = TaskState.from_plan(plan)
    state.steps[0].status = "succeeded"
    agent._task_state = state
    note = await agent._replan_async("g")
    assert "first" in note
    assert "Do not repeat them" in note


@pytest.mark.asyncio
async def test_engine_checkpoints_persist_task_state_progress(tmp_path):
    async def handler(arguments, context):
        return "done"

    from eurus.tools.base import Tool

    tool = Tool(
        "read_file",
        "read",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler,
    )
    router, _ = scripted_router(
        json_responses=[
            openai_message("", calls=[("c1", "read_file", {"path": "app.py"})]),
            openai_message("final answer"),
        ]
    )
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = Agent(
        router,
        ToolRegistry([tool]),
        ToolContext(workspace=tmp_path),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=10),
        stream=False,
    )
    engine = AgentEngine(agent, run_store=run_store)
    result = await engine.run("objective with tools")
    assert result.run.status is RunStatus.COMPLETED
    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    state = checkpoint.get("state", {})
    assert isinstance(state, dict)
    assert "task_state" in state or "iteration" in state
    events = [event.event_type for event in result.runtime_events]
    assert "run.completed" in events
