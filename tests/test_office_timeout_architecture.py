from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from eurus.models import Message, ToolCall, Usage
from eurus.office.config import OfficeConfig
from eurus.office.errors import OfficeError
from eurus.office.models.task import OfficeTask
from eurus.office.renderers.base import verify_artifact_file
from eurus.office.specialist.budget import BudgetLimits
from eurus.office.specialist.manager import OfficeSpecialist
from eurus.office.storage import JobWorkspace, brief_from_payload, create_job_id
from eurus.office.tools import (
    TOOL_NAME,
    office_specialist_tool,
    specialist_deadline_seconds,
    verify_office_mutation,
)
from eurus.providers.base import ChatResponse
from eurus.runtime.contracts import ToolExecutionStatus
from eurus.security.approval import SessionScopedApprover, action_fingerprint
from eurus.tools.base import (
    MutationVerification,
    Tool,
    ToolContext,
    ToolRegistry,
    effective_tool_timeout,
)
from eurus.tools.boundary import ToolExecutionBoundary

docx = pytest.importorskip("docx")


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call-{name}-{time.monotonic_ns()}", name=name, arguments=dict(arguments))


def make_registry(tool: Tool) -> ToolRegistry:
    return ToolRegistry([tool])


async def _allow(call: ToolCall, tool: Tool) -> bool:
    return True


def sleeping_handler(delay: float, flag: list[bool] | None = None):
    async def handler(arguments: dict, context: ToolContext) -> str:
        if flag is not None:
            flag[0] = True
        await asyncio.sleep(delay)
        return "done"

    return handler


def mutating_tool(
    handler,
    *,
    timeout_seconds: float | None = None,
    verify=None,
    output_path: str = "outputs/report.docx",
) -> Tool:
    return Tool(
        name="artificial_mutation",
        description="test mutation",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        mutates=True,
        path_keys=("output_path",),
        timeout_seconds=timeout_seconds,
        verify_mutation=verify,
    )


def context_with_deadline(workspace: Path, timeout: float = 120.0, remaining: float | None = None):
    if remaining is None:
        return ToolContext(workspace=workspace, timeout=timeout)
    return ToolContext(
        workspace=workspace, timeout=timeout, deadline_at=time.monotonic() + remaining
    )


def write_real_docx(path: Path) -> None:
    document = docx.Document()
    document.add_heading("Verified", level=1)
    document.save(str(path))


@pytest.mark.asyncio
async def test_ordinary_tool_keeps_context_default_timeout(tmp_path: Path) -> None:
    flag = [False]
    tool = Tool(
        name="slow_read",
        description="read",
        parameters={"type": "object", "properties": {}},
        handler=sleeping_handler(1.0, flag),
    )
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path, timeout=0.25)
    )
    started = time.monotonic()
    outcome = await boundary.execute(call("slow_read"))
    elapsed = time.monotonic() - started
    assert outcome.status is ToolExecutionStatus.TIMEOUT
    assert flag == [True]
    assert 0.2 < elapsed < 0.9
    assert "timed out after 0.25s" in outcome.output


@pytest.mark.asyncio
async def test_tool_with_dedicated_timeout_ignores_shorter_context_timeout(tmp_path: Path) -> None:
    tool = mutating_tool(sleeping_handler(0.4), timeout_seconds=2.0)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path, timeout=0.1), approver=_allow
    )
    outcome = await boundary.execute(call("artificial_mutation"))
    assert outcome.status is ToolExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_user_deadline_overrides_longer_tool_timeout(tmp_path: Path) -> None:
    tool = mutating_tool(sleeping_handler(5.0), timeout_seconds=10.0)
    context = context_with_deadline(tmp_path, timeout=10.0, remaining=0.3)
    boundary = ToolExecutionBoundary(make_registry(tool), context, approver=_allow)
    started = time.monotonic()
    outcome = await boundary.execute(call("artificial_mutation"))
    elapsed = time.monotonic() - started
    assert outcome.status is ToolExecutionStatus.TIMEOUT
    assert elapsed < 2.5
    assert "timed out after" in outcome.output


