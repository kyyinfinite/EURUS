"""B5 regression tests: engine event buffers must stay bounded.

Before the fix, ``agent_events`` and ``runtime_events`` grew without bound for
the whole lifetime of a run (including every streamed text delta), so long
runs leaked memory.
"""

from __future__ import annotations

import pytest

from eurus.agent.events import AgentEvent
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import RunStatus
from eurus.storage.runs import RunStore


class StreamingAgent:
    def __init__(self, deltas: int = 12) -> None:
        self.deltas = deltas
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()

    async def run(self, objective):
        for index in range(self.deltas):
            yield AgentEvent.text_delta(f"chunk-{index} ")
        yield AgentEvent.finished("completed", None)


class SteppingAgent:
    def __init__(self, steps: int = 10) -> None:
        self.steps = steps
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()

    async def run(self, objective):
        for index in range(1, self.steps + 1):
            yield AgentEvent.step_started(index)
        yield AgentEvent.finished("completed", None)


class ToolNoiseAgent:
    """Yields tool events that force the engine to record runtime events."""

    def __init__(self, rounds: int = 6) -> None:
        self.rounds = rounds
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()

    async def run(self, objective):
        for index in range(self.rounds):
            call = type(
                "Call",
                (),
                {"id": f"c{index}", "name": "probe", "arguments": {"i": index}},
            )()
            yield AgentEvent.tool_started(call)
            result = type(
                "Result",
                (),
                {
                    "call_id": f"c{index}",
                    "name": "probe",
                    "output": "ok",
                    "is_error": False,
                    "error_code": "",
                },
            )()
            yield AgentEvent.tool_finished(result)
        yield AgentEvent.finished("completed", None)


@pytest.mark.asyncio
async def test_agent_event_buffer_is_bounded(tmp_path):
    engine = AgentEngine(StreamingAgent(deltas=12), max_agent_events=5)
    result = await engine.run("objective", owner_id="test")

    assert len(result.events) == 5
    assert result.events_dropped == 8, "12 text deltas + 1 done = 13 events, keep 5"
    assert result.events[-1].kind == "done", "the tail must be preserved"
    assert result.events[-2].text == "chunk-11 "
    assert result.run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_runtime_event_buffer_is_bounded(tmp_path):
    engine = AgentEngine(ToolNoiseAgent(rounds=6), max_runtime_events=4)
    result = await engine.run("objective", owner_id="test")

    assert len(result.runtime_events) == 4
    assert result.runtime_events_dropped > 0
    assert result.runtime_events[-1].event_type == "budget.snapshot"
    assert result.runtime_events[0].event_type != "run.started", "oldest events are dropped"


@pytest.mark.asyncio
async def test_last_step_survives_truncation_for_final_checkpoint(tmp_path):
    run_store = RunStore(tmp_path / "runtime.sqlite3")
    engine = AgentEngine(
        SteppingAgent(steps=10),
        run_store=run_store,
        checkpoint_interval_steps=1,
        max_agent_events=3,
    )
    result = await engine.run("objective", owner_id="test")

    assert result.run.status == RunStatus.COMPLETED
    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    assert checkpoint["state"].get("iteration") == 10, (
        "the final checkpoint must still see the latest step after truncation"
    )


@pytest.mark.asyncio
async def test_default_buffers_do_not_truncate_small_runs():
    engine = AgentEngine(StreamingAgent(deltas=12))
    result = await engine.run("objective", owner_id="test")
    assert len(result.events) == 13
    assert result.events_dropped == 0
    assert result.runtime_events_dropped == 0
