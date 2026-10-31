from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus.errors import ToolError
from eurus.execution.envelope import envelope_violation
from eurus.models import ToolCall, ToolResult
from eurus.providers.base import INVALID_ARGUMENTS_KEY
from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.security.approval import action_fingerprint
from eurus.security.redaction import redact_text
from eurus.tools.base import Tool, ToolContext, ToolOutput, ToolRegistry, truncate_text
from eurus.tools.schema import validate_arguments

log = logging.getLogger(__name__)

Approver = Callable[[ToolCall, Tool], Awaitable[bool]]
OutcomeRecorder = Callable[[ToolExecutionOutcome], Awaitable[None] | None]
BoundaryEventRecorder = Callable[[str, dict[str, Any]], Awaitable[None] | None]
UncertainKeyProvider = Callable[[str], list[str]]
BeforeMutationHook = Callable[[ToolCall, Tool], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class AttemptStarted:
    """Write-ahead record emitted just before a mutating tool executes."""

    attempt_id: str
    tool_name: str
    call_key: str
    run_id: str | None
    risk_level: str


AttemptStartedRecorder = Callable[[AttemptStarted], Awaitable[None] | None]


class ToolBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RiskClassification:
    risk_level: str
    requires_approval: bool
    reason: str


def classify_risk(tool: Tool) -> RiskClassification:
    if tool.mutates:
        return RiskClassification("high", True, "tool mutates persistent state")
    if tool.requires_approval:
        return RiskClassification("high", True, "tool is flagged as approval-gated")
    if tool.interactive:
        return RiskClassification("medium", False, "tool is interactive")
    return RiskClassification("low", False, "read-only tool")


def call_key(call: ToolCall) -> str:
    return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"


OUTCOME_ERROR_CODES = {
    "denied": "approval_denied",
    "timeout": "timeout",
    "cancelled": "cancelled",
    "unknown": "uncertain",
    "pending_approval": "approval_unavailable",
}


class ToolExecutionBoundary:
    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolContext,
        *,
        approver: Approver | None = None,
        outcome_recorder: OutcomeRecorder | None = None,
        event_recorder: BoundaryEventRecorder | None = None,
        budget_check: Callable[[int], bool] | None = None,
        uncertain_keys_provider: UncertainKeyProvider | None = None,
        attempt_started_recorder: AttemptStartedRecorder | None = None,
        before_mutation: BeforeMutationHook | None = None,
    ) -> None:
        self.registry = registry
        self.context = context
        self.approver = approver
        self.outcome_recorder = outcome_recorder
        self.event_recorder = event_recorder
        self.budget_check = budget_check
        self.uncertain_keys_provider = uncertain_keys_provider
        self.attempt_started_recorder = attempt_started_recorder
        self.before_mutation = before_mutation
        self._path_locks: dict[str, asyncio.Lock] = {}
        self._global_mutation_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Lock] = {}
        self._inflight_holders: dict[str, int] = {}
        self._path_lock_holders: dict[str, int] = {}
        self._uncertain_keys: set[str] = set()
        self._durable_uncertain_loaded: set[str] = set()

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_recorder is None:
            return
        try:
            recorded = self.event_recorder(event_type, payload)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception:
            log.warning("boundary event recording failed", exc_info=True)

    async def _record(self, outcome: ToolExecutionOutcome) -> None:
        if outcome.uncertain:
            self._uncertain_keys.add(outcome.call_key)
        if self.outcome_recorder is None:
            return
        try:
            recorded = self.outcome_recorder(outcome)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception:
            log.warning("boundary outcome recording failed", exc_info=True)

    async def _record_started(self, started: AttemptStarted) -> None:
        """Persist the write-ahead pending row before the tool runs."""
        if self.attempt_started_recorder is None:
            return
        try:
            recorded = self.attempt_started_recorder(started)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception:
            log.warning("boundary write-ahead recording failed", exc_info=True)

    async def _run_before_mutation(self, call: ToolCall, tool: Tool) -> None:
        """Capture pre-images (checkpoints) before an approved mutation starts."""
        if self.before_mutation is None or not tool.mutates:
            return
        try:
            recorded = self.before_mutation(call, tool)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception:
            log.warning("before-mutation checkpoint failed", exc_info=True)

    def _durable_uncertain_keys(self, run_id: str | None) -> set[str]:
        if run_id is None or self.uncertain_keys_provider is None:
            return set()
        if run_id in self._durable_uncertain_loaded:
            return set()
        try:
            keys = set(self.uncertain_keys_provider(run_id))
        except Exception:
            log.warning("durable uncertain key lookup failed", exc_info=True)
            return set()
        self._durable_uncertain_loaded.add(run_id)
        return keys

    def _reconcile(self, call: ToolCall, tool: Tool, status: ToolExecutionStatus) -> str:
        if status is ToolExecutionStatus.TIMEOUT:
            return (
                f"mutation tool {tool.name} timed out; the side effect may or may not have "
                "been applied; verify the target state before repeating this call"
            )
        if status is ToolExecutionStatus.CANCELLED:
            return (
                f"mutation tool {tool.name} was cancelled mid-flight; the side effect is "
                "unresolved; reconcile the target state before repeating this call"
            )
        return f"mutation tool {tool.name} ended in status {status.value}; side effect unknown"

    def _lock_keys(self, call: ToolCall, tool: Tool) -> list[str]:
        if not tool.mutates:
            return []
        values: list[str] = []
        for key in tool.path_keys:
            value = call.arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self.context.workspace / path
            values.append(str(path.resolve(strict=False)))
        if not values:
            return ["__eurus_mutation__"]
        return sorted(set(values))

    def _finished_event(
        self,
        call: ToolCall,
        tool: Tool,
        attempt: str,
        run_id: str | None,
        status: ToolExecutionStatus,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": tool.name,
            "call_id": call.id,
            "attempt_id": attempt,
            "run_id": run_id,
            "status": status.value,
        }
        envelope = self.context.envelope
        if envelope is not None:
            payload["envelope_id"] = envelope.envelope_id
        return payload

    def _denied(
        self,
        call: ToolCall,
        status: ToolExecutionStatus,
        detail: str,
        *,
        run_id: str | None = None,
        started_at: Any = None,
    ) -> ToolExecutionOutcome:
        return ToolExecutionOutcome(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=truncate_text(f"error: {detail}", self.context.max_output),
            is_error=True,
            uncertain=status is ToolExecutionStatus.UNKNOWN,
            call_key=call_key(call),
            run_id=run_id,
            risk_level="high",
            started_at=started_at,
            detail=detail,
        )

    def _tool_result(self, call: ToolCall, outcome: ToolExecutionOutcome) -> ToolResult:
        return ToolResult(
            call.id,
            call.name,
            outcome.output,
            outcome.is_error,
            OUTCOME_ERROR_CODES.get(outcome.status.value, ""),
        )

    async def execute(
        self,
        call: ToolCall,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolExecutionOutcome:
        del step_id
        attempt = attempt_id or uuid.uuid4().hex
        tool = self.registry.get(call.name)
        if tool is None:
            known = ", ".join(self.registry.names()) or "none"
            outcome = self._denied(
                call,
                ToolExecutionStatus.FAILED,
                f"unknown tool '{call.name}'. available tools: {known}",
                run_id=run_id,
            )
            await self._record(outcome)
            return outcome
        if INVALID_ARGUMENTS_KEY in call.arguments:
            outcome = self._denied(
                call,
                ToolExecutionStatus.FAILED,
                "arguments are not valid JSON, send a JSON object",
                run_id=run_id,
            )
            await self._record(outcome)
            return outcome
        problems = validate_arguments(tool.parameters, call.arguments)
        if problems:
            outcome = self._denied(
                call,
                ToolExecutionStatus.FAILED,
                "invalid arguments: " + "; ".join(problems),
                run_id=run_id,
            )
            await self._record(outcome)
            return outcome
        envelope = self.context.envelope
        if envelope is not None:
            violation = envelope_violation(
                envelope,
                tool_name=tool.name,
                required_capabilities=tool.required_capabilities,
                run_id=run_id,
            )
            if violation is not None:
                cancelled = envelope.cancellation is not None and envelope.cancellation.cancelled
                outcome = self._denied(
                    call,
                    ToolExecutionStatus.CANCELLED if cancelled else ToolExecutionStatus.DENIED,
                    violation,
                    run_id=run_id,
                )
                await self._record(outcome)
                await self._emit(
                    "envelope.violation",
                    {
                        "tool": tool.name,
                        "envelope_id": envelope.envelope_id,
                        "reason": violation,
                        "run_id": run_id,
                    },
                )
                return outcome
        risk = classify_risk(tool)
        key = call_key(call)
        if tool.mutates:
            unresolved = self._uncertain_keys | self._durable_uncertain_keys(run_id)
            if key in unresolved:
                outcome = self._denied(
                    call,
                    ToolExecutionStatus.UNKNOWN,
                    (
                        "an earlier identical call ended with an unresolved side effect; "
                        "reconcile the target state before repeating it"
                    ),
                    run_id=run_id,
                )
                await self._record(outcome)
                return outcome
        if self.budget_check is not None and not self.budget_check(1):
            outcome = self._denied(
                call,
                ToolExecutionStatus.DENIED,
                "tool call budget is exhausted",
                run_id=run_id,
            )
            await self._record(outcome)
            return outcome
        if risk.requires_approval:
            if self.approver is None:
                outcome = self._denied(
                    call,
                    ToolExecutionStatus.DENIED,
                    ("this action requires owner approval, but no approval provider is configured"),
                    run_id=run_id,
                )
                await self._record(outcome)
                await self._emit(
                    "approval.unavailable",
                    {"tool": tool.name, "fingerprint": action_fingerprint(call), "run_id": run_id},
                )
                return outcome
            await self._emit(
                "approval.requested",
                {"tool": tool.name, "fingerprint": action_fingerprint(call), "run_id": run_id},
            )
            try:
                decision = await self._ask_approval(call, tool)
            except Exception:
                log.warning("approval provider failed; denying action", exc_info=True)
                outcome = self._denied(
                    call,
                    ToolExecutionStatus.DENIED,
                    "the approval service failed; the action was not executed",
                    run_id=run_id,
                )
                await self._record(outcome)
                await self._emit(
                    "approval.unavailable",
                    {
                        "tool": tool.name,
                        "fingerprint": action_fingerprint(call),
                        "run_id": run_id,
                        "reason": "approval_service_error",
                    },
                )
                return outcome
            if not decision:
                outcome = self._denied(
                    call, ToolExecutionStatus.DENIED, "the user denied this action", run_id=run_id
                )
                await self._record(outcome)
                await self._emit(
                    "approval.denied",
                    {"tool": tool.name, "fingerprint": action_fingerprint(call), "run_id": run_id},
                )
                return outcome
            await self._emit(
                "approval.granted",
                {"tool": tool.name, "fingerprint": action_fingerprint(call), "run_id": run_id},
            )
        outcome = await self._dispatch(call, tool, risk, key, attempt, run_id)
        await self._record(outcome)
        return outcome

    async def _ask_approval(self, call: ToolCall, tool: Tool) -> bool:
        decision = self.approver(call, tool) if self.approver is not None else False
        if inspect.isawaitable(decision):
            return bool(await decision)
        return bool(decision)

    def _acquire_inflight(self, key: str) -> None:
        """Register interest in a call key so its lock survives concurrent users."""
        self._inflight_holders[key] = self._inflight_holders.get(key, 0) + 1

    def _release_inflight(self, key: str) -> None:
        if self._inflight_holders.get(key, 0) > 1:
            self._inflight_holders[key] -= 1
            return
        self._inflight_holders.pop(key, None)
        self._inflight.pop(key, None)

    def _acquire_path_keys(self, lock_keys: list[str]) -> None:
        for key in lock_keys:
            self._path_lock_holders[key] = self._path_lock_holders.get(key, 0) + 1

    def _release_path_keys(self, lock_keys: list[str]) -> None:
        for key in lock_keys:
            if self._path_lock_holders.get(key, 0) > 1:
                self._path_lock_holders[key] -= 1
                continue
            self._path_lock_holders.pop(key, None)
            self._path_locks.pop(key, None)

    async def _dispatch(
        self,
        call: ToolCall,
        tool: Tool,
        risk: RiskClassification,
        key: str,
        attempt: str,
        run_id: str | None,
    ) -> ToolExecutionOutcome:
        self._acquire_inflight(key)
        try:
            inflight = self._inflight.setdefault(key, asyncio.Lock())
            async with inflight:
                started = datetime.now(UTC)
                if tool.mutates or tool.requires_approval:
                    await self._record_started(
                        AttemptStarted(
                            attempt_id=attempt,
                            tool_name=tool.name,
                            call_key=key,
                            run_id=run_id,
                            risk_level=risk.risk_level,
                        )
                    )
                    await self._run_before_mutation(call, tool)
                dispatch_envelope = self.context.envelope
                await self._emit(
                    "tool.started",
                    {
                        "tool": tool.name,
                        "call_id": call.id,
                        "attempt_id": attempt,
                        "run_id": run_id,
                        "risk_level": risk.risk_level,
                        "envelope_id": (
                            dispatch_envelope.envelope_id if dispatch_envelope is not None else ""
                        ),
                    },
                )
                try:
                    output = await self._invoke_locked(call, tool)
                except asyncio.CancelledError:
                    uncertain = tool.mutates
                    detail = (
                        self._reconcile(call, tool, ToolExecutionStatus.CANCELLED)
                        if uncertain
                        else "cancelled before completion"
                    )
                    outcome = ToolExecutionOutcome(
                        call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.CANCELLED,
                        output=f"error: cancelled before finishing; {detail}"
                        if uncertain
                        else "error: cancelled before finishing",
                        is_error=True,
                        uncertain=uncertain,
                        call_key=key,
                        attempt_id=attempt,
                        run_id=run_id,
                        risk_level=risk.risk_level,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        detail=detail,
                    )
                    await self._emit(
                        "tool.finished",
                        self._finished_event(call, tool, attempt, run_id, outcome.status),
                    )
                    await self._record(outcome)
                    raise
                except TimeoutError:
                    uncertain = tool.mutates
                    detail = (
                        self._reconcile(call, tool, ToolExecutionStatus.TIMEOUT)
                        if uncertain
                        else f"timed out after {self.context.timeout:g}s"
                    )
                    outcome = ToolExecutionOutcome(
                        call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.TIMEOUT,
                        output=f"error: timed out after {self.context.timeout:g}s; {detail}"
                        if uncertain
                        else f"error: timed out after {self.context.timeout:g}s",
                        is_error=True,
                        uncertain=uncertain,
                        call_key=key,
                        attempt_id=attempt,
                        run_id=run_id,
                        risk_level=risk.risk_level,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        detail=detail,
                    )
                    await self._emit(
                        "tool.finished",
                        self._finished_event(call, tool, attempt, run_id, outcome.status),
                    )
                    return outcome
                except ToolError as exc:
                    outcome = ToolExecutionOutcome(
                        call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.FAILED,
                        output=f"error: {exc}",
                        is_error=True,
                        call_key=key,
                        attempt_id=attempt,
                        run_id=run_id,
                        risk_level=risk.risk_level,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        detail=redact_text(str(exc))[:500],
                    )
                    await self._emit(
                        "tool.finished",
                        self._finished_event(call, tool, attempt, run_id, outcome.status),
                    )
                    return outcome
                except Exception as exc:
                    outcome = ToolExecutionOutcome(
                        call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.FAILED,
                        output=f"error: tool crashed: {type(exc).__name__}: {exc}",
                        is_error=True,
                        call_key=key,
                        attempt_id=attempt,
                        run_id=run_id,
                        risk_level=risk.risk_level,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        detail=redact_text(f"{type(exc).__name__}: {exc}")[:500],
                    )
                    await self._emit(
                        "tool.finished",
                        self._finished_event(call, tool, attempt, run_id, outcome.status),
                    )
                    return outcome
                if isinstance(output, ToolOutput):
                    is_error = output.is_error
                    timed_out = output.status == "timeout"
                    text = output.text
                else:
                    is_error = output.startswith("error:")
                    timed_out = False
                    text = output
                status = (
                    ToolExecutionStatus.TIMEOUT
                    if timed_out
                    else ToolExecutionStatus.FAILED
                    if is_error
                    else ToolExecutionStatus.SUCCESS
                )
                uncertain = timed_out and tool.mutates
                detail = ""
                if timed_out:
                    detail = (
                        self._reconcile(call, tool, ToolExecutionStatus.TIMEOUT)
                        if tool.mutates
                        else f"timed out after {self.context.timeout:g}s"
                    )
                outcome = ToolExecutionOutcome(
                    call_id=call.id,
                    tool_name=call.name,
                    status=status,
                    output=truncate_text(text, self.context.max_output),
                    is_error=is_error or timed_out,
                    uncertain=uncertain,
                    call_key=key,
                    attempt_id=attempt,
                    run_id=run_id,
                    risk_level=risk.risk_level,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    detail=detail,
                )
                await self._emit(
                    "tool.finished",
                    {
                        "tool": tool.name,
                        "call_id": call.id,
                        "attempt_id": attempt,
                        "run_id": run_id,
                        "status": outcome.status.value,
                    },
                )
                return outcome
        finally:
            self._release_inflight(key)

    async def _invoke_locked(self, call: ToolCall, tool: Tool) -> str | ToolOutput:
        if tool.mutates and not tool.path_keys:
            async with self._global_mutation_lock:
                return await asyncio.wait_for(
                    tool.handler(dict(call.arguments), self.context), timeout=self.context.timeout
                )
        lock_keys = self._lock_keys(call, tool)
        self._acquire_path_keys(lock_keys)
        acquired: list[asyncio.Lock] = []
        try:
            locks = [self._path_locks.setdefault(item, asyncio.Lock()) for item in lock_keys]
            try:
                for lock in locks:
                    await lock.acquire()
                    acquired.append(lock)
                return await asyncio.wait_for(
                    tool.handler(dict(call.arguments), self.context),
                    timeout=self.context.timeout,
                )
            finally:
                for lock in reversed(acquired):
                    lock.release()
        finally:
            self._release_path_keys(lock_keys)
