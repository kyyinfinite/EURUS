import asyncio
from pathlib import Path

import pytest

from eurus.agent.events import AgentEvent
from eurus.providers.testing import scripted_router
from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.resume import RecoveryBlocked, RecoveryManager
from eurus.runtime.state_machine import transition_run
from eurus.storage.database import Database
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import ToolContext, ToolRegistry

EMPTY = {"type": "object", "properties": {}}


def test_tool_attempt_store_roundtrip(tmp_path):
    store = ToolAttemptStore(Database(tmp_path / "attempts.sqlite3"))
    outcome = ToolExecutionOutcome(
        call_id="c1",
        tool_name="write_file",
        status=ToolExecutionStatus.SUCCESS,
        call_key="write_file:{path}",
        run_id="run-1",
        risk_level="high",
    )
    store.record(outcome)
    rows = store.list_for_run("run-1")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["uncertain"] is False
    assert rows[0]["risk_level"] == "high"


def test_tool_attempt_store_tracks_uncertain_and_resolution(tmp_path):
    store = ToolAttemptStore(Database(tmp_path / "attempts.sqlite3"))
    outcome = ToolExecutionOutcome(
        call_id="c1",
        tool_name="shell",
        status=ToolExecutionStatus.TIMEOUT,
        is_error=True,
        uncertain=True,
        call_key="shell:{cmd}",
        run_id="run-2",
        detail="mutation timed out",
    )
    store.record(outcome)
    assert store.has_uncertain_mutation("run-2") is True
    assert store.has_uncertain_mutation("run-3") is False
    uncertain = store.uncertain_keys("run-2")
    assert len(uncertain) == 1
    assert uncertain[0]["attempt_id"]
    assert store.resolve_uncertain(uncertain[0]["attempt_id"], "verified file state") is True
    assert store.has_uncertain_mutation("run-2") is False


def test_tool_attempt_store_rejects_empty_resolution(tmp_path):
    store = ToolAttemptStore(Database(tmp_path / "attempts.sqlite3"))
    with pytest.raises(ValueError, match="resolution"):
        store.resolve_uncertain("missing", "  ")


class ToolAgent:
    def __init__(self, registry, outcome):
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()
        self.runtime_budget_tracker = None
        self.interrupted = False
        self.registry = registry
        self._outcome = outcome

    def interrupt(self):
        self.interrupted = True

    async def run(self, objective):
        yield AgentEvent.step_started(1)
        yield AgentEvent.tool_started(type("C", (), {"id": "c1", "name": "x", "arguments": {}})())
        record = type("R", (), {"id": "c1", "name": "x", "output": "ok", "is_error": False})()
        yield AgentEvent.tool_finished(record)
        yield AgentEvent.finished("completed", None)


@pytest.mark.asyncio
async def test_engine_records_tool_attempts_and_checkpoints(tmp_path):
    recorded = []

    async def capture(outcome):
        recorded.append(outcome)

    class RealAgent:
        def __init__(self):
            from pathlib import Path as _P

            from eurus.tools.base import ToolContext as _TC

            self.max_parallel_tools = 4
            self.iteration_budget = type("Budget", (), {"limit": 50})()
            self.runtime_budget_tracker = None
            self.context = _TC(workspace=_P("."))

            from eurus.tools.boundary import ToolExecutionBoundary as _B

            self.boundary = _B(ToolRegistry([]), self.context)

        def interrupt(self):
            return None

        async def run(self, objective):
            from eurus.models import ToolCall

            outcome = await self.boundary.execute(ToolCall("c1", "nope", {}))
            assert outcome.status is ToolExecutionStatus.FAILED
            yield AgentEvent.text_delta("done work")
            yield AgentEvent.finished("completed", None)

    path = tmp_path / "runtime.sqlite3"
    run_store = RunStore(path)
    engine = AgentEngine(RealAgent(), run_store=run_store)
    result = await engine.run("objective")
    assert result.run.status == RunStatus.COMPLETED
    attempts = ToolAttemptStore(run_store.database)
    rows = attempts.list_for_run(result.run.run_id)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "nope"
    assert rows[0]["status"] == "failed"
    checkpoint = run_store.latest_checkpoint(result.run.run_id)
    assert checkpoint is not None
    assert checkpoint["checkpoint_type"] == "runtime"
    assert checkpoint["state"]["objective"] == "objective"


@pytest.mark.asyncio
async def test_engine_reports_completed_unverified_without_verifier(tmp_path):
    class FakeAgent:
        max_parallel_tools = 4

        def __init__(self):
            self.iteration_budget = type("Budget", (), {"limit": 50})()
            self.runtime_budget_tracker = None

        def interrupt(self):
            return None

        async def run(self, objective):
            yield AgentEvent.finished("completed", None)

    result = await AgentEngine(FakeAgent()).run("objective")
    assert result.run.status == RunStatus.COMPLETED
    assert result.verified is None
    assert result.verification_status.value == "unverified"
    assert result.run.stop_reason == "completed_unverified"


@pytest.mark.asyncio
async def test_engine_verification_failure_includes_evidence(tmp_path):
    class FakeAgent:
        max_parallel_tools = 4

        def __init__(self):
            self.iteration_budget = type("Budget", (), {"limit": 50})()
            self.runtime_budget_tracker = None

        def interrupt(self):
            return None

        async def run(self, objective):
            yield AgentEvent.text_delta("output text")
            yield AgentEvent.finished("completed", None)

    async def reject(criteria, output):
        return False

    result = await AgentEngine(FakeAgent(), verifier=reject).run("objective")
    assert result.run.status == RunStatus.FAILED
    assert result.verification_status.value == "failed"
    assert result.verification is not None
    assert any(item.startswith("output_digest:") for item in result.verification.evidence)


