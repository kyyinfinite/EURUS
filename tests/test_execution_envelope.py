from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from eurus.execution.envelope import (
    ApprovalScope,
    EnvelopeError,
    ExecutionEnvelope,
    IsolationBackend,
    NetworkProfile,
    ResourceBudget,
    envelope_violation,
)
from eurus.models import ToolCall
from eurus.runtime.cancellation import CancellationToken
from eurus.runtime.contracts import ToolExecutionStatus
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary


def _envelope(**overrides) -> ExecutionEnvelope:
    defaults = {
        "workspace_root": "/tmp/eurus-ws",
        "backend": IsolationBackend.CONTAINER,
    }
    defaults.update(overrides)
    return ExecutionEnvelope.authorize(**defaults)


def test_envelope_is_immutable_after_authorization():
    envelope = _envelope()
    with pytest.raises(AttributeError):
        envelope.run_id = "other"


def test_authorize_defaults_fail_closed():
    envelope = _envelope()
    assert envelope.network_profile is NetworkProfile.OFFLINE
    assert "shell.execute" in envelope.capabilities
    assert envelope.run_id == ""
    assert envelope.ttl_seconds is None
    assert not envelope.expired()


def test_authorize_rejects_empty_workspace_root():
    with pytest.raises(EnvelopeError, match="workspace_root"):
        ExecutionEnvelope.authorize(workspace_root="", backend=IsolationBackend.CONTAINER)


def test_authorize_rejects_host_with_network_profile():
    with pytest.raises(EnvelopeError, match="host isolation"):
        ExecutionEnvelope.authorize(
            workspace_root="/tmp/w",
            backend=IsolationBackend.HOST,
            network_profile=NetworkProfile.WEB_READ,
        )


def test_authorize_rejects_invalid_identifiers():
    with pytest.raises(EnvelopeError, match="envelope_id"):
        ExecutionEnvelope.authorize(
            workspace_root="/tmp/w", backend=IsolationBackend.CONTAINER, envelope_id="bad id!"
        )
    with pytest.raises(EnvelopeError, match="run_id"):
        ExecutionEnvelope.authorize(
            workspace_root="/tmp/w", backend=IsolationBackend.CONTAINER, run_id="bad run id"
        )
    with pytest.raises(EnvelopeError, match="workspace_id"):
        ExecutionEnvelope(
            envelope_id="env-1",
            workspace_id="",
            workspace_root="/tmp/w",
            backend=IsolationBackend.CONTAINER,
        )


def test_with_run_stamps_new_instance_and_keeps_original():
    envelope = _envelope()
    stamped = envelope.with_run("run-1", attempt=3)
    assert stamped.run_id == "run-1"
    assert stamped.attempt == 3
    assert envelope.run_id == ""
    assert stamped.envelope_id == envelope.envelope_id
    with pytest.raises(EnvelopeError):
        envelope.with_run("")


def test_expiry_honours_ttl_and_timezone():
    envelope = _envelope(ttl_seconds=60.0)
    now = datetime.now(UTC)
    assert not envelope.expired(now)
    later = now + timedelta(seconds=61)
    assert envelope.expired(later)
    stale = envelope.__class__(
        envelope_id=envelope.envelope_id,
        workspace_id=envelope.workspace_id,
        workspace_root=envelope.workspace_root,
        backend=envelope.backend,
        created_at=now - timedelta(seconds=120),
        ttl_seconds=60.0,
    )
    assert stale.expired(now)


def test_envelope_rejects_bad_capability_names():
    with pytest.raises(EnvelopeError, match="capability"):
        _envelope(capabilities=frozenset({"SHELL"}))


def test_resource_budget_validation():
    with pytest.raises(EnvelopeError):
        ResourceBudget(memory_mb=1)
    with pytest.raises(EnvelopeError):
        ResourceBudget(cpu_limit=0.01)
    with pytest.raises(EnvelopeError):
        ResourceBudget(pids_limit=1)
    with pytest.raises(EnvelopeError):
        ResourceBudget(timeout_seconds=0)
    with pytest.raises(EnvelopeError):
        ResourceBudget(max_output_bytes=0)


def test_violation_none_when_clean():
    envelope = _envelope()
    assert (
        envelope_violation(envelope, tool_name="shell", required_capabilities=("shell.execute",))
        is None
    )


def test_violation_when_cancelled():
    token = CancellationToken()
    envelope = _envelope(cancellation=token)
    assert envelope_violation(envelope, tool_name="read") is None
    token.cancel()
    message = envelope_violation(envelope, tool_name="read")
    assert message is not None and "cancelled" in message


def test_violation_when_capability_missing():
    envelope = _envelope(capabilities=frozenset({"filesystem.read"}))
    message = envelope_violation(
        envelope, tool_name="shell", required_capabilities=("shell.execute",)
    )
    assert message is not None and "shell.execute" in message


def test_violation_on_run_mismatch():
    envelope = _envelope().with_run("run-1")
    assert envelope_violation(envelope, tool_name="read", run_id="run-1") is None
    assert envelope_violation(envelope, tool_name="read", run_id=None) is None
    message = envelope_violation(envelope, tool_name="read", run_id="run-2")
    assert message is not None and "does not match" in message