def test_effective_timeout_is_capped_by_remaining_deadline(tmp_path: Path) -> None:
    tool = Tool(
        name="t",
        description="t",
        parameters={},
        handler=sleeping_handler(0.01),
        timeout_seconds=30.0,
    )
    plain = ToolContext(workspace=tmp_path, timeout=120.0)
    assert effective_tool_timeout(tool, plain) == 30.0
    bounded = context_with_deadline(tmp_path, timeout=120.0, remaining=5.0)
    assert effective_tool_timeout(tool, bounded) <= 5.0
    assert effective_tool_timeout(tool, bounded) > 0.0


def test_effective_timeout_falls_back_to_zero_when_deadline_passed(tmp_path: Path) -> None:
    tool = Tool(
        name="t",
        description="t",
        parameters={},
        handler=sleeping_handler(0.01),
        timeout_seconds=30.0,
    )
    expired = ToolContext(workspace=tmp_path, timeout=120.0, deadline_at=time.monotonic() - 1.0)
    assert effective_tool_timeout(tool, expired) == 0.0


def test_tool_timeout_validation_rejects_zero_and_negative() -> None:
    with pytest.raises(ValueError):
        Tool(
            name="bad",
            description="bad",
            parameters={},
            handler=sleeping_handler(0.01),
            timeout_seconds=0,
        )
    with pytest.raises(ValueError):
        Tool(
            name="bad",
            description="bad",
            parameters={},
            handler=sleeping_handler(0.01),
            timeout_seconds=-5.0,
        )


@pytest.mark.asyncio
async def test_exhausted_deadline_fails_fast_without_invoking_handler(tmp_path: Path) -> None:
    flag = [False]
    tool = mutating_tool(sleeping_handler(0.1, flag), timeout_seconds=30.0)
    expired = ToolContext(workspace=tmp_path, timeout=120.0, deadline_at=time.monotonic() - 1.0)
    boundary = ToolExecutionBoundary(make_registry(tool), expired, approver=_allow)
    outcome = await boundary.execute(call("artificial_mutation"))
    assert outcome.status is ToolExecutionStatus.TIMEOUT
    assert outcome.uncertain is False
    assert flag == [False]
    assert "no side effect was attempted" in outcome.detail


def test_specialist_deadline_is_clamped_to_agent_budget(tmp_path: Path) -> None:
    config = OfficeConfig()
    context = context_with_deadline(tmp_path, timeout=120.0, remaining=300.0)
    clamped = specialist_deadline_seconds({"deadline_seconds": 1800}, context, config)
    assert clamped == pytest.approx(295.0)
    assert specialist_deadline_seconds({"deadline_seconds": 120}, context, config) == 120.0
    unlimited = ToolContext(workspace=tmp_path, timeout=120.0)
    assert specialist_deadline_seconds({}, unlimited, config) == config.deadline_seconds


def test_specialist_deadline_refuses_tiny_remaining_budget(tmp_path: Path) -> None:
    context = context_with_deadline(tmp_path, timeout=120.0, remaining=10.0)
    with pytest.raises(OfficeError, match="execution budget"):
        specialist_deadline_seconds({}, context, OfficeConfig())


def test_child_budget_spec_prefers_child_deadline_over_context_timeout() -> None:
    from eurus.agent.child_runtime import child_budget_spec

    spec = child_budget_spec(max_iterations=5, timeout_seconds=120.0, deadline_seconds=295.0)
    assert spec.max_runtime_seconds == 295.0
    fallback = child_budget_spec(max_iterations=5, timeout_seconds=120.0)
    assert fallback.max_runtime_seconds == 120.0


def test_nested_child_context_inherits_deadline(tmp_path: Path) -> None:
    from dataclasses import replace

    parent = context_with_deadline(tmp_path, timeout=120.0, remaining=42.0)
    child = replace(parent, workspace=tmp_path / "child")
    assert child.deadline_at == parent.deadline_at
    assert child.remaining_seconds() == pytest.approx(parent.remaining_seconds(), abs=0.01)


