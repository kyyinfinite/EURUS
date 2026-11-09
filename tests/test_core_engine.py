import asyncio
from pathlib import Path

import pytest

from eurus.agent.evaluation import (
    FailureKind,
    RecoveryAction,
    classify_failure,
    recovery_strategy,
)
from eurus.agent.loop import Agent, ProgressTracker
from eurus.agent.planner import TaskPlanner, assess_objective
from eurus.agent.verification import VerificationResult
from eurus.models import ToolResult
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def make_agent(router, registry=None, **kwargs):
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)
    return Agent(
        router,
        registry or ToolRegistry([]),
        context,
        system_prompt="sys",
        max_steps=kwargs.pop("max_steps", 12),
        stream=kwargs.pop("stream", True),
        **kwargs,
    )


def run_agent(agent, text="go"):
    async def go():
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def failing_tool_registry(output="error: boom"):
    async def handler(arguments, ctx):
        return output

    return ToolRegistry([Tool("flaky", "flaky tool", EMPTY, handler)])


def mutating_registry(name="write"):
    async def writer(arguments, ctx):
        return "written"

    return ToolRegistry([Tool(name, "mutating", EMPTY, writer, mutates=True)])


def test_failure_classification_covers_boundary_statuses():
    assert (
        classify_failure(ToolResult("c", "write", "error: unknown tool 'x'", True))
        is FailureKind.MISSING_CAPABILITY
    )
    assert (
        classify_failure(ToolResult("c", "write", "error: the user denied this action", True))
        is FailureKind.PERMISSION_DENIED
    )
    assert (
        classify_failure(
            ToolResult(
                "c",
                "write",
                "error: this action requires owner approval, but no approval provider "
                "is configured",
                True,
            )
        )
        is FailureKind.PERMISSION_DENIED
    )
    assert (
        classify_failure(ToolResult("c", "x", "error: timed out after 5s", True))
        is FailureKind.TIMEOUT
    )
    assert (
        classify_failure(ToolResult("c", "x", "error: invalid arguments: q is required", True))
        is FailureKind.INVALID_ACTION
    )
    assert (
        classify_failure(
            ToolResult("c", "x", "error: arguments are not valid JSON, send a JSON object", True)
        )
        is FailureKind.INVALID_ACTION
    )
    assert (
        classify_failure(ToolResult("c", "x", "error: tool call budget is exhausted", True))
        is FailureKind.RESOURCE_EXHAUSTED
    )
    assert (
        classify_failure(
            ToolResult("c", "x", "error: this exact call already failed 2 times (boom)", True)
        )
        is FailureKind.NONPRODUCTIVE
    )
    assert classify_failure(ToolResult("c", "x", "error: successful result", False)) is None


def test_uncertain_side_effect_requires_mutating_tool():
    result = ToolResult(
        "c",
        "write",
        "error: timed out after 1s; mutation tool write timed out; the side effect may or "
        "may not have been applied; verify the target state before repeating this call",
        True,
    )
    from eurus.tools.base import Tool

    mutating = Tool("write", "w", EMPTY, lambda a, c: asyncio.sleep(0, "x"), mutates=True)
    readonly = Tool("write", "w", EMPTY, lambda a, c: asyncio.sleep(0, "x"))
    assert classify_failure(result, mutating) is FailureKind.UNCERTAIN_SIDE_EFFECT
    assert classify_failure(result, readonly) is FailureKind.UNCERTAIN_SIDE_EFFECT
    plain_timeout = ToolResult("c", "x", "error: timed out after 5s", True)
    assert classify_failure(plain_timeout, readonly) is FailureKind.TIMEOUT


def test_recovery_strategy_escalates_after_repeated_tool_failure():
    first_action, first_guidance = recovery_strategy(FailureKind.TOOL_FAILURE, 0)
    second_action, _ = recovery_strategy(FailureKind.TOOL_FAILURE, 2)
    assert first_action is RecoveryAction.RETRY_MODIFIED
    assert second_action is RecoveryAction.SWITCH_TOOL
    assert "Retry once" in first_guidance
    action, guidance = recovery_strategy(FailureKind.PERMISSION_DENIED)
    assert action is RecoveryAction.AWAIT_APPROVAL
    assert "Do not repeat it unchanged" in guidance


