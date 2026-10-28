import pytest

from eurus.agent.events import AgentEvent
from eurus.models import Usage
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus


class FakeAgent:
    def __init__(self):
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True

    async def run(self, objective):
        yield AgentEvent.text_delta("result")
        yield AgentEvent.finished("completed", None)


@pytest.mark.asyncio
async def test_engine_runs_and_restores_agent_limits():
    agent = FakeAgent()
    engine = AgentEngine(agent)
    result = await engine.run(
        "objective", budget=BudgetSpec(max_iterations=3, max_parallel_tools=2)
    )
    assert result.run.status == RunStatus.COMPLETED
    assert result.output == "result"
    assert agent.iteration_budget.limit == 50
    assert agent.max_parallel_tools == 4


@pytest.mark.asyncio
async def test_engine_fails_when_verifier_rejects_result():
    async def reject(objective, output):
        return False

    result = await AgentEngine(FakeAgent(), verifier=reject).run("objective")
    assert result.run.status == RunStatus.FAILED
    assert result.run.stop_reason == "verification_failed"


@pytest.mark.asyncio
async def test_engine_enforces_runtime_timeout_and_restores_limits():
    import asyncio

    class SlowAgent(FakeAgent):
        async def run(self, objective):
            await asyncio.sleep(0.05)
            yield AgentEvent.finished("completed", None)

    agent = SlowAgent()
    result = await AgentEngine(agent).run("objective", budget=BudgetSpec(max_runtime_seconds=0.001))
    assert result.run.status == RunStatus.FAILED
    assert result.run.stop_reason == "runtime_budget_exhausted"
    assert agent.iteration_budget.limit == 50
    assert agent.max_parallel_tools == 4


def test_engine_reports_unenforceable_cost_budget_honestly():
    import asyncio

    result = asyncio.run(AgentEngine(FakeAgent()).run("objective", budget=BudgetSpec(max_cost=0.5)))
    assert result.run.status == RunStatus.COMPLETED
    assert result.cost_known is False
    assert result.cost_estimate is None
    events = [event.event_type for event in result.runtime_events]
    assert "cost.tracking_unavailable" in events


@pytest.mark.asyncio
async def test_engine_enforces_cost_budget_when_pricing_is_known():
    from eurus.runtime.pricing import ModelPrice, PricingCatalog

    class SpendingAgent(FakeAgent):
        def __init__(self) -> None:
            super().__init__()
            self.last_used = "fake/big spender"

        async def run(self, objective):
            yield AgentEvent.model_completed(1, Usage(input_tokens=1_000_000), "stop")
            if self.interrupted:
                yield AgentEvent.finished("interrupted", None)
            else:
                yield AgentEvent.finished("completed", None)

    prices = {
        "fake/big spender": ModelPrice("fake/big spender", input_per_mtok=10.0, output_per_mtok=0.0)
    }
    catalog = PricingCatalog(prices=prices)
    agent = SpendingAgent()
    result = await AgentEngine(agent, pricing=catalog).run(
        "objective", budget=BudgetSpec(max_cost=0.5)
    )
    assert result.run.status == RunStatus.FAILED
    assert result.run.stop_reason == "runtime_budget_exhausted"
    assert result.cost_known is True
    assert result.cost_estimate is not None and result.cost_estimate >= 10.0


@pytest.mark.asyncio
async def test_engine_returns_runtime_event_sequence_and_verification_result():
    async def accept(objective, output):
        return objective == "criteria" and output == "result"

    result = await AgentEngine(FakeAgent(), verifier=accept).run(
        "objective", success_criteria="criteria"
    )
    assert result.verified is True
    assert result.verification_status.value == "verified"
    assert result.verification is not None and result.verification.evidence
    assert [event.sequence_no for event in result.runtime_events] == [1, 2, 3, 4]
    assert result.runtime_events[-2].event_type == "run.completed"
    assert result.runtime_events[-1].event_type == "budget.snapshot"
    assert result.runtime_events[1].event_type == "verification.completed"


@pytest.mark.asyncio
async def test_engine_blocks_tool_batch_that_exceeds_budget():
    from pathlib import Path

    from eurus.agent.loop import Agent
    from eurus.providers.testing import openai_stream, scripted_router
    from eurus.tools.base import Tool, ToolContext, ToolRegistry

    calls = []

    async def handler(arguments, context):
        calls.append(arguments["value"])
        return "ok"

    tool = Tool(
        "read_value",
        "read value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )
    router, _ = scripted_router(
        streams=[
            openai_stream(
                "",
                [("a", "read_value", {"value": "a"}), ("b", "read_value", {"value": "b"})],
            ),
        ]
    )
    agent = Agent(
        router,
        ToolRegistry([tool]),
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
    )
    result = await AgentEngine(agent).run("objective", budget=BudgetSpec(max_tool_calls=1))
    assert calls == []
    assert result.run.status == RunStatus.FAILED
    assert result.run.stop_reason == "tool_budget"
