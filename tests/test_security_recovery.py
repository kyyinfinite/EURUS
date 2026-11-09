from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from eurus.models import ToolCall
from eurus.providers.testing import openai_message, scripted_router
from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus, RuntimeEvent
from eurus.runtime.state_machine import InvalidRunTransition, transition_run
from eurus.storage.runs import ConcurrentRunUpdate, RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

EMPTY = {"type": "object", "properties": {}}


def _mutating_tool(handler) -> Tool:
    return Tool("mutate", "mutates state", EMPTY, handler, mutates=True)


@pytest.mark.asyncio
async def test_approval_service_exception_denies_safely(tmp_path):
    from eurus.security.redaction import redact_text

    async def handler(arguments, context):
        return "mutated"

    events: list[tuple[str, dict]] = []

    async def failing_approver(call, tool):
        raise RuntimeError("approval backend unavailable")

    boundary = ToolExecutionBoundary(
        ToolRegistry([_mutating_tool(handler)]),
        ToolContext(workspace=tmp_path),
        approver=failing_approver,
        event_recorder=lambda event_type, payload: events.append((event_type, payload)),
    )
    outcome = await boundary.execute(ToolCall("c1", "mutate", {}))
    assert outcome.is_error is True
    assert outcome.status is ToolExecutionStatus.DENIED
    assert "approval service failed" in outcome.output
    emitted = [event_type for event_type, _ in events]
    assert "approval.requested" in emitted
    assert "approval.unavailable" in emitted
    assert "secret" not in redact_text(outcome.output)


@pytest.mark.asyncio
async def test_boundary_redacts_secrets_in_durable_attempt_details(tmp_path):
    async def handler(arguments, context):
        raise RuntimeError("connection failed with token sk-abcdefghijklmnop123456")

    run_store = RunStore(tmp_path / "runs.sqlite3")
    attempt_store = ToolAttemptStore(run_store.database)
    run = run_store.create(Run(owner_id="o", objective="obj"))

    async def recorder(outcome):
        attempt_store.record(outcome)

    boundary = ToolExecutionBoundary(
        ToolRegistry([Tool("boom", "explodes", EMPTY, handler)]),
        ToolContext(workspace=tmp_path),
        outcome_recorder=recorder,
    )
    outcome = await boundary.execute(ToolCall("c1", "boom", {}), run_id=run.run_id)
    assert outcome.is_error is True
    attempts = attempt_store.list_for_run(run.run_id)
    assert attempts
    detail = attempts[0]["detail"]
    assert "sk-abcdefghijklmnop123456" not in detail
    assert "REDACTED" in detail


@pytest.mark.asyncio
async def test_engine_records_uncertain_attempt_when_mutation_times_out(tmp_path):

    started = asyncio.Event()

    async def slow_mutator(arguments, context):
        started.set()
        await asyncio.sleep(10)
        return "mutated"

    router, _ = scripted_router(
        json_responses=[
            openai_message("", calls=[("c1", "mutate", {})]),
            openai_message("gave up after the tool error"),
        ]
    )
    from eurus.agent.loop import Agent, IterationBudget

    async def allow(call, tool):
        return True

    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = Agent(
        router,
        ToolRegistry([_mutating_tool(slow_mutator)]),
        ToolContext(workspace=tmp_path, timeout=0.3),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=10),
        stream=False,
        approver=allow,
    )
    engine = AgentEngine(agent, run_store=run_store)
    result = await engine.run("mutate something")
    attempts = ToolAttemptStore(run_store.database).list_for_run(result.run.run_id)
    assert attempts
    uncertain = [item for item in attempts if item["uncertain"]]
    assert uncertain
    assert uncertain[0]["status"] == "timeout"


def test_optimistic_concurrency_rejects_stale_persistence(tmp_path):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    first = run_store.create(Run(owner_id="o", objective="one"))
    second = run_store.get(first.run_id)
    transition_run(second, RunStatus.RUNNING)
    run_store.save(second, expected_status_version=second.status_version - 1)
    stale = run_store.get(first.run_id)
    transition_run(stale, RunStatus.VERIFYING)
    stale.stop_reason = "stale write"
    with pytest.raises(ConcurrentRunUpdate):
        run_store.save(stale, expected_status_version=0)