def test_recovery_manager_blocks_uncertain_and_resumes_clean(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    run_store = RunStore(path)
    run = Run(owner_id="op", objective="recoverable work")
    run_store.create(run)
    transition_run(run, RunStatus.RUNNING)
    run_store.save(run, expected_status_version=0)
    run_store.save_checkpoint(
        run.run_id,
        {
            "state": {
                "objective": "recoverable work",
                "iteration": 2,
                "side_effect_status": "settled",
            }
        },
    )
    manager = RecoveryManager(run_store)
    plan = manager.inspect(run.run_id)
    assert plan.action.value == "resume_checkpoint"

    dispatched = []

    class FakeEngine:
        def __init__(self, resumed):
            dispatched.append(resumed.run_id)

        async def run(self, objective, run=None, budget=None, resumption_note=None):
            transition_run(run, RunStatus.RUNNING)
            run_store.save(run, expected_status_version=run.status_version - 1)
            transition_run(run, RunStatus.VERIFYING)
            run_store.save(run, expected_status_version=run.status_version - 1)
            transition_run(run, RunStatus.COMPLETED, stop_reason="completed")
            run_store.save(run, expected_status_version=run.status_version - 1)
            return "resumed"

    async def go():
        return await manager.resume(run.run_id, lambda resumed: FakeEngine(resumed))

    result = asyncio.run(go())
    assert result == "resumed"
    assert dispatched == [run.run_id]
    stored = run_store.get(run.run_id)
    assert stored.status == RunStatus.COMPLETED


def test_recovery_manager_blocks_manual_review_and_terminal(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    run_store = RunStore(path)
    orphan = Run(owner_id="op", objective="no checkpoint")
    run_store.create(orphan)
    transition_run(orphan, RunStatus.RUNNING)
    run_store.save(orphan, expected_status_version=0)
    manager = RecoveryManager(run_store)
    assert manager.inspect(orphan.run_id).action.value == "manual_review"
    with pytest.raises(RecoveryBlocked, match="manual review"):
        asyncio.run(manager.resume(orphan.run_id, lambda run: None))

    transition_run(orphan, RunStatus.FAILED)
    run_store.save(orphan, expected_status_version=orphan.status_version - 1)
    assert manager.inspect(orphan.run_id).action.value == "terminal"
    with pytest.raises(RecoveryBlocked, match="terminal"):
        asyncio.run(manager.resume(orphan.run_id, lambda run: None))


def test_recovery_manager_blocks_queued_run_with_uncertain_attempts(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    run_store = RunStore(path)
    run = Run(owner_id="op", objective="queued work")
    run_store.create(run)
    attempts = ToolAttemptStore(run_store.database)
    attempts.record(
        ToolExecutionOutcome(
            call_id="c9",
            tool_name="shell",
            status=ToolExecutionStatus.CANCELLED,
            is_error=True,
            uncertain=True,
            call_key="shell:{}",
            run_id=run.run_id,
            detail="cancelled mid mutation",
        )
    )
    manager = RecoveryManager(run_store)
    plan = manager.inspect(run.run_id)
    assert plan.action.value == "reconcile_unknown_outcome"
    assert plan.uncertain_attempts
    with pytest.raises(RecoveryBlocked, match="reconciliation"):
        asyncio.run(manager.resume(run.run_id, lambda run: None))
    assert run_store.get(run.run_id).status == RunStatus.QUEUED


def test_delegation_child_run_persists_and_reports_unverified(tmp_path):
    from eurus.agent.delegation import run_subagent
    from eurus.providers.testing import openai_message

    router, _ = scripted_router(json_responses=[openai_message("subagent result text")])
    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = Run(owner_id="owner", objective="parent objective")
    run_store.create(parent)
    transition_run(parent, RunStatus.RUNNING)
    run_store.save(parent, expected_status_version=0)
    context = ToolContext(
        workspace=Path("."),
        run_id=parent.run_id,
        owner_id=parent.owner_id,
        run_store=run_store,
        max_child_iterations=10,
    )

    async def go():
        return await run_subagent(
            router,
            ToolRegistry([]),
            context,
            "research the thing",
            max_iterations=5,
        )

    result = asyncio.run(go())
    assert result["verified"] is False
    assert result["child_run_id"]
    child = run_store.get(result["child_run_id"])
    assert child is not None
    assert child.parent_run_id == parent.run_id
    assert child.status == RunStatus.COMPLETED
    assert child.stop_reason == "completed_unverified"
    assert child.budget["max_iterations"] == 5


def test_delegation_blocked_when_parent_budget_allocates_no_children(tmp_path):
    from eurus.agent.delegation import run_subagent
    from eurus.providers.testing import openai_message

    router, _ = scripted_router(json_responses=[openai_message("unused")])
    context = ToolContext(workspace=Path("."), max_child_iterations=0)

    async def go():
        return await run_subagent(router, ToolRegistry([]), context, "task")

    with pytest.raises(ValueError, match="allocates"):
        asyncio.run(go())


def test_delegation_honors_parent_iteration_cap(tmp_path):
    from eurus.agent.delegation import run_subagent
    from eurus.providers.testing import openai_message

    router, _ = scripted_router(json_responses=[openai_message("ok")])
    context = ToolContext(workspace=Path("."), max_child_iterations=7)

    async def go():
        return await run_subagent(router, ToolRegistry([]), context, "task", max_iterations=30)

    result = asyncio.run(go())
    assert result["iteration_limit"] == 7