def test_recovery_note_injected_into_next_model_request():
    streams = [
        openai_stream("", [("c1", "flaky", {})]),
        openai_stream("final answer"),
    ]
    router, transport = scripted_router(streams=streams)
    agent = make_agent(router, failing_tool_registry())
    events = run_agent(agent)
    assert events[-1].stop_reason == "completed"
    payload_messages = transport.calls[1].payload["messages"]
    volatile = [
        m["content"] for m in payload_messages if "eurus-tier:volatile" in (m.get("content") or "")
    ]
    assert any("Recovery guidance from runtime evaluation:" in content for content in volatile)
    assert any("[tool_failure" in content for content in volatile)
    assert any("adjusted" in content for content in volatile)


def test_recovery_note_cleared_for_clean_iterations():
    async def conditional(arguments, ctx):
        return "error: boom" if not arguments else "ok"

    registry = ToolRegistry([Tool("flaky", "flaky tool", EMPTY, conditional)])
    streams = [
        openai_stream("", [("c1", "flaky", {})]),
        openai_stream("", [("c2", "flaky", {"x": 1})]),
        openai_stream("done"),
    ]
    router, transport = scripted_router(streams=streams)
    agent = make_agent(router, registry)
    run_agent(agent)
    second_payload = transport.calls[2].payload["messages"]
    volatile = [
        m["content"] for m in second_payload if "eurus-tier:volatile" in (m.get("content") or "")
    ]
    assert not any("Recovery guidance from runtime evaluation:" in content for content in volatile)


def test_repeated_denials_block_execution():
    streams = [
        openai_stream("", [("c1", "write", {})]),
        openai_stream("", [("c2", "write", {})]),
        openai_stream("never reached"),
    ]
    router, _ = scripted_router(streams=streams)
    agent = make_agent(router, mutating_registry())
    events = run_agent(agent)
    assert events[-1].stop_reason == "blocked"
    assert "not completed" in events[-1].text
    names = [decision.name for decision in agent.decisions]
    assert "WAIT_EXTERNAL" in names
    assert "COMPLETED_UNVERIFIED" not in names


def test_engine_maps_blocked_to_waiting_input():
    streams = [
        openai_stream("", [("c1", "write", {})]),
        openai_stream("", [("c2", "write", {})]),
    ]
    router, _ = scripted_router(streams=streams)
    agent = make_agent(router, mutating_registry())
    result = asyncio.run(AgentEngine(agent).run("objective"))
    assert result.run.status is RunStatus.WAITING_INPUT
    assert result.run.stop_reason == "blocked"
    assert any(event.event_type == "run.blocked" for event in result.runtime_events)


def test_single_denial_does_not_block():
    streams = [
        openai_stream("", [("c1", "write", {})]),
        openai_stream("acknowledged"),
    ]
    router, _ = scripted_router(streams=streams)
    agent = make_agent(router, mutating_registry())
    events = run_agent(agent)
    assert events[-1].stop_reason == "completed"
    assert any(event.kind == "recovery" for event in events)
    assert any("permission_denied" in event.text for event in events if event.kind == "recovery")


def test_completion_verifier_requests_bounded_revision():
    streams = [openai_stream("draft one"), openai_stream("draft two"), openai_stream("final")]
    router, transport = scripted_router(streams=streams)
    from eurus.tools import default_registry

    calls = []

    async def verifier(objective, output):
        calls.append(output)
        satisfied = len(calls) >= 3
        summary = "criteria satisfied" if satisfied else "missing detail"
        return VerificationResult(satisfied, summary)

    agent = make_agent(router, default_registry(), completion_verifier=verifier)
    events = run_agent(agent)
    assert events[-1].stop_reason == "completed"
    recoveries = [event for event in events if event.kind == "recovery"]
    assert len(recoveries) == 2
    names = [decision.name for decision in agent.decisions]
    assert names.count("RECOVER") == 2
    assert names[-1] == "VERIFIED_SUCCESS"
    feedback = [
        message
        for message in agent.history
        if message.role == "user" and "Runtime verification feedback:" in message.content
    ]
    assert len(feedback) == 2
    assert "final" in calls[-1]


