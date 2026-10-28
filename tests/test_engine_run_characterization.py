"""C1/C2 characterization tests: pin AgentEngine.run() observable behavior.

Rule 8 requires these to be written and GREEN before the Phase C refactor of
``AgentEngine.run()`` so the restructuring cannot silently change behavior.
They must pass before AND after the refactor.
"""

from __future__ import annotations

import asyncio

import pytest

from eurus.agent.events import AgentEvent
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.cancellation import CancellationToken
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus
from eurus.storage.runs import RunStore


class ScriptedAgent:
    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()
        self.max_model_calls = 100
        self.deadline_at = None

    async def run(self, objective):
        for event in self._events:
            yield event


def _types(result) -> list[str]:
    return [event.event_type for event in result.runtime_events]


@pytest.mark.asyncio
async def test_characterize_completed_unverified_run_event_sequence():
    agent = ScriptedAgent(
        [
            AgentEvent.step_started(1),
            AgentEvent.text_delta("hello"),
            AgentEvent.finished("completed", None),
        ]
    )
    result = await AgentEngine(agent).run("objective", owner_id="t")
    assert result.run.status is RunStatus.COMPLETED
    assert result.run.stop_reason == "completed_unverified"
    assert result.output == "hello"
    assert result.verified is None
    assert _types(result) == [
        "run.started",
        "verification.completed",
        "run.completed",
        "budget.snapshot",
    ]


@pytest.mark.asyncio
async def test_characterize_completed_run_with_verifier():
    async def accept(objective, output):
        return True

    agent = ScriptedAgent([AgentEvent.finished("completed", None)])
    result = await AgentEngine(agent, verifier=accept).run("objective", owner_id="t")
    assert result.run.status is RunStatus.COMPLETED
    assert result.verified is True
    assert _types(result) == [
        "run.started",
        "verification.completed",
        "run.completed",
        "budget.snapshot",
    ]


@pytest.mark.asyncio
async def test_characterize_blocked_run_waits_for_input():
    agent = ScriptedAgent([AgentEvent.finished("blocked", None)])
    result = await AgentEngine(agent).run("objective", owner_id="t")
    assert result.run.status is RunStatus.WAITING_INPUT
    assert result.run.stop_reason == "blocked"
    assert "run.blocked" in _types(result)
    assert "verification.completed" not in _types(result)


@pytest.mark.asyncio
async def test_characterize_interrupted_and_budget_stop_reasons():
    interrupted = ScriptedAgent([AgentEvent.finished("interrupted", None)])
    result = await AgentEngine(interrupted).run("objective", owner_id="t")
    assert result.run.status is RunStatus.INTERRUPTED
    assert result.run.stop_reason == "interrupted"
    assert "run.interrupted" in _types(result)

    exhausted = ScriptedAgent([AgentEvent.finished("budget_exhausted", None)])
    result = await AgentEngine(exhausted).run("objective", owner_id="t")
    assert result.run.status is RunStatus.FAILED
    assert result.run.stop_reason == "runtime_budget_exhausted"
    types = _types(result)
    assert "budget.exhausted" in types
    assert "run.failed" in types


@pytest.mark.asyncio
async def test_characterize_runtime_timeout_fails_with_budget_event():
    class SlowAgent(ScriptedAgent):
        async def run(self, objective):
            await asyncio.sleep(0.05)
            yield AgentEvent.finished("completed", None)

    result = await AgentEngine(SlowAgent([])).run(
        "objective", owner_id="t", budget=BudgetSpec(max_runtime_seconds=0.001)
    )
    assert result.run.status is RunStatus.FAILED
    assert result.run.stop_reason == "runtime_budget_exhausted"
    assert _types(result) == ["run.started", "budget.exhausted", "run.failed", "budget.snapshot"]


@pytest.mark.asyncio
async def test_characterize_cancelled_before_start():
    token = CancellationToken()
    token.cancel()
    agent = ScriptedAgent([AgentEvent.finished("completed", None)])
    result = await AgentEngine(agent).run("objective", owner_id="t", cancellation=token)
    assert result.run.status is RunStatus.CANCELLED
    assert result.run.stop_reason == "cancelled_before_start"
    assert _types(result) == ["run.started", "run.cancelled"]
    assert result.events == ()
    assert result.output == ""


@pytest.mark.asyncio
async def test_characterize_engine_rewrites_and_restores_agent_limits():
    from datetime import UTC, datetime, timedelta

    agent = ScriptedAgent([AgentEvent.finished("completed", None)])
    deadline = datetime.now(UTC) + timedelta(hours=1)
    result = await AgentEngine(agent).run(
        "objective",
        owner_id="t",
        budget=BudgetSpec(
            max_iterations=3,
            max_parallel_tools=2,
            max_model_calls=7,
            deadline_at=deadline,
        ),
    )
    assert result.run.status is RunStatus.COMPLETED
    assert agent.max_parallel_tools == 4, "parallel limit must be restored"
    assert agent.iteration_budget.limit == 50, "iteration limit must be restored"
    assert agent.max_model_calls == 100, "model call limit must be restored"
    assert agent.deadline_at is None, "deadline must be restored"


@pytest.mark.asyncio
async def test_characterize_engine_error_fails_run_and_reraises():
    class ExplodingAgent(ScriptedAgent):
        async def run(self, objective):
            yield AgentEvent.text_delta("partial")
            raise RuntimeError("boom")

    agent = ExplodingAgent([])
    with pytest.raises(RuntimeError, match="boom"):
        await AgentEngine(agent).run("objective", owner_id="t")
    assert agent_dead(agent) is False
    assert agent.iteration_budget.limit == 50, "limits restored even on error"


def agent_dead(agent: ScriptedAgent) -> bool:
    return agent.max_parallel_tools != 4


@pytest.mark.asyncio
async def test_characterize_checkpoints_persisted_each_step(tmp_path):
    run_store = RunStore(tmp_path / "runtime.sqlite3")
    agent = ScriptedAgent(
        [
            AgentEvent.step_started(1),
            AgentEvent.step_started(2),
            AgentEvent.finished("completed", None),
        ]
    )
    engine = AgentEngine(agent, run_store=run_store, checkpoint_interval_steps=1)
    result = await engine.run("objective", owner_id="t")
    assert result.run.status is RunStatus.COMPLETED
    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    assert checkpoint["state"]["iteration"] == 2
    saved = [e for e in result.runtime_events if e.event_type == "checkpoint.saved"]
    assert len(saved) == 3, "two step checkpoints plus the final done checkpoint"


@pytest.mark.asyncio
async def test_characterize_run_result_fields_and_output():
    agent = ScriptedAgent(
        [
            AgentEvent.text_delta("a"),
            AgentEvent.text_delta("b"),
            AgentEvent.finished("completed", None),
        ]
    )
    result = await AgentEngine(agent).run("objective", owner_id="t")
    assert result.output == "ab"
    assert [event.kind for event in result.events] == ["text", "text", "done"]
    assert result.events_dropped == 0
    assert result.cost_known is False
    assert result.verification is not None
    assert result.verification.status.value == "unverified"
