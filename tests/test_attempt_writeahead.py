"""B1 regression tests: write-ahead attempt records for mutating tools.

Before the fix, tool attempts were only persisted after execution finished, so a
crash mid-execution left no durable trace and recovery could not detect the
in-flight side effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eurus.models import ToolCall
from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

EMPTY_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def _mutating_tool(handler) -> Tool:
    return Tool(
        "write_file",
        "write a file",
        EMPTY_SCHEMA,
        handler,
        mutates=True,
        path_keys=("path",),
    )


def _readonly_tool(handler) -> Tool:
    return Tool(
        "read_value",
        "read a value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )


@pytest.mark.asyncio
async def test_mutating_tool_writes_pending_attempt_before_completion(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts.sqlite3")
    seen_during_execution: list[list[dict]] = []

    async def handler(arguments, context):
        seen_during_execution.append(store.list_for_run("run-wa-1"))
        return "ok"

    boundary = ToolExecutionBoundary(
        ToolRegistry([_mutating_tool(handler)]),
        ToolContext(workspace=Path(".")),
        approver=lambda call, tool: True,
    )
    boundary.attempt_started_recorder = lambda started: store.record_pending(
        run_id=started.run_id,
        tool_name=started.tool_name,
        call_key=started.call_key,
        attempt_id=started.attempt_id,
        risk_level=started.risk_level,
    )
    boundary.outcome_recorder = lambda outcome: store.record(outcome)

    outcome = await boundary.execute(
        ToolCall("call-1", "write_file", {"path": "a.txt"}), run_id="run-wa-1"
    )

    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert len(seen_during_execution) == 1
    pending_rows = [
        row for row in seen_during_execution[0] if row["attempt_id"] == outcome.attempt_id
    ]
    assert len(pending_rows) == 1, "write-ahead pending row must exist during execution"
    assert pending_rows[0]["status"] == "pending"
    assert pending_rows[0]["uncertain"] is True

    final_rows = [
        row for row in store.list_for_run("run-wa-1") if row["attempt_id"] == outcome.attempt_id
    ]
    assert len(final_rows) == 1, "final outcome must upsert over the pending row"
    assert final_rows[0]["status"] == "success"
    assert final_rows[0]["uncertain"] is False


@pytest.mark.asyncio
async def test_readonly_tool_gets_no_write_ahead_row(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts.sqlite3")
    seen_during_execution: list[list[dict]] = []

    async def handler(arguments, context):
        seen_during_execution.append(store.list_for_run("run-wa-2"))
        return "value"

    boundary = ToolExecutionBoundary(
        ToolRegistry([_readonly_tool(handler)]),
        ToolContext(workspace=Path(".")),
    )
    boundary.attempt_started_recorder = lambda started: store.record_pending(
        run_id=started.run_id,
        tool_name=started.tool_name,
        call_key=started.call_key,
        attempt_id=started.attempt_id,
        risk_level=started.risk_level,
    )

    outcome = await boundary.execute(
        ToolCall("call-2", "read_value", {"value": "x"}), run_id="run-wa-2"
    )
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert seen_during_execution[0] == [], "read-only tools must not write pending rows"


def test_record_pending_then_finalize_upserts_single_row(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts.sqlite3")
    store.record_pending(
        run_id="run-wa-3",
        tool_name="write_file",
        call_key='write_file:{"path": "b.txt"}',
        attempt_id="attempt-3",
        risk_level="high",
    )
    store.record(
        ToolExecutionOutcome(
            call_id="call-3",
            tool_name="write_file",
            status=ToolExecutionStatus.SUCCESS,
            call_key='write_file:{"path": "b.txt"}',
            attempt_id="attempt-3",
            run_id="run-wa-3",
            risk_level="high",
        )
    )
    rows = [row for row in store.list_for_run("run-wa-3") if row["attempt_id"] == "attempt-3"]
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["uncertain"] is False
    assert rows[0]["finished_at"] is not None


def test_crash_leftover_pending_row_demands_reconciliation(tmp_path):
    from eurus.runtime.models import Run, RunStatus
    from eurus.runtime.recovery import RecoveryAction
    from eurus.runtime.resume import RecoveryManager
    from eurus.runtime.state_machine import transition_run

    run_store = RunStore(tmp_path / "runtime.sqlite3")
    run = Run(owner_id="op", objective="crashed mid mutation")
    run_store.create(run)
    transition_run(run, RunStatus.RUNNING)
    run_store.save(run, expected_status_version=run.status_version - 1)
    run_store.save_checkpoint(
        run.run_id, {"objective": run.objective, "iteration": 2}, checkpoint_type="runtime"
    )
    transition_run(run, RunStatus.INTERRUPTED, stop_reason="crashed")
    run_store.save(run, expected_status_version=run.status_version - 1)

    attempts = ToolAttemptStore(run_store.database)
    attempts.record_pending(
        run_id=run.run_id,
        tool_name="write_file",
        call_key='write_file:{"path": "c.txt"}',
        attempt_id="attempt-crash",
        risk_level="high",
    )

    manager = RecoveryManager(run_store)
    plan = manager.inspect(run.run_id)
    assert plan.action is RecoveryAction.RECONCILE_UNKNOWN_OUTCOME


@pytest.mark.asyncio
async def test_engine_wires_write_ahead_recorder(tmp_path):
    from eurus.agent.loop import Agent
    from eurus.providers.testing import openai_stream, scripted_router
    from eurus.runtime.engine import AgentEngine

    store = ToolAttemptStore(tmp_path / "engine-attempts.sqlite3")
    seen_pending: list[bool] = []

    async def handler(arguments, context):
        rows = store.list_for_run("run-wa-4")
        seen_pending.append(any(row["status"] == "pending" for row in rows))
        return "ok"

    tool = _mutating_tool(handler)
    router, _ = scripted_router(streams=[openai_stream("done")])
    agent = Agent(
        router,
        ToolRegistry([tool]),
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
        approver=lambda call, tool: True,
    )
    engine = AgentEngine(agent, attempt_store=store)
    engine._attach_boundary_recorders("run-wa-4")
    assert agent.boundary.attempt_started_recorder is not None

    outcome = await agent.boundary.execute(
        ToolCall("call-4", "write_file", {"path": "d.txt"}), run_id="run-wa-4"
    )
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert seen_pending == [True], "pending row must be visible during execution"
    rows = store.list_for_run("run-wa-4")
    assert rows and all(row["status"] == "success" for row in rows)
