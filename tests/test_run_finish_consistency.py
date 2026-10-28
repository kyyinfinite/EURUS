from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus.agent import Agent
from eurus.agent.planner import TaskPlanner
from eurus.models import Message, ToolCall
from eurus.providers.base import StreamEvent
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.budgets import BudgetSpec, BudgetTracker
from eurus.tools.base import Tool, ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}
CLOSED_STATES = {"completed", "failed", "cancelled"}


class RecordingLearner:
    def __init__(self) -> None:
        self.transcripts: list[list[Message]] = []

    async def learn(self, task: str, transcript: Any, approver: Any) -> list[str]:
        self.transcripts.append(list(transcript))
        return []


class SlowStreamRouter:
    async def stream(self, messages: Any, tools: Any = ()) -> Any:
        yield StreamEvent.text_delta("slow")
        await asyncio.sleep(1.0)

    async def chat(self, messages: Any, tools: Any = ()) -> Any:
        raise AssertionError("streaming is expected")


def noop_registry(output: str = "ok") -> ToolRegistry:
    async def noop(arguments: dict[str, Any], ctx: ToolContext) -> str:
        return output

    return ToolRegistry([Tool("noop", "noop tool", EMPTY, noop)])


def write_registry() -> ToolRegistry:
    async def write(arguments: dict[str, Any], ctx: ToolContext) -> str:
        return "written"

    return ToolRegistry([Tool("write", "write tool", EMPTY, write, mutates=True)])


def make_agent(
    router: Any,
    registry: ToolRegistry,
    learner: RecordingLearner,
    **overrides: Any,
) -> Agent:
    params: dict[str, Any] = {
        "system_prompt": "sys",
        "max_steps": 500,
        "planner": TaskPlanner(),
        "learner": learner,
    }
    params.update(overrides)
    return Agent(
        router,
        registry,
        ToolContext(workspace=Path("."), max_output=1000, timeout=10.0),
        **params,
    )


def drive(agent: Agent, text: str = "go") -> list[Any]:
    async def go() -> list[Any]:
        return [event async for event in agent.run(text)]

    return asyncio.run(go())


def assert_consistent(
    agent: Agent,
    learner: RecordingLearner,
    *,
    expected_reason: str,
    expect_closed: bool = True,
) -> None:
    assert learner.transcripts, f"learn did not run for stop_reason={expected_reason}"
    state = agent.task_state
    if expect_closed:
        assert state is not None, f"task state missing for stop_reason={expected_reason}"
        assert state.status in CLOSED_STATES, (
            f"task state left open ({state.status}) for stop_reason={expected_reason}"
        )
    decisions = [decision.name for decision in agent.decisions]
    assert decisions, f"no decision recorded for stop_reason={expected_reason}"


def test_budget_exhausted_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("", [("c1", "noop", {})])])
    agent = make_agent(router, noop_registry(), learner)
    tracker = BudgetTracker(BudgetSpec(max_runtime_seconds=60.0, max_tokens=1))
    tracker.start()
    tracker.tokens_used = 10
    agent.runtime_budget_tracker = tracker

    events = drive(agent)

    assert events[-1].kind == "done"
    assert events[-1].stop_reason == "budget_exhausted"
    assert_consistent(agent, learner, expected_reason="budget_exhausted")


def test_tool_budget_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(
        streams=[openai_stream("", [("c1", "noop", {}), ("c2", "noop", {})])]
    )
    agent = make_agent(router, noop_registry(), learner)
    agent.runtime_budget_tracker = BudgetTracker(
        BudgetSpec(max_runtime_seconds=60.0, max_tool_calls=0)
    )

    events = drive(agent)

    assert events[-1].stop_reason == "tool_budget"
    assert_consistent(agent, learner, expected_reason="tool_budget")


def test_model_call_budget_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("", [("c1", "noop", {})])])
    agent = make_agent(router, noop_registry(), learner, max_model_calls=1)

    events = drive(agent)

    assert events[-1].stop_reason == "model_call_budget"
    assert_consistent(agent, learner, expected_reason="model_call_budget")
    assert "noop" in events[-1].text or events[-1].text, "model budget summary missing"