def test_completion_verifier_exhaustion_fails_the_run():
    streams = [openai_stream("attempt")] * 3
    router, _ = scripted_router(streams=streams)
    from eurus.tools import default_registry

    async def verifier(objective, output):
        return VerificationResult(False, "never satisfied")

    agent = make_agent(router, default_registry(), completion_verifier=verifier)
    events = run_agent(agent)
    assert events[-1].stop_reason == "verification_failed"
    names = [decision.name for decision in agent.decisions]
    assert names[-1] == "FAILED"


def test_plain_stop_without_verifier_is_completed_unverified():
    router, _ = scripted_router(streams=[openai_stream("plausible answer")])
    from eurus.tools import default_registry

    agent = make_agent(router, default_registry())
    events = run_agent(agent)
    assert events[-1].stop_reason == "completed"
    assert agent.decisions[-1].name == "COMPLETED_UNVERIFIED"


def test_adaptive_replans_rotate_strategies_then_fail():
    streams = [openai_stream("", [("c1", "noop", {})]) for _ in range(40)]
    router, _ = scripted_router(streams=streams)
    registry = ToolRegistry(
        [Tool("noop", "noop", EMPTY, lambda arguments, ctx: asyncio.sleep(0, "same"))]
    )
    agent = make_agent(
        router,
        registry,
        max_steps=40,
        max_parallel_tools=1,
        planner=TaskPlanner(),
    )
    events = run_agent(agent, "fix the parser bug")
    assert events[-1].stop_reason == "no_progress"
    assert agent.replan_count == 2
    plan_events = [event for event in events if event.kind == "plan"]
    assert len(plan_events) >= 3
    assert "Inspect workspace" in plan_events[0].text
    assert "Change the approach" in plan_events[1].text
    assert "smallest useful slice" in plan_events[2].text
    assert "Observed failure kinds" in plan_events[1].text
    names = [decision.name for decision in agent.decisions]
    assert names.count("REPLAN") == 2
    assert agent.task_state is not None and agent.task_state.status == "failed"


def test_max_replans_is_respected():
    streams = [openai_stream("", [("c1", "noop", {})]) for _ in range(30)]
    router, _ = scripted_router(streams=streams)
    registry = ToolRegistry(
        [Tool("noop", "noop", EMPTY, lambda arguments, ctx: asyncio.sleep(0, "same"))]
    )
    agent = make_agent(router, registry, max_steps=30, max_parallel_tools=1, max_replans=1)
    events = run_agent(agent)
    assert events[-1].stop_reason == "no_progress"
    assert agent.replan_count == 1


def test_simple_objective_skips_planning():
    router, _ = scripted_router(streams=[openai_stream("Paris")])
    from eurus.tools import default_registry

    agent = make_agent(router, default_registry(), planner=TaskPlanner())
    events = run_agent(agent, "What is the capital of France?")
    assert not [event for event in events if event.kind == "plan"]
    assert agent.task_state is None
    assert events[-1].stop_reason == "completed"


def test_step_verifier_advances_task_state():
    streams = [
        openai_stream("", [("a", "calculate", {"expression": "1+1"})]),
        openai_stream("", [("b", "calculate", {"expression": "2+2"})]),
        openai_stream("done"),
    ]
    router, _ = scripted_router(streams=streams)

    async def verifier(title, criteria, output):
        return VerificationResult(True, f"{title} confirmed")

    from eurus.tools import default_registry

    agent = make_agent(
        router,
        default_registry(),
        planner=TaskPlanner(),
        step_verifier=verifier,
    )
    events = run_agent(agent, "fix the calculation bug")
    state = agent.task_state
    assert state is not None
    statuses = [step.status for step in state.steps]
    assert statuses[0] == "succeeded"
    assert statuses[1] == "succeeded"
    assert "pending" in statuses
    assert events[-1].stop_reason == "completed"
    assert state.status == "completed"


