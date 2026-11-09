import asyncio
from pathlib import Path

import pytest

from eurus.agent.child_runtime import child_budget_spec
from eurus.agent.delegation import run_subagent
from eurus.models import ToolCall
from eurus.providers.testing import openai_message, scripted_router
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.state_machine import transition_run
from eurus.sessions import SessionStore
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary
from eurus.tools.schema import validate_arguments

EMPTY = {"type": "object", "properties": {}}


def _parent_context(run_store, parent_run, **overrides):
    base = dict(
        workspace=Path("."),
        run_id=parent_run.run_id,
        owner_id=parent_run.owner_id,
        run_store=run_store,
        max_child_iterations=10,
    )
    base.update(overrides)
    return ToolContext(**base)


def _make_parent_run(run_store):
    parent = Run(owner_id="owner", objective="parent objective")
    run_store.create(parent)
    transition_run(parent, RunStatus.RUNNING)
    run_store.save(parent, expected_status_version=0)
    return parent


@pytest.mark.asyncio
async def test_subagent_records_tool_attempts_and_events_on_child_run(tmp_path):

    call_arguments = {"query": "eurus runtime", "limit": 3}
    router, _ = scripted_router(
        json_responses=[
            openai_message("", calls=[("c1", "web_search", call_arguments)]),
            openai_message("search findings summary"),
        ]
    )
    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = _make_parent_run(run_store)
    registry = ToolRegistry(
        [
            Tool(
                "web_search",
                "search",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                lambda arguments, context: asyncio.sleep(0, result="result for query"),
            )
        ]
    )
    context = _parent_context(run_store, parent)
    result = await run_subagent(router, registry, context, "research it", max_iterations=5)
    child_id = result["child_run_id"]
    assert child_id is not None
    assert result["durable"] is True
    attempt_store = ToolAttemptStore(run_store.database)
    attempts = attempt_store.list_for_run(child_id)
    assert attempts, "child tool attempts must be durably recorded"
    assert attempts[0]["tool_name"] == "web_search"
    assert attempts[0]["run_id"] == child_id
    events = EventStore(run_store.database)
    assert events.last_sequence(child_id) > 0
    child = run_store.get(child_id)
    assert child.parent_run_id == parent.run_id
    assert child.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_subagent_finalizes_child_run_not_orphaned(tmp_path):
    class SlowRouter:
        async def stream(self, messages, tools=None):
            await asyncio.sleep(30)
            raise RuntimeError("unreachable")

        async def chat(self, messages, tools=None, **kwargs):
            await asyncio.sleep(30)
            raise RuntimeError("unreachable")

    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = _make_parent_run(run_store)
    context = _parent_context(run_store, parent, timeout=0.4)
    task = asyncio.create_task(
        run_subagent(SlowRouter(), ToolRegistry([]), context, "slow task", max_iterations=5)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    children = [run for run in run_store.list_runs(limit=10) if run.parent_run_id == parent.run_id]
    assert len(children) == 1
    assert children[0].status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_subagent_deadline_is_enforced_on_child_run(tmp_path):
    class SlowRouter:
        async def stream(self, messages, tools=None):
            await asyncio.sleep(10)
            raise RuntimeError("unreachable")

        async def chat(self, messages, tools=None, **kwargs):
            await asyncio.sleep(10)
            raise RuntimeError("unreachable")

    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = _make_parent_run(run_store)
    context = _parent_context(run_store, parent, timeout=10.0)
    result = await run_subagent(
        SlowRouter(),
        ToolRegistry([]),
        context,
        "bounded task",
        max_iterations=50,
        deadline_seconds=0.3,
    )
    child = run_store.get(result["child_run_id"])
    assert child.status is RunStatus.FAILED
    assert child.stop_reason == "runtime_budget_exhausted"


def test_child_budget_spec_clamps_runtime_and_tool_calls():
    spec = child_budget_spec(max_iterations=10, timeout_seconds=60.0, deadline_seconds=5.0)
    assert spec.max_runtime_seconds == 5.0
    assert spec.max_tool_calls == 80
    assert spec.max_child_runs == 0
    clamped = child_budget_spec(max_iterations=100, timeout_seconds=60.0)
    assert clamped.max_tool_calls == 400


@pytest.mark.asyncio
async def test_boundary_truncates_successful_output(tmp_path):
    payload = "x" * 5000

    async def handler(arguments, context):
        return payload

    tool = Tool("blob", "big output", EMPTY, handler)
    boundary = ToolExecutionBoundary(
        ToolRegistry([tool]),
        ToolContext(workspace=tmp_path, max_output=500),
    )
    outcome = await boundary.execute(ToolCall("c1", "blob", {}))
    assert len(outcome.output) <= 700
    assert "characters truncated" in outcome.output
    assert outcome.status.value == "success"


@pytest.mark.asyncio
async def test_boundary_blocks_repeat_mutation_using_durable_uncertain_keys(tmp_path):
    database = tmp_path / "runs.sqlite3"
    run_store = RunStore(database)
    attempt_store = ToolAttemptStore(database)
    parent = run_store.create(Run(owner_id="o", objective="obj"))
    parent_run_id = parent.run_id
    from datetime import UTC, datetime

    from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus

    uncertain = ToolExecutionOutcome(
        call_id="c0",
        tool_name="mut",
        status=ToolExecutionStatus.TIMEOUT,
        output="error: timed out",
        is_error=True,
        uncertain=True,
        call_key="mut:{}",
        run_id=parent_run_id,
        risk_level="high",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        detail="uncertain",
    )
    attempt_store.record(uncertain)

    async def handler(arguments, context):
        return "mutated"

    tool = Tool("mut", "mutator", EMPTY, handler, mutates=True)
    boundary = ToolExecutionBoundary(
        ToolRegistry([tool]),
        ToolContext(workspace=tmp_path),
        uncertain_keys_provider=lambda run_id: [
            row["call_key"] for row in attempt_store.uncertain_keys(run_id)
        ],
    )
    outcome = await boundary.execute(ToolCall("c1", "mut", {}), run_id=parent_run_id)
    assert outcome.is_error is True
    assert outcome.status.value == "unknown"
    assert "reconcile" in outcome.output


def test_schema_validates_string_length_bounds():
    schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "minLength": 2, "maxLength": 5},
        },
        "required": ["task"],
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {"task": "ok"}) == []
    assert validate_arguments(schema, {"task": "x"})
    assert validate_arguments(schema, {"task": "too long"})


