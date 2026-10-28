import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.agent.loop import Agent, IterationBudget, ProgressTracker
from eurus.agent.planner import TaskPlanner
from eurus.errors import ProviderError, RateLimitError
from eurus.models import ToolResult
from eurus.providers.base import StreamEvent
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.contracts import ToolExecutionStatus
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

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


def test_progress_tracker_detects_stall_after_window():
    tracker = ProgressTracker(window=4)
    result = ToolResult("c", "read", "same output", False)
    assert tracker.record_iteration([result], "", "") is True
    for _ in range(4):
        progressed = tracker.record_iteration([result], "", "")
    assert progressed is False
    assert tracker.stalled() is True


def test_progress_tracker_new_tool_output_counts_as_progress():
    tracker = ProgressTracker(window=4)
    first = ToolResult("c", "read", "output one", False)
    second = ToolResult("c", "read", "output two", False)
    assert tracker.record_iteration([first], "", "") is True
    assert tracker.record_iteration([second], "", "") is True
    assert tracker.stalled() is False


def test_progress_tracker_rejects_small_window():
    with pytest.raises(ValueError, match="progress window"):
        ProgressTracker(window=1)


def test_planner_emits_plan_event_and_volatile_prompt():
    router, transport = scripted_router(streams=[openai_stream("hello")])
    agent = make_agent(router, planner=TaskPlanner())
    events = run_agent(agent, "fix the bug")
    plan_events = [event for event in events if event.kind == "plan"]
    assert plan_events and "Plan for:" in plan_events[0].text
    payload = transport.calls[0].payload["messages"]
    assert any("eurus-tier:volatile" in message["content"] for message in payload)


def test_retryable_provider_error_recovers():
    streams = [[RateLimitError("slow down", retryable=True)], openai_stream("final")]
    router, transport = scripted_router(streams=streams)
    agent = make_agent(router, model_retry_limit=2)
    events = run_agent(agent)
    recoveries = [event for event in events if event.kind == "recovery"]
    assert recoveries
    assert events[-1].stop_reason == "completed"
    assert "final" in agent.history[-1].content
    assert any(event.kind == "text" and "final" in event.text for event in events)


def test_streaming_delivers_first_chunk_before_provider_finishes_in_order():
    async def scenario():
        release = asyncio.Event()
        provider_finished = asyncio.Event()

        class IncrementalRouter:
            async def stream(self, messages, tools=()):
                yield StreamEvent.text_delta("first")
                await release.wait()
                yield StreamEvent.text_delta(" second")
                provider_finished.set()

            async def chat(self, messages, tools=()):
                raise AssertionError("streaming is expected")

        agent = make_agent(IncrementalRouter())
        events = agent.run("go")
        collected = []
        async for event in events:
            collected.append(event)
            if event.kind == "text":
                break
        assert collected[-1].text == "first"
        assert not provider_finished.is_set()
        release.set()
        collected.extend([event async for event in events])
        chunks = [event.text for event in collected if event.kind == "text"]
        assert chunks == ["first", " second"]

    asyncio.run(scenario())


def test_non_retryable_provider_error_propagates():
    router, _ = scripted_router(streams=[[ProviderError("bad request", retryable=False)]])
    agent = make_agent(router, model_retry_limit=3)
    with pytest.raises(ProviderError):
        run_agent(agent)


def test_retry_exhaustion_propagates_after_bounded_attempts():
    router, transport = scripted_router(
        streams=[[RateLimitError("limit", retryable=True)] for _ in range(5)]
    )
    agent = make_agent(router, model_retry_limit=2)
    with pytest.raises(RateLimitError):
        run_agent(agent)
    assert len(transport.calls) == 3


def test_model_call_budget_stops_the_loop():
    streams = [openai_stream("", [("c1", "noop", {})]) for _ in range(5)]
    router, _ = scripted_router(streams=streams)
    registry = ToolRegistry(
        [Tool("noop", "noop", EMPTY, lambda arguments, ctx: asyncio.sleep(0, "ok"))]
    )
    agent = make_agent(registry=registry, router=None)
    agent.router = router
    agent.max_model_calls = 2
    events = run_agent(agent)
    assert events[-1].stop_reason == "model_call_budget"