@pytest.mark.asyncio
async def test_engine_stamps_and_restores_monotonic_deadline(tmp_path: Path) -> None:
    from eurus.runtime.engine import AgentEngine

    class _Agent:
        def __init__(self) -> None:
            self.context = ToolContext(workspace=tmp_path)

    agent = _Agent()
    engine = AgentEngine(agent)
    with engine._stamp_context_deadline(7.5):
        stamped = agent.context.deadline_at
        assert stamped is not None
        assert stamped > time.monotonic()
        assert agent.context.remaining_seconds() == pytest.approx(7.5, abs=0.5)
    assert agent.context.deadline_at is None


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_child_and_propagates(tmp_path: Path) -> None:
    outcomes: list[object] = []
    child_started = asyncio.Event()

    async def handler(arguments: dict, context: ToolContext) -> str:
        async def child() -> None:
            child_started.set()
            await asyncio.sleep(30)

        child_task = asyncio.create_task(child())
        try:
            await asyncio.sleep(30)
        finally:
            child_task.cancel()
            await asyncio.gather(child_task, return_exceptions=True)

    tool = mutating_tool(handler)
    boundary = ToolExecutionBoundary(
        make_registry(tool),
        ToolContext(workspace=tmp_path, timeout=60.0),
        approver=_allow,
        outcome_recorder=outcomes.append,
    )
    task = asyncio.create_task(boundary.execute(call("artificial_mutation")))
    await child_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is ToolExecutionStatus.CANCELLED
    assert outcome.uncertain is True
    assert asyncio.current_task() is not None
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert pending == []


@pytest.mark.asyncio
async def test_doomed_process_is_killed_on_timeout(tmp_path: Path) -> None:
    from eurus.office.renderers.base import _run_doomed_process

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    with pytest.raises(TimeoutError):
        await _run_doomed_process(process, 0.3)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_doomed_process_is_killed_on_cancellation(tmp_path: Path) -> None:
    from eurus.office.renderers.base import _run_doomed_process

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    task = asyncio.create_task(_run_doomed_process(process, 30.0))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_mutation_timeout_verified_completed_returns_success(tmp_path: Path) -> None:
    artifact = tmp_path / "report.docx"

    async def handler(arguments: dict, context: ToolContext) -> str:
        await asyncio.sleep(30)
        return "too late"

    async def verify(arguments: dict, context: ToolContext) -> MutationVerification:
        artifact.write_bytes(b"PK\x03\x04 verified payload bytes")
        return MutationVerification("completed", "artifact exists")

    tool = mutating_tool(handler, timeout_seconds=0.3, verify=verify)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=_allow
    )
    outcome = await boundary.execute(call("artificial_mutation"))
    assert outcome.status is ToolExecutionStatus.SUCCESS
    assert outcome.uncertain is False
    assert outcome.is_error is False
    assert "verified" in outcome.output


@pytest.mark.asyncio
async def test_mutation_timeout_verified_absent_allows_single_retry(tmp_path: Path) -> None:
    artifact = tmp_path / "outputs" / "report.docx"
    writes = 0

    async def handler(arguments: dict, context: ToolContext) -> str:
        nonlocal writes
        writes += 1
        await asyncio.sleep(0.6 if writes == 1 else 0.0)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"PK\x03\x04 generated artifact payload")
        return "written"

    async def verify(arguments: dict, context: ToolContext) -> MutationVerification:
        if artifact.is_file():
            return MutationVerification("completed", "artifact exists")
        return MutationVerification("absent", "artifact was not created")

    tool = mutating_tool(handler, timeout_seconds=0.3, verify=verify)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=_allow
    )
    first = await boundary.execute(call("artificial_mutation"))
    assert first.status is ToolExecutionStatus.FAILED
    assert first.uncertain is False
    assert "may be retried" in first.output
    second = await boundary.execute(call("artificial_mutation"))
    assert second.status is ToolExecutionStatus.SUCCESS
    assert writes == 2
    assert artifact.is_file()