def test_violation_when_expired():
    envelope = _envelope(ttl_seconds=60.0)
    later = datetime.now(UTC) + timedelta(seconds=120)
    message = envelope_violation(envelope, tool_name="read", now=later)
    assert message is not None and "expired" in message


def test_violation_from_approval_scope():
    envelope = _envelope(approval_scope=ApprovalScope(denied_tools=frozenset({"shell"})))
    message = envelope_violation(envelope, tool_name="shell")
    assert message is not None and "approval scope" in message
    open_scope = _envelope(approval_scope=ApprovalScope(allowed_tools=frozenset({"shell"})))
    assert envelope_violation(open_scope, tool_name="shell") is None
    assert envelope_violation(open_scope, tool_name="other") is not None


def test_approval_scope_permits_matrix():
    scope = ApprovalScope()
    assert scope.permits("anything")
    assert not ApprovalScope(denied_tools=frozenset({"a"})).permits("a")
    assert ApprovalScope(allowed_tools=frozenset({"a"})).permits("a")
    assert not ApprovalScope(allowed_tools=frozenset({"a"})).permits("b")


def _tool(name: str, *, caps: tuple[str, ...] = (), mutates: bool = False) -> tuple[Tool, list]:
    seen: list = []

    async def handler(arguments, context):
        seen.append(context.envelope)
        return "ok"

    tool = Tool(
        name,
        f"{name} description",
        {"type": "object", "properties": {}, "additionalProperties": False},
        handler,
        mutates,
        False,
        True,
        False,
        (),
        caps,
    )
    return tool, seen


def _boundary(tmp_path, envelope, tool):
    registry = ToolRegistry([tool])
    context = ToolContext(workspace=tmp_path, envelope=envelope)
    return ToolExecutionBoundary(registry, context)


def test_boundary_denies_missing_capability(tmp_path):
    envelope = _envelope(capabilities=frozenset({"filesystem.read"}))
    tool, _seen = _tool("shell", caps=("shell.execute",))
    boundary = _boundary(tmp_path, envelope, tool)
    outcome = asyncio.run(boundary.execute(ToolCall("c1", "shell", {})))
    assert outcome.status is ToolExecutionStatus.DENIED
    assert "missing capabilities" in outcome.output
    assert "shell.execute" in outcome.output


def test_boundary_denies_run_mismatch(tmp_path):
    envelope = _envelope().with_run("run-1")
    tool, _seen = _tool("reader")
    boundary = _boundary(tmp_path, envelope, tool)
    outcome = asyncio.run(boundary.execute(ToolCall("c1", "reader", {}), run_id="run-2"))
    assert outcome.status is ToolExecutionStatus.DENIED
    assert "does not match" in outcome.output


def test_boundary_reports_cancelled_envelope(tmp_path):
    token = CancellationToken()
    token.cancel()
    envelope = _envelope(cancellation=token)
    tool, _seen = _tool("reader")
    boundary = _boundary(tmp_path, envelope, tool)
    outcome = asyncio.run(boundary.execute(ToolCall("c1", "reader", {})))
    assert outcome.status is ToolExecutionStatus.CANCELLED


def test_boundary_passes_envelope_to_handler(tmp_path):
    envelope = _envelope()
    tool, seen = _tool("reader")
    boundary = _boundary(tmp_path, envelope, tool)
    outcome = asyncio.run(boundary.execute(ToolCall("c1", "reader", {})))
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert seen[0] is envelope


def test_boundary_legacy_behaviour_without_envelope(tmp_path):
    tool, _seen = _tool("reader")
    registry = ToolRegistry([tool])
    context = ToolContext(workspace=tmp_path)
    boundary = ToolExecutionBoundary(registry, context)
    outcome = asyncio.run(boundary.execute(ToolCall("c1", "reader", {}), run_id="whatever"))
    assert outcome.status is ToolExecutionStatus.SUCCESS


def test_boundary_emits_violation_event(tmp_path):
    events: list[tuple[str, dict]] = []

    async def recorder(event_type, payload):
        events.append((event_type, payload))

    envelope = _envelope(capabilities=frozenset({"filesystem.read"}))
    tool, _seen = _tool("shell", caps=("shell.execute",))
    registry = ToolRegistry([tool])
    context = ToolContext(workspace=tmp_path, envelope=envelope)
    boundary = ToolExecutionBoundary(registry, context, event_recorder=recorder)
    asyncio.run(boundary.execute(ToolCall("c1", "shell", {})))
    assert events[-1][0] == "envelope.violation"
    assert events[-1][1]["envelope_id"] == envelope.envelope_id


def test_boundary_started_event_carries_envelope_id(tmp_path):
    events: list[tuple[str, dict]] = []

    async def recorder(event_type, payload):
        events.append((event_type, payload))

    envelope = _envelope()
    tool, _seen = _tool("reader")
    registry = ToolRegistry([tool])
    context = ToolContext(workspace=tmp_path, envelope=envelope)
    boundary = ToolExecutionBoundary(registry, context, event_recorder=recorder)
    asyncio.run(boundary.execute(ToolCall("c1", "reader", {})))
    started = [payload for kind, payload in events if kind == "tool.started"]
    assert started and started[0]["envelope_id"] == envelope.envelope_id
