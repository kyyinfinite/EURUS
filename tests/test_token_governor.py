from __future__ import annotations

import asyncio

from eurus.models import Message, Usage
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.runtime.token_governor import TokenGovernor


def _usage(inp: int, out: int = 10, cache_read: int = 0) -> Usage:
    return Usage(input_tokens=inp, output_tokens=out, cache_read_tokens=cache_read)


def test_governor_tracks_usage_and_growth() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(1000))
    governor.observe(_usage(1500, cache_read=500))
    assert governor.calls_observed == 2
    assert governor.input_tokens == 2500
    assert governor.output_tokens == 20
    assert governor.average_input_per_call == 1250
    assert governor.context_growth_per_call == 500
    assert governor.cache_read_ratio > 0
    snapshot = governor.snapshot()
    assert snapshot["calls_observed"] == 2
    assert snapshot["winddown_requested"] is False


def test_governor_winddown_on_token_threshold() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(5000))
    assert governor.should_winddown(tokens_used=5000, max_tokens=10000) is False
    governor.observe(_usage(4000))
    assert governor.should_winddown(tokens_used=9000, max_tokens=10000) is True
    governor.request_winddown()
    assert governor.should_winddown(tokens_used=9000, max_tokens=10000) is False
    assert governor.snapshot()["winddown_requested"] is True


def test_governor_winddown_projects_next_iteration() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(6000))
    governor.observe(_usage(7000))
    assert governor.average_input_per_call == 6500
    assert governor.should_winddown(tokens_used=7000, max_tokens=12000) is True


def test_governor_ignores_winddown_without_budget() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(10**9))
    governor.observe(_usage(10**9))
    assert governor.should_winddown(tokens_used=2 * 10**9, max_tokens=None) is False


def test_governor_winddown_on_cost_threshold_only_when_priced() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(100))
    governor.observe(_usage(100))
    assert (
        governor.should_winddown(tokens_used=200, max_tokens=None, cost_used=0.9, max_cost=1.0)
        is True
    )
    assert (
        governor.should_winddown(
            tokens_used=200,
            max_tokens=None,
            cost_used=0.9,
            max_cost=1.0,
            cost_known=False,
        )
        is False
    )


def test_governor_requires_multiple_calls_before_winddown() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(9999))
    assert governor.should_winddown(tokens_used=9999, max_tokens=10000) is False


def test_governor_status_line_and_note() -> None:
    governor = TokenGovernor()
    governor.observe(_usage(3000))
    governor.observe(_usage(3000))
    line = governor.status_line(6000, 10000)
    assert "6000/10000" in line and "2 model calls" in line
    note = governor.winddown_note(9000, 10000)
    assert "final answer" in note
    assert "90%" in note


class _IterationBudgetStub:
    limit = 10
    used = 0


class _FakeAgent:
    def __init__(self, stream_count: int = 3) -> None:
        self.stream_count = stream_count
        self.history: list[Message] = [Message.system("sys")]
        self.usage = Usage()
        self.wrap_up_note = ""
        self.model_calls_used = 0
        self.max_model_calls = 100
        self.max_parallel_tools = 4
        self.iteration_budget = _IterationBudgetStub()
        self.runtime_budget_tracker = None
        self.deadline_at = None
        self.run_id = None

    def request_wrap_up(self, note: str) -> None:
        self.wrap_up_note = note

    async def run(self, text: str):
        from eurus.agent.events import AgentEvent

        for index in range(self.stream_count):
            self.model_calls_used += 1
            yield AgentEvent.step_started(index + 1)
            yield AgentEvent.model_completed(index + 1, _usage(4000, 100), "stop")
        yield AgentEvent.finished("completed", self.usage, "done text")


def test_engine_requests_winddown_near_token_budget() -> None:
    agent = _FakeAgent(stream_count=3)
    engine = AgentEngine(agent)
    result = asyncio.run(
        engine.run(
            "do work",
            budget=BudgetSpec(max_tokens=9000, max_runtime_seconds=60.0),
        )
    )
    assert agent.wrap_up_note != ""
    assert "final answer" in agent.wrap_up_note
    assert any(event.event_type == "token.winddown" for event in result.runtime_events)
    snapshot = next(
        event for event in result.runtime_events if event.event_type == "budget.snapshot"
    )
    assert "governor" in snapshot.payload
    assert snapshot.payload["governor"]["calls_observed"] == 3


def test_engine_does_not_winddown_without_token_budget() -> None:
    agent = _FakeAgent(stream_count=3)
    engine = AgentEngine(agent)
    result = asyncio.run(engine.run("do work", budget=BudgetSpec(max_runtime_seconds=60.0)))
    assert agent.wrap_up_note == ""
    assert not any(event.event_type == "token.winddown" for event in result.runtime_events)


def test_agent_loop_clears_wrap_up_note_between_runs(tmp_path) -> None:
    from eurus.agent.loop import Agent
    from eurus.catalog import BUILTIN_PROVIDERS
    from eurus.models import ModelRef
    from eurus.providers.base import RetryPolicy
    from eurus.providers.openai_compat import OpenAICompatProvider
    from eurus.providers.router import Router
    from eurus.providers.testing import FakeTransport, RecordingSleep, openai_stream
    from eurus.tools.base import ToolContext, ToolRegistry

    class _MinimalRouter(Router):
        def __init__(self) -> None:
            provider = OpenAICompatProvider(
                BUILTIN_PROVIDERS["groq"],
                "k",
                FakeTransport(streams=[openai_stream("first answer"), openai_stream("second")]),
                RetryPolicy(max_retries=0),
                RecordingSleep(),
            )
            super().__init__({"groq": provider}, [ModelRef.parse("groq/test")])

    agent = Agent(
        _MinimalRouter(),
        ToolRegistry([]),
        ToolContext(workspace=tmp_path, max_output=100, timeout=10.0),
        system_prompt="sys",
        stream=True,
    )
    agent.request_wrap_up("wrap up now")
    assert agent.wrap_up_requested is True

    async def consume(text: str) -> None:
        async for _ in agent.run(text):
            pass

    asyncio.run(consume("hello"))
    assert agent.wrap_up_requested is False
    agent.request_wrap_up("second advisory")
    asyncio.run(consume("again"))
    assert agent.wrap_up_requested is False