@pytest.mark.asyncio
async def test_mutation_timeout_unknown_verification_blocks_repeat(tmp_path: Path) -> None:
    attempts = 0

    async def handler(arguments: dict, context: ToolContext) -> str:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(30)
        return "never"

    async def verify(arguments: dict, context: ToolContext) -> MutationVerification:
        return MutationVerification("unknown", "cannot determine the outcome")

    tool = mutating_tool(handler, timeout_seconds=0.25, verify=verify)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=_allow
    )
    first = await boundary.execute(call("artificial_mutation"))
    assert first.status is ToolExecutionStatus.TIMEOUT
    assert first.uncertain is True
    second = await boundary.execute(call("artificial_mutation"))
    assert second.status is ToolExecutionStatus.UNKNOWN
    assert second.uncertain is True
    assert "unresolved side effect" in second.output
    assert attempts == 1


@pytest.mark.asyncio
async def test_mutation_timeout_without_verifier_stays_uncertain(tmp_path: Path) -> None:
    async def handler(arguments: dict, context: ToolContext) -> str:
        await asyncio.sleep(30)
        return "never"

    tool = mutating_tool(handler, timeout_seconds=0.25)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=_allow
    )
    outcome = await boundary.execute(call("artificial_mutation"))
    assert outcome.status is ToolExecutionStatus.TIMEOUT
    assert outcome.uncertain is True
    repeat = await boundary.execute(call("artificial_mutation"))
    assert repeat.status is ToolExecutionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_verify_office_mutation_resolves_document_states(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path)
    unknown = await verify_office_mutation({"format": "word"}, context)
    assert unknown.status == "unknown"
    absent = await verify_office_mutation(
        {"format": "word", "output_path": "outputs/missing.docx"}, context
    )
    assert absent.status == "absent"
    target = tmp_path / "outputs" / "real.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_real_docx(target)
    completed = await verify_office_mutation(
        {"format": "word", "output_path": "outputs/real.docx"}, context
    )
    assert completed.status == "completed"
    target.write_bytes(b"corrupt bytes")
    invalid = await verify_office_mutation(
        {"format": "word", "output_path": "outputs/real.docx"}, context
    )
    assert invalid.status == "unknown"


def test_verify_artifact_file_rejects_non_office_payloads(tmp_path: Path) -> None:
    missing = tmp_path / "missing.docx"
    ok, detail = verify_artifact_file(missing, "docx")
    assert ok is False and "does not exist" in detail
    tiny = tmp_path / "tiny.docx"
    tiny.write_bytes(b"PK\x03\x04")
    ok, detail = verify_artifact_file(tiny, "docx")
    assert ok is False and "bytes" in detail
    garbage = tmp_path / "garbage.docx"
    garbage.write_bytes(b"not an office document at all" * 10)
    ok, detail = verify_artifact_file(garbage, "docx")
    assert ok is False and "ZIP" in detail
    valid = tmp_path / "valid.docx"
    write_real_docx(valid)
    ok, detail = verify_artifact_file(valid, "docx")
    assert ok is True


class _RecordingApprover:
    def __init__(self, decision: bool = True) -> None:
        self.decision = decision
        self.requests: list[ToolCall] = []

    async def __call__(self, call: ToolCall, tool: Tool) -> bool:
        self.requests.append(call)
        return self.decision


@pytest.mark.asyncio
async def test_approval_is_bound_to_call_and_denial_blocks_execution(tmp_path: Path) -> None:
    executed = [False]
    tool = mutating_tool(sleeping_handler(0.01, executed))
    approver = _RecordingApprover(decision=False)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=approver
    )
    denied = await boundary.execute(call("artificial_mutation"))
    assert denied.status is ToolExecutionStatus.DENIED
    assert executed == [False]
    approver.decision = True
    approved = await boundary.execute(call("artificial_mutation"))
    assert approved.status is ToolExecutionStatus.SUCCESS
    assert executed == [True]
    assert len(approver.requests) == 2
    assert all(request.name == "artificial_mutation" for request in approver.requests)
    assert action_fingerprint(approver.requests[0]) == action_fingerprint(approver.requests[1])


