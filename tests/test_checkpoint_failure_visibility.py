"""B3 regression tests: checkpoint save failures must be visible, not silent.

Before the fix, ``save_checkpoint`` swallowed every exception with a bare
``except Exception: return`` so a broken run store silently disabled
durability while the run kept going.
"""

from __future__ import annotations

import pytest

from eurus.agent.events import AgentEvent
from eurus.runtime.engine import AgentEngine
from eurus.storage.runs import RunStore


class FakeAgent:
    def __init__(self) -> None:
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()

    async def run(self, objective):
        yield AgentEvent.step_started(1)
        yield AgentEvent.text_delta("partial ")
        yield AgentEvent.text_delta("result")
        yield AgentEvent.finished("completed", None)


class BrokenCheckpointStore(RunStore):
    def save_checkpoint(self, run_id, state, *, checkpoint_type="runtime", expected_version=None):
        raise RuntimeError("disk on fire")


@pytest.mark.asyncio
async def test_checkpoint_failure_records_event_and_run_continues(tmp_path):
    run_store = BrokenCheckpointStore(tmp_path / "runtime.sqlite3")
    engine = AgentEngine(FakeAgent(), run_store=run_store, checkpoint_interval_steps=1)
    result = await engine.run("objective", owner_id="test")

    assert result.run.status.value == "completed"
    event_types = [event.event_type for event in result.runtime_events]
    assert "checkpoint.failed" in event_types, (
        "a failing checkpoint save must be recorded as checkpoint.failed"
    )
    failed = [event for event in result.runtime_events if event.event_type == "checkpoint.failed"]
    assert any("disk on fire" in str(event.payload.get("error")) for event in failed)
    assert "run.completed" in event_types


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_mask_successful_checkpoints(tmp_path):
    class FlakyStore(RunStore):
        def __init__(self, path):
            super().__init__(path)
            self.save_calls = 0

        def save_checkpoint(
            self, run_id, state, *, checkpoint_type="runtime", expected_version=None
        ):
            self.save_calls += 1
            if self.save_calls == 1:
                raise RuntimeError("flaky")
            return super().save_checkpoint(
                run_id, state, checkpoint_type=checkpoint_type, expected_version=expected_version
            )

    run_store = FlakyStore(tmp_path / "runtime.sqlite3")
    engine = AgentEngine(FakeAgent(), run_store=run_store, checkpoint_interval_steps=1)
    result = await engine.run("objective", owner_id="test")

    event_types = [event.event_type for event in result.runtime_events]
    assert "checkpoint.failed" in event_types
    assert "checkpoint.saved" in event_types
    assert result.run.status.value == "completed"