@pytest.mark.asyncio
async def test_runtime_requires_engine_in_production_mode(tmp_path):
    from eurus.runtime.application import AgentRuntime

    store = SessionStore(tmp_path / "sessions.sqlite3")
    with pytest.raises(ValueError, match="engine_factory"):
        AgentRuntime(lambda session_id: None, store, require_engine=True)


@pytest.mark.asyncio
async def test_coder_delegation_creates_child_run_and_enforces_allowance(tmp_path):
    from eurus.coding.worktree import IsolatedWorktree

    class FakeWorktree(IsolatedWorktree):
        def add_all(self):
            return []

        def diff_against_head(self):
            return ""

        def cleanup(self):
            return "removed"

    async def apply_handler(arguments, context):
        return "applied"

    apply_patch_tool = Tool(
        "apply_patch",
        "apply patch",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        apply_handler,
        mutates=True,
    )
    router, _ = scripted_router(json_responses=[openai_message("did nothing")])
    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = _make_parent_run(run_store)
    context = _parent_context(run_store, parent, max_child_iterations=4)
    worktree = FakeWorktree(root=tmp_path, path=tmp_path, identifier="wt-test")
    from eurus.agent.coder_tools import run_coder_subagent

    result = await run_coder_subagent(
        router,
        ToolRegistry([apply_patch_tool]),
        context,
        worktree,
        "make a change",
        max_iterations=30,
    )
    assert result["iterations_used"] <= 4
    child = run_store.get(result["child_run_id"])
    assert child is not None
    assert child.parent_run_id == parent.run_id


@pytest.mark.asyncio
async def test_coder_delegation_rejects_dangerous_patch_as_error(tmp_path):
    from eurus.coding.worktree import IsolatedWorktree

    async def apply_handler(arguments, context):
        return "applied"

    apply_patch_tool = Tool(
        "apply_patch",
        "apply patch",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        apply_handler,
        mutates=True,
    )
    router, _ = scripted_router(json_responses=[openai_message("wrote secrets")])
    run_store = RunStore(tmp_path / "runs.sqlite3")
    parent = _make_parent_run(run_store)
    context = _parent_context(run_store, parent)

    class SecretPatchWorktree(IsolatedWorktree):
        def add_all(self):
            return []

        def diff_against_head(self):
            return "+password = 'sk-abcdefghij1234567890abcd'"

        def cleanup(self):
            return "removed"

    from eurus.agent.coder_tools import run_coder_subagent
    from eurus.errors import ToolError

    worktree = SecretPatchWorktree(root=tmp_path, path=tmp_path, identifier="wt-secret")
    with pytest.raises(ToolError, match="rejected by safety review"):
        await run_coder_subagent(
            router,
            ToolRegistry([apply_patch_tool]),
            context,
            worktree,
            "write a secret",
        )
