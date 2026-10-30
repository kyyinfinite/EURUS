"""Phase 1.6: checkpoint policy — interval fulls, lights in between, pruning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eurus.agent.loop import Agent
from eurus.providers.testing import openai_message, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.storage.runs import RunStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry


def make_agent(router, registry=None) -> Agent:
    return Agent(
        router,
        registry or ToolRegistry([]),
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
        iteration_budget=None,
        max_steps=50,
        stream=False,
    )


def _multi_step_streams(steps: int):
    streams = []
    for index in range(steps):
        streams.append(openai_message("", calls=[(f"c{index}", "noop", {"n": index})]))
    streams.append(openai_message("final answer"))
    return scripted_router(json_responses=streams)


def _noop_registry() -> ToolRegistry:
    async def handler(arguments, context):
        return "ok"

    return ToolRegistry(
        [
            Tool(
                "noop",
                "no-op",
                {"type": "object", "properties": {"n": {"type": "number"}}},
                handler,
            )
        ]
    )


def test_full_checkpoints_only_every_n_steps_and_on_done(tmp_path):
    router, _ = _multi_step_streams(5)
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = make_agent(router, _noop_registry())
    engine = AgentEngine(agent, run_store=run_store, checkpoint_interval_steps=2)
    result = asyncio.run(engine.run("objective"))
    assert result.run.status.value == "completed"

    with run_store.database.connection() as connection:
        rows = connection.execute(
            "SELECT checkpoint_type, iteration FROM eurus_run_checkpoints "
            "WHERE run_id = ? ORDER BY checkpoint_version",
            (result.run.run_id,),
        ).fetchall()
    fulls = [row["iteration"] for row in rows if row["checkpoint_type"] == "runtime"]
    lights = [row["iteration"] for row in rows if row["checkpoint_type"] == "light"]
    # steps 1..6 with interval 2 (5 tool steps + the final-answer step):
    # fulls at 2, 4, 6 plus the done checkpoint; lights at 1, 3, 5.
    assert fulls == [2, 4, 6, 6]
    assert lights == [1, 3, 5]


def test_light_checkpoints_carry_no_history(tmp_path):
    router, _ = _multi_step_streams(3)
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = make_agent(router, _noop_registry())
    engine = AgentEngine(agent, run_store=run_store, checkpoint_interval_steps=10)
    result = asyncio.run(engine.run("objective"))

    with run_store.database.connection() as connection:
        rows = connection.execute(
            "SELECT checkpoint_type, state_json, iteration, side_effect_status "
            "FROM eurus_run_checkpoints WHERE run_id = ? ORDER BY checkpoint_version",
            (result.run.run_id,),
        ).fetchall()
    lights = [row for row in rows if row["checkpoint_type"] == "light"]
    assert lights, "expected at least one light checkpoint"
    for row in lights:
        assert "history" not in row["state_json"]
        assert row["iteration"] is not None
        assert row["side_effect_status"] == "settled"


def test_latest_checkpoint_ignores_light_rows(tmp_path):
    router, _ = _multi_step_streams(3)
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = make_agent(router, _noop_registry())
    engine = AgentEngine(agent, run_store=run_store, checkpoint_interval_steps=10)
    result = asyncio.run(engine.run("objective"))

    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    assert checkpoint["checkpoint_type"] == "runtime"
    assert "history" in checkpoint["state"]
    progress = run_store.latest_progress(result.run.run_id)
    assert progress is not None and progress["iteration"] is not None


def test_mutating_tool_creates_full_checkpoint_before_and_light_after(tmp_path):
    async def handler(arguments, context):
        return "written"

    mutating = Tool(
        "write_thing",
        "write",
        {"type": "object", "properties": {"n": {"type": "number"}}},
        handler,
        mutates=True,
        requires_approval=False,
    )
    router, _ = scripted_router(
        json_responses=[
            openai_message("", calls=[("c1", "write_thing", {"n": 1})]),
            openai_message("final answer"),
        ]
    )
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = make_agent(router, ToolRegistry([mutating]))
    engine = AgentEngine(agent, run_store=run_store, checkpoint_interval_steps=10)
    result = asyncio.run(engine.run("objective"))

    with run_store.database.connection() as connection:
        rows = connection.execute(
            "SELECT checkpoint_type, iteration, state_json "
            "FROM eurus_run_checkpoints WHERE run_id = ? ORDER BY checkpoint_version",
            (result.run.run_id,),
        ).fetchall()
    before = [row for row in rows if row["checkpoint_type"] == "runtime"]
    assert any(row["state_json"].find("in_flight") >= 0 for row in before), (
        "a full checkpoint must be saved before the mutating tool runs"
    )
    after = [row for row in rows if row["checkpoint_type"] == "light"]
    assert any(row["state_json"].find("settled") >= 0 for row in after)


def test_pruning_keeps_only_k_checkpoints_per_type(tmp_path):
    router, _ = _multi_step_streams(1)
    run_store = RunStore(tmp_path / "runs.sqlite3")
    run_store.create(_make_run())
    run_id = run_store.list_runs(limit=1)[0].run_id
    for index in range(15):
        run_store.save_checkpoint(
            run_id,
            {"iteration": index, "side_effect_status": "settled"},
            checkpoint_type="runtime",
        )
    deleted = run_store.prune_checkpoints(run_id, "runtime", 5)
    assert deleted == 10
    with run_store.database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM eurus_run_checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    assert count == 5
    remaining = run_store.latest_checkpoint(run_id)
    assert remaining is not None and remaining["state"]["iteration"] == 14


def _make_run():
    from eurus.runtime.models import Run

    run = Run(owner_id="test", objective="prune me")
    return run


def test_prune_rejects_invalid_keep(tmp_path):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    run_store.create(_make_run())
    run_id = run_store.list_runs(limit=1)[0].run_id
    run_store.save_checkpoint(run_id, {"iteration": 1})
    with pytest.raises(ValueError):
        run_store.prune_checkpoints(run_id, "runtime", 0)


def test_checkpoint_state_preparation_runs_off_loop(tmp_path):
    """Snapshot serialization stays off the event loop even for big histories."""
    router, _ = _multi_step_streams(1)
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = make_agent(router, _noop_registry())
    for index in range(50):
        agent.history.append(Message_lite(index))
    engine = AgentEngine(agent, run_store=run_store)
    result = asyncio.run(engine.run("objective"))
    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    assert len(checkpoint["state"]["history"]) >= 50


class Message_lite:
    """Minimal duck-typed history message (to_dict contract)."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.role = "assistant"
        self.content = f"m{index}"
        self.tool_calls = ()

    def to_dict(self) -> dict[str, object]:
        return {"role": self.role, "content": self.content}