def test_run_state_machine_rejects_illegal_transitions():
    run = Run(owner_id="o", objective="obj")
    transition_run(run, RunStatus.RUNNING)
    transition_run(run, RunStatus.VERIFYING)
    transition_run(run, RunStatus.COMPLETED)
    with pytest.raises(InvalidRunTransition):
        transition_run(run, RunStatus.RUNNING)
    fresh = Run(owner_id="o", objective="obj")
    with pytest.raises(InvalidRunTransition):
        transition_run(fresh, RunStatus.COMPLETED)


@pytest.mark.asyncio
async def test_engine_run_survives_simulated_process_crash(tmp_path):
    from eurus.runtime.resume import RecoveryManager
    from eurus.storage.events import EventStore

    router, _ = scripted_router(json_responses=[openai_message("resumed answer")])
    from eurus.agent.loop import Agent, IterationBudget

    run_store = RunStore(tmp_path / "runs.sqlite3")
    event_store = EventStore(run_store.database)
    run = run_store.create(Run(owner_id="o", objective="long objective"))
    stored = run_store.get(run.run_id)
    transition_run(stored, RunStatus.RUNNING)
    run_store.save(stored, expected_status_version=stored.status_version - 1)
    run_store.save_checkpoint(
        run.run_id,
        {"objective": "long objective", "iteration": 2, "side_effect_status": "settled"},
        checkpoint_type="runtime",
    )
    event_store.append(
        RuntimeEvent(
            run_id=run.run_id,
            event_type="tool.call.finished",
            sequence_no=1,
            payload={"tool": "read_file", "is_error": False},
        )
    )

    manager = RecoveryManager(run_store, event_store=event_store)

    async def make_engine(resumed_run):
        agent = Agent(
            router,
            ToolRegistry([]),
            ToolContext(workspace=tmp_path),
            system_prompt="test",
            iteration_budget=IterationBudget(limit=10),
            stream=False,
        )
        return AgentEngine(agent, run_store=run_store)

    resumed = await manager.resume(run.run_id, make_engine)
    assert resumed.run.status is RunStatus.COMPLETED
    assert "resumed answer" in resumed.output
    directive = manager.resumption_directive(run.run_id)
    assert directive is None or "read_file" in directive or "iteration" in directive


@pytest.mark.asyncio
async def test_delegation_child_has_independent_bounded_budget(tmp_path):
    from eurus.agent.child_runtime import child_budget_spec

    spec = child_budget_spec(max_iterations=5, timeout_seconds=30.0)
    assert spec.max_iterations == 5
    assert spec.max_child_runs == 0
    assert spec.max_runtime_seconds == 30.0
    assert spec.max_tool_calls == 40


@pytest.mark.asyncio
async def test_boundary_blocks_uncertain_mutation_across_engine_instances(tmp_path):
    async def handler(arguments, context):
        return "mutated"

    database = tmp_path / "runs.sqlite3"
    run_store = RunStore(database)
    attempt_store = ToolAttemptStore(database)
    run = run_store.create(Run(owner_id="o", objective="obj"))
    run_id = run.run_id
    uncertain = ToolExecutionOutcome(
        call_id="c0",
        tool_name="mutate",
        status=ToolExecutionStatus.UNKNOWN,
        output="error: unknown",
        is_error=True,
        uncertain=True,
        call_key="mutate:{}",
        run_id=run_id,
        risk_level="high",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        detail="uncertain",
    )
    attempt_store.record(uncertain)

    second_boundary = ToolExecutionBoundary(
        ToolRegistry([_mutating_tool(handler)]),
        ToolContext(workspace=tmp_path),
        uncertain_keys_provider=lambda active_run_id: [
            row["call_key"] for row in attempt_store.uncertain_keys(active_run_id)
        ],
    )
    outcome = await second_boundary.execute(ToolCall("c1", "mutate", {}), run_id=run_id)
    assert outcome.is_error is True
    assert outcome.status is ToolExecutionStatus.UNKNOWN
    assert outcome.uncertain is True