def test_deadline_exceeded_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("never")])
    agent = make_agent(
        router, noop_registry(), learner, deadline_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    events = drive(agent)

    assert events[-1].stop_reason == "deadline_exceeded"
    assert_consistent(agent, learner, expected_reason="deadline_exceeded")


def test_iteration_budget_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("", [("c1", "noop", {})])])
    agent = make_agent(router, noop_registry(), learner, max_steps=1)

    events = drive(agent)

    assert events[-1].stop_reason == "max_steps"
    assert_consistent(agent, learner, expected_reason="max_steps")


def test_timeout_finishes_consistently() -> None:
    learner = RecordingLearner()
    agent = make_agent(SlowStreamRouter(), noop_registry(), learner, request_timeout=0.05)

    events = drive(agent)

    assert events[-1].stop_reason == "timeout"
    assert_consistent(agent, learner, expected_reason="timeout")


def test_interrupted_records_decision_and_learns() -> None:
    learner = RecordingLearner()
    registry = noop_registry()
    holder: dict[str, Any] = {}

    class InterruptingRouter:
        async def stream(self, messages: Any, tools: Any = ()) -> Any:
            holder["agent"].interrupt()
            yield StreamEvent.text_delta("starting")
            yield StreamEvent.tool(ToolCall("c1", "noop", {}))

        async def chat(self, messages: Any, tools: Any = ()) -> Any:
            raise AssertionError("streaming is expected")

    agent = make_agent(InterruptingRouter(), registry, learner)
    holder["agent"] = agent

    events = drive(agent)

    assert events[-1].stop_reason == "interrupted"
    assert learner.transcripts, "learn did not run for interrupted"
    decisions = [decision.name for decision in agent.decisions]
    assert "CANCELLED" in decisions
    state = agent.task_state
    if state is not None:
        assert state.status not in CLOSED_STATES or state.status == "cancelled"


def test_completed_finishes_consistently() -> None:
    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("all done")])
    agent = make_agent(router, noop_registry(), learner)

    events = drive(agent)

    assert events[-1].stop_reason == "completed"
    assert_consistent(agent, learner, expected_reason="completed")
    assert agent.task_state is not None
    assert agent.task_state.status == "completed"


def test_verification_failed_finishes_consistently() -> None:
    from eurus.agent.verification import VerificationResult

    learner = RecordingLearner()
    router, _ = scripted_router(streams=[openai_stream("answer") for _ in range(4)])

    async def verifier(objective: str, output: str) -> VerificationResult:
        return VerificationResult(False, "criteria not met")

    agent = make_agent(router, noop_registry(), learner, completion_verifier=verifier)

    events = drive(agent)

    assert events[-1].stop_reason == "verification_failed"
    assert_consistent(agent, learner, expected_reason="verification_failed")


def test_blocked_records_decision_and_learns() -> None:
    learner = RecordingLearner()
    streams = [openai_stream("", [("c1", "write", {})]) for _ in range(3)]
    router, _ = scripted_router(streams=streams)
    agent = make_agent(router, write_registry(), learner)

    events = drive(agent)

    assert events[-1].stop_reason == "blocked"
    assert learner.transcripts, "learn did not run for blocked"
    decisions = [decision.name for decision in agent.decisions]
    assert "WAIT_EXTERNAL" in decisions
    state = agent.task_state
    assert state is not None
    assert state.status not in CLOSED_STATES, "blocked run must keep task state resumable"


def test_budget_stop_summarizes_current_run_not_entire_history() -> None:
    learner = RecordingLearner()
    first_router, _ = scripted_router(
        streams=[openai_stream("first turn finished with old content")]
    )
    agent = make_agent(first_router, noop_registry(), learner)
    drive(agent, "first objective")

    second_router, _ = scripted_router(streams=[openai_stream("", [("c1", "noop", {})])])
    agent.router = second_router
    tracker = BudgetTracker(BudgetSpec(max_runtime_seconds=60.0, max_tokens=1))
    tracker.start()
    tracker.tokens_used = 10
    agent.runtime_budget_tracker = tracker

    events = drive(agent)

    assert events[-1].stop_reason == "budget_exhausted"
    assert "old content" not in events[-1].text
    transcript = learner.transcripts[-1]
    contents = [message.content for message in transcript]
    assert not any("first turn finished" in content for content in contents)