@pytest.mark.asyncio
async def test_retry_does_not_bypass_approval(tmp_path: Path) -> None:
    executed = [0]

    async def handler(arguments: dict, context: ToolContext) -> str:
        executed[0] += 1
        await asyncio.sleep(0.3)
        return "ok"

    tool = mutating_tool(handler, timeout_seconds=0.05)
    approver = _RecordingApprover(decision=True)
    boundary = ToolExecutionBoundary(
        make_registry(tool), ToolContext(workspace=tmp_path), approver=approver
    )
    first = await boundary.execute(call("artificial_mutation"))
    assert first.status is ToolExecutionStatus.TIMEOUT
    second = await boundary.execute(call("artificial_mutation"))
    assert second.status is ToolExecutionStatus.UNKNOWN
    assert executed[0] == 1
    assert len(approver.requests) == 1


@pytest.mark.asyncio
async def test_office_specialist_is_not_session_scoped(tmp_path: Path) -> None:
    asks = 0

    async def base(call: ToolCall, tool: Tool) -> bool:
        nonlocal asks
        asks += 1
        return True

    approver = SessionScopedApprover(base)
    office_call = call(TOOL_NAME, format="word", objective="doc")
    office_tool = Tool(
        name=TOOL_NAME,
        description="office",
        parameters={},
        handler=sleeping_handler(0.0),
        requires_approval=True,
    )
    assert await approver(office_call, office_tool) is True
    assert await approver(office_call, office_tool) is True
    assert asks == 2


OFFICE_ANALYSIS = {
    "audience": "ops",
    "tone": "formal",
    "style": "business",
    "requirements": [],
    "acceptance_criteria": ["clean"],
    "research_needed": False,
    "research_questions": [],
    "language": "en",
}

OFFICE_RESEARCH = {
    "facts": [{"statement": "Fact one", "kind": "verified", "source": "Source"}],
    "sources": ["Source"],
    "outline": ["Summary"],
    "gaps": [],
}

OFFICE_DESIGN = {
    "direction": "corporate",
    "document_style": "business",
    "guidance": [],
    "palette_notes": "",
    "typography_notes": "",
    "layout_plan": [],
}

OFFICE_SPEC = {
    "kind": "document",
    "metadata": {"title": "Report", "author": "Eurus"},
    "style": "business",
    "include_toc": False,
    "blocks": [
        {"kind": "heading", "level": 1, "text": "Summary"},
        {"kind": "paragraph", "text": "Opening paragraph of the report body."},
    ],
}

OFFICE_CRITIC = {"content_score": 92, "summary": "Fine.", "defects": []}

OFFICE_ACCEPT = {
    "accepted": True,
    "content_score": 93,
    "missing_content": [],
    "reasons": [],
    "client_summary": "OK",
}


class SleepyRouter:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def chat(self, messages, *, tools=(), temperature=None, max_tokens=None):
        system = messages[0].content
        user = messages[1].content
        await asyncio.sleep(self.delay)
        if "evaluating the finished document" in user:
            payload = OFFICE_ACCEPT
        elif "Critic Agent" in system:
            payload = OFFICE_CRITIC
        elif "Client Agent" in system:
            payload = OFFICE_ANALYSIS
        elif "Research Agent" in system:
            payload = OFFICE_RESEARCH
        elif "Design Agent" in system:
            payload = OFFICE_DESIGN
        elif "Production Agent" in system:
            payload = OFFICE_SPEC
        else:
            raise AssertionError("unexpected role prompt")
        return ChatResponse(
            message=Message.assistant(json.dumps(payload)), usage=Usage(), finish_reason="stop"
        )