def test_deadline_exceeded_stops_the_loop():
    router, _ = scripted_router(streams=[openai_stream("late")])
    agent = make_agent(router, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    events = run_agent(agent)
    assert events[-1].stop_reason == "deadline_exceeded"


def test_request_timeout_is_clamped_to_deadline():
    class SlowStreamRouter:
        async def stream(self, messages, tools=()):
            yield StreamEvent.text_delta("late")
            await asyncio.sleep(1.0)

        async def chat(self, messages, tools=()):
            raise AssertionError("streaming is expected")

    agent = make_agent(SlowStreamRouter(), deadline_at=datetime.now(UTC) + timedelta(seconds=0.05))
    events = run_agent(agent)
    assert events[-1].stop_reason == "timeout"


def test_decisions_track_loop_choices():
    streams = [
        openai_stream("", [("c1", "calculate", {"expression": "1+1"})]),
        openai_stream("done"),
    ]
    router, _ = scripted_router(streams=streams)
    from eurus.tools import default_registry

    agent = make_agent(router, default_registry())
    run_agent(agent)
    kinds = [decision.name for decision in agent.decisions]
    assert "EXECUTE_TOOLS" in kinds
    assert "COMPLETED_UNVERIFIED" in kinds


def test_completion_verifier_grants_verified_success():
    streams = [openai_stream("42")]
    router, _ = scripted_router(streams=streams)
    from eurus.tools import default_registry

    async def verifier(objective, output):
        from eurus.agent.verification import VerificationResult

        return VerificationResult(True, "answer satisfies criteria")

    agent = make_agent(router, default_registry(), completion_verifier=verifier)
    events = run_agent(agent, "compute 42")
    kinds = [decision.name for decision in agent.decisions]
    assert "VERIFIED_SUCCESS" in kinds
    assert events[-1].stop_reason == "completed"


def test_no_progress_terminates_after_replan():
    calls = [openai_stream("", [("c1", "noop", {})]) for _ in range(30)]
    router, transport = scripted_router(streams=calls)
    registry = ToolRegistry(
        [Tool("noop", "noop", EMPTY, lambda arguments, ctx: asyncio.sleep(0, "same"))]
    )
    agent = make_agent(
        router,
        registry,
        max_steps=40,
        max_parallel_tools=1,
    )
    events = run_agent(agent)
    assert events[-1].stop_reason == "no_progress"
    plan_events = [event for event in events if event.kind == "plan"]
    assert any("no measurable progress" in event.text for event in plan_events)


def test_boundary_reports_denial_without_approver():
    executed = []

    async def writer(arguments, ctx):
        executed.append(arguments)
        return "written"

    registry = ToolRegistry([Tool("write", "write", EMPTY, writer, mutates=True)])
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)

    from eurus.models import ToolCall

    async def go():
        boundary = ToolExecutionBoundary(registry, context)
        return await boundary.execute(ToolCall("c1", "write", {}))

    outcome = asyncio.run(go())
    assert outcome.status is ToolExecutionStatus.DENIED
    assert outcome.is_error
    assert "no approval provider" in outcome.output
    assert executed == []


def test_boundary_records_uncertain_mutation_on_timeout():
    from eurus.models import ToolCall

    async def slow_writer(arguments, ctx):
        await asyncio.sleep(1.0)
        return "written"

    async def approve(call, tool):
        return True

    registry = ToolRegistry([Tool("write", "write", EMPTY, slow_writer, mutates=True)])
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=0.05)
    recorded = []

    async def record(outcome):
        recorded.append(outcome)

    async def go():
        boundary = ToolExecutionBoundary(
            registry, context, approver=approve, outcome_recorder=record
        )
        first = await boundary.execute(ToolCall("c1", "write", {}))
        replay = await boundary.execute(ToolCall("c2", "write", {}))
        return first, replay

    first, replay = asyncio.run(go())
    assert first.status is ToolExecutionStatus.TIMEOUT
    assert first.uncertain is True
    assert first.side_effect_unresolved is True
    assert recorded and recorded[0].status is ToolExecutionStatus.TIMEOUT
    assert replay.status is ToolExecutionStatus.UNKNOWN
    assert replay.uncertain is True


def test_boundary_rerequests_cancellation_and_records_outcome():
    from eurus.models import ToolCall

    async def hanging_writer(arguments, ctx):
        await asyncio.sleep(5.0)
        return "written"

    async def approve(call, tool):
        return True

    registry = ToolRegistry([Tool("write", "write", EMPTY, hanging_writer, mutates=True)])
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)
    recorded = []

    async def record(outcome):
        recorded.append(outcome)

    async def go():
        boundary = ToolExecutionBoundary(
            registry, context, approver=approve, outcome_recorder=record
        )
        task = asyncio.create_task(boundary.execute(ToolCall("c1", "write", {})))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert recorded and recorded[0].status is ToolExecutionStatus.CANCELLED
    assert recorded[0].uncertain is True


def test_boundary_emits_approval_events():
    from eurus.models import ToolCall

    async def writer(arguments, ctx):
        return "written"

    registry = ToolRegistry([Tool("write", "write", EMPTY, writer, mutates=True)])
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)
    events = []

    async def emit(event_type, payload):
        events.append((event_type, payload["tool"]))

    async def approve(call, tool):
        return True

    async def go():
        boundary = ToolExecutionBoundary(registry, context, approver=approve, event_recorder=emit)
        return await boundary.execute(ToolCall("c1", "write", {}))

    outcome = asyncio.run(go())
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert [name for name, _ in events] == [
        "approval.requested",
        "approval.granted",
        "tool.started",
        "tool.finished",
    ]


def test_boundary_budget_check_blocks_execution():
    from eurus.models import ToolCall

    executed = []

    async def reader(arguments, ctx):
        executed.append("run")
        return "ok"

    registry = ToolRegistry([Tool("read", "read", EMPTY, reader)])
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)

    async def go():
        boundary = ToolExecutionBoundary(registry, context, budget_check=lambda count: False)
        return await boundary.execute(ToolCall("c1", "read", {}))

    outcome = asyncio.run(go())
    assert outcome.status is ToolExecutionStatus.DENIED
    assert "budget" in outcome.detail
    assert executed == []


def test_iteration_budget_for_subagent_is_capped():
    budget = IterationBudget(limit=200)
    assert budget.for_subagent().limit == 50


def test_streaming_partial_text_is_not_committed_on_interrupt():
    class SlowRouter:
        async def chat(self, messages, tools=()):
            await asyncio.sleep(0.1)
            raise AssertionError("streaming is expected")

        async def stream(self, messages, tools=()):
            yield StreamEvent.text_delta("partial")
            await asyncio.sleep(0.5)
            yield StreamEvent.text_delta(" more")

    router = SlowRouter()
    agent = make_agent(router)

    async def go():
        task = asyncio.create_task(_collect(agent))
        await asyncio.sleep(0.05)
        agent.interrupt()
        events = await task
        return events

    events = asyncio.run(go())
    assert events[-1].stop_reason == "interrupted"
    assert agent.history[-1].role == "user"


async def _collect(agent):
    return [event async for event in agent.run("go")]