def test_step_verifier_failure_forces_replan_and_marks_step_failed():
    streams = [
        openai_stream("", [("a", "calculate", {"expression": "1+1"})]),
        openai_stream("", [("b", "calculate", {"expression": "2+2"})]),
        openai_stream("", [("c", "calculate", {"expression": "3+3"})]),
        openai_stream("", [("d", "calculate", {"expression": "4+4"})]),
        openai_stream("giving up"),
    ]
    router, _ = scripted_router(streams=streams)

    async def verifier(title, criteria, output):
        return VerificationResult(False, "criteria not satisfied")

    from eurus.tools import default_registry

    agent = make_agent(
        router,
        default_registry(),
        planner=TaskPlanner(),
        step_verifier=verifier,
        max_steps=8,
        max_replans=1,
    )
    events = run_agent(agent, "fix the calculation bug")
    plan_events = [event for event in events if event.kind == "plan"]
    assert any("Replan strategy" in event.text for event in plan_events)
    assert any("alternate strategy" in event.text for event in plan_events)
    assert agent.replan_count == 1
    assert agent.task_state.steps[0].status == "failed"
    assert agent.task_state.steps[0].attempts >= 2
    assert "0/" in agent.task_state.summary or "steps verified" in agent.task_state.summary


def test_progress_tracker_prefix_growth_is_not_progress():
    tracker = ProgressTracker(window=4)
    assert tracker.record_iteration([ToolResult("c", "read", "line one", False)], "", "") is True
    assert tracker.record_iteration([ToolResult("c", "read", "line one", False)], "", "") is False
    assert (
        tracker.record_iteration([ToolResult("c", "read", "line one\nline two", False)], "", "")
        is False
    )
    assert tracker.record_iteration([ToolResult("c", "read", "line one", False)], "", "") is False
    assert tracker.record_iteration([ToolResult("c", "read", "line one", False)], "", "") is False
    assert tracker.stalled() is True


def test_progress_tracker_error_only_iteration_is_not_progress():
    tracker = ProgressTracker(window=3)
    tracker.record_iteration([ToolResult("c", "read", "ok", False)], "", "")
    assert tracker.record_iteration([ToolResult("c", "read", "error: one", True)], "", "") is False
    assert (
        tracker.record_iteration([ToolResult("c", "read", "error: two variant", True)], "", "")
        is False
    )


def test_delegation_unverified_result_adds_guidance():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router)
    result = ToolResult(
        "c1", "delegate_task", '{\n  "result": "claims",\n  "verified": false\n}', False
    )
    blocked, signal = agent._evaluate_results([result], 1)
    assert blocked is False
    assert signal == ""
    assert "Delegated results are marked unverified" in agent.recovery_note


def test_assess_objective_profiles():
    simple = assess_objective("What is the capital of France?")
    assert simple.is_simple and not simple.needs_tools
    task = assess_objective("Fix the failing parser test")
    assert not task.is_simple and task.needs_tools
    stale = assess_objective("What is the latest stable version?")
    assert stale.missing_info


def test_planner_replan_plan_strategies():
    planner = TaskPlanner()
    first = planner.replan_plan("refactor the module", replan_number=1)
    second = planner.replan_plan("refactor the module", replan_number=2)
    assert first.steps[0].title == "Re-read the failing evidence"
    assert second.steps[0].title == "Act on the smallest useful slice"
    with pytest.raises(ValueError):
        planner.replan_plan("x", replan_number=0)


def test_max_replans_validation():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    with pytest.raises(ValueError, match="max_replans"):
        make_agent(router, max_replans=0)


def test_engine_passes_with_loop_level_completion_verifier():
    streams = [openai_stream("structured answer")]
    router, _ = scripted_router(streams=streams)
    from eurus.tools import default_registry

    async def verifier(objective, output):
        return VerificationResult(True, "checked")

    agent = make_agent(router, default_registry(), completion_verifier=verifier)
    result = asyncio.run(AgentEngine(agent).run("objective"))
    assert result.run.status is RunStatus.COMPLETED
    assert result.run.stop_reason == "completed"


def test_blocked_run_is_resumable():
    from eurus.runtime.models import Run
    from eurus.runtime.recovery import RecoveryAction, classify_recovery

    run = Run(owner_id="test", objective="blocked objective", status=RunStatus.WAITING_INPUT)
    assert classify_recovery(run, {"state": {"side_effect_status": "settled"}}) is (
        RecoveryAction.RESUME_CHECKPOINT
    )