def office_arguments(output_path: str) -> dict[str, object]:
    return {
        "format": "word",
        "objective": "A compact management report.",
        "output_path": output_path,
        "language": "en",
    }


@pytest.mark.asyncio
async def test_specialist_outlives_legacy_global_tool_timeout(tmp_path: Path, monkeypatch) -> None:
    from eurus.office import tools as office_tools

    monkeypatch.setattr(office_tools, "OFFICE_DEADLINE_MARGIN_SECONDS", 0.05)
    monkeypatch.setattr(office_tools, "MIN_SPECIALIST_DEADLINE_SECONDS", 0.1)
    config = OfficeConfig(tool_timeout_seconds=30.0, deadline_seconds=3600.0)
    tool = office_specialist_tool(SleepyRouter(0.05), ToolRegistry(), config)
    context = context_with_deadline(tmp_path, timeout=0.3, remaining=2.0)
    boundary = ToolExecutionBoundary(
        ToolRegistry([tool]), context, approver=_RecordingApprover(decision=True)
    )
    outcome = await boundary.execute(call(TOOL_NAME, **office_arguments("outputs/long.docx")))
    assert outcome.status is ToolExecutionStatus.SUCCESS
    package = json.loads(outcome.output)
    assert package["status"] == "completed"
    assert Path(package["artifact_path"]).is_file()


@pytest.mark.asyncio
async def test_specialist_stops_at_user_deadline_with_clean_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from eurus.office import tools as office_tools

    monkeypatch.setattr(office_tools, "OFFICE_DEADLINE_MARGIN_SECONDS", 0.5)
    monkeypatch.setattr(office_tools, "MIN_SPECIALIST_DEADLINE_SECONDS", 0.1)
    config = OfficeConfig(tool_timeout_seconds=30.0, deadline_seconds=3600.0)
    tool = office_specialist_tool(SleepyRouter(0.3), ToolRegistry(), config)
    context = context_with_deadline(tmp_path, timeout=30.0, remaining=2.0)
    boundary = ToolExecutionBoundary(
        ToolRegistry([tool]), context, approver=_RecordingApprover(decision=True)
    )
    started = time.monotonic()
    outcome = await boundary.execute(call(TOOL_NAME, **office_arguments("outputs/slow.docx")))
    elapsed = time.monotonic() - started
    assert outcome.status is ToolExecutionStatus.FAILED
    assert outcome.uncertain is False
    assert elapsed < 2.0
    assert "deadline" in outcome.output
    assert not (tmp_path / "outputs" / "slow.docx").exists()


def test_office_specialist_tool_carries_dedicated_timeout() -> None:
    config = OfficeConfig(tool_timeout_seconds=1800.0)
    tool = office_specialist_tool(object(), ToolRegistry(), config)
    assert tool.name == TOOL_NAME
    assert tool.timeout_seconds == 1800.0
    assert tool.verify_mutation is not None
    default_config = OfficeConfig()
    plain_tool = office_specialist_tool(object(), ToolRegistry(), default_config)
    assert plain_tool.timeout_seconds == default_config.tool_timeout_seconds


@pytest.mark.asyncio
async def test_specialist_budget_deadline_fails_job_cleanly(tmp_path: Path) -> None:
    router = SleepyRouter(0.0)
    context = ToolContext(workspace=tmp_path)
    brief = brief_from_payload(
        {
            "objective": "A compact management report.",
            "format": "docx",
            "output_path": "outputs/deadline.docx",
        }
    )
    task = OfficeTask(task_id="task-1", job_id=create_job_id(), brief=brief)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=JobWorkspace.create(task.job_id),
        limits=BudgetLimits(max_total_model_calls=60, max_revision_cycles=2, deadline_seconds=5.0),
    )
    specialist.budget.started = time.monotonic() - 10.0
    with pytest.raises(OfficeError, match="deadline"):
        await specialist.run()
    assert specialist.state.stage.value == "failed"
