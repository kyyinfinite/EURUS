from __future__ import annotations

import asyncio
from enum import StrEnum

from eurus.errors import (
    AllProvidersFailedError,
    AuthenticationError,
    InvalidRequestError,
    PolicyError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ToolError,
)
from eurus.models import ToolResult
from eurus.tools.base import Tool


class FailureKind(StrEnum):
    INVALID_ACTION = "invalid_action"
    MISSING_CAPABILITY = "missing_capability"
    PERMISSION_DENIED = "permission_denied"
    TOOL_FAILURE = "tool_failure"
    TIMEOUT = "timeout"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"
    NONPRODUCTIVE = "nonproductive"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    IRRECOVERABLE = "irrecoverable"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    PROVIDER = "provider"
    INVALID_OUTPUT = "invalid_output"
    WORKSPACE_VIOLATION = "workspace_violation"
    NETWORK_POLICY = "network_policy"
    VERIFICATION = "verification"
    CANCELLATION = "cancellation"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    CORRECT_ARGUMENTS = "correct_arguments"
    SWITCH_TOOL = "switch_tool"
    AWAIT_APPROVAL = "await_approval"
    WAIT_EXTERNAL = "wait_external"
    REPLAN = "replan"
    RETRY_MODIFIED = "retry_modified"
    STOP = "stop"
    SURFACE = "surface"


UNCERTAIN_MARKERS = (
    "unresolved side effect",
    "may or may not have been applied",
    "reconcile the target state",
    "cancelled before finishing",
)

DENIAL_MARKERS = (
    "no approval provider",
    "user denied",
    "requires owner approval",
    "not permitted for this run",
)

BUDGET_MARKERS = (
    "budget is exhausted",
    "budget exhausted",
    "exceeds",
)

REPEAT_MARKERS = (
    "already failed",
    "repeated too many times",
)

STRUCTURED_CODE_KINDS: dict[str, FailureKind] = {
    "unknown_tool": FailureKind.MISSING_CAPABILITY,
    "invalid_arguments": FailureKind.INVALID_ACTION,
    "invalid_json_arguments": FailureKind.INVALID_ACTION,
    "approval_denied": FailureKind.PERMISSION_DENIED,
    "approval_unavailable": FailureKind.PERMISSION_DENIED,
    "timeout": FailureKind.TIMEOUT,
    "cancelled": FailureKind.CANCELLATION,
    "uncertain": FailureKind.UNCERTAIN_SIDE_EFFECT,
    "workspace_violation": FailureKind.WORKSPACE_VIOLATION,
    "network_policy": FailureKind.NETWORK_POLICY,
    "budget_exhausted": FailureKind.RESOURCE_EXHAUSTED,
    "verification_failed": FailureKind.VERIFICATION,
    "rate_limit": FailureKind.RATE_LIMIT,
    "authentication": FailureKind.AUTHENTICATION,
    "provider": FailureKind.PROVIDER,
}

RETRYABLE_KINDS = frozenset(
    {
        FailureKind.TIMEOUT,
        FailureKind.RATE_LIMIT,
        FailureKind.PROVIDER,
        FailureKind.EXTERNAL_DEPENDENCY,
    }
)


def classify_failure(result: ToolResult, tool: Tool | None = None) -> FailureKind | None:
    if not result.is_error:
        return None
    code = getattr(result, "error_code", "")
    if code:
        kind = STRUCTURED_CODE_KINDS.get(code)
        if kind is not None:
            return kind
    output = result.output.lower()
    if "unknown tool" in output:
        return FailureKind.MISSING_CAPABILITY
    if "not valid json" in output or "invalid arguments" in output:
        return FailureKind.INVALID_ACTION
    if "outside the workspace" in output or "symbolic link" in output:
        return FailureKind.WORKSPACE_VIOLATION
    if any(marker in output for marker in UNCERTAIN_MARKERS):
        return FailureKind.UNCERTAIN_SIDE_EFFECT
    if any(marker in output for marker in DENIAL_MARKERS):
        return FailureKind.PERMISSION_DENIED
    if "timed out" in output:
        return FailureKind.TIMEOUT
    if any(marker in output for marker in BUDGET_MARKERS):
        return FailureKind.RESOURCE_EXHAUSTED
    if any(marker in output for marker in REPEAT_MARKERS):
        return FailureKind.NONPRODUCTIVE
    return FailureKind.TOOL_FAILURE


def classify_exception(exc: BaseException) -> FailureKind:
    if isinstance(exc, AuthenticationError):
        return FailureKind.AUTHENTICATION
    if isinstance(exc, RateLimitError):
        return FailureKind.RATE_LIMIT
    if isinstance(exc, ProviderUnavailableError):
        return FailureKind.PROVIDER
    if isinstance(exc, InvalidRequestError):
        return FailureKind.INVALID_ACTION
    if isinstance(exc, PolicyError):
        return FailureKind.NETWORK_POLICY
    if isinstance(exc, AllProvidersFailedError):
        return FailureKind.PROVIDER
    if isinstance(exc, ProviderError):
        return FailureKind.PROVIDER
    if isinstance(exc, asyncio.CancelledError):
        return FailureKind.CANCELLATION
    if isinstance(exc, TimeoutError):
        return FailureKind.TIMEOUT
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code:
        kind = STRUCTURED_CODE_KINDS.get(code)
        if kind is not None:
            return kind
    if isinstance(exc, ToolError):
        return FailureKind.TOOL_FAILURE
    return FailureKind.UNKNOWN


def is_retryable_kind(kind: FailureKind, exc: BaseException | None = None) -> bool:
    if kind is FailureKind.PROVIDER and exc is not None:
        return bool(getattr(exc, "retryable", False))
    return kind in RETRYABLE_KINDS


def is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, AllProvidersFailedError):
        return any(failure.retryable for _, failure in exc.failures)
    return is_retryable_kind(classify_exception(exc), exc)


RECOVERY_GUIDANCE: dict[FailureKind, str] = {
    FailureKind.INVALID_ACTION: (
        "Fix the arguments so they match the tool schema, then retry once with the "
        "corrected values."
    ),
    FailureKind.MISSING_CAPABILITY: (
        "That tool does not exist in this registry; choose one of the available tools "
        "or answer from the evidence already collected."
    ),
    FailureKind.PERMISSION_DENIED: (
        "The action was not authorized. Do not repeat it unchanged; ask the user to "
        "approve it, or pick a different approach that needs no approval."
    ),
    FailureKind.TIMEOUT: (
        "The call exceeded its time budget; narrow the request (smaller scope, shorter "
        "output) and retry at most once, without repeating the identical call."
    ),
    FailureKind.EXTERNAL_DEPENDENCY: (
        "An external operation was interrupted; check the resulting state before continuing."
    ),
    FailureKind.UNCERTAIN_SIDE_EFFECT: (
        "A mutating action ended with an unresolved side effect; verify the target "
        "state first and never blindly replay the same call."
    ),
    FailureKind.NONPRODUCTIVE: (
        "This exact action no longer helps; change the plan, the arguments, or the "
        "approach instead of repeating it."
    ),
    FailureKind.RESOURCE_EXHAUSTED: (
        "A resource budget is exhausted; finalize using the evidence collected so far."
    ),
    FailureKind.TOOL_FAILURE: (
        "Retry once with adjusted arguments or a different entry point; if it fails "
        "again, switch to another tool or gather more evidence."
    ),
    FailureKind.IRRECOVERABLE: (
        "The failure cannot be recovered automatically; report the blocker and stop."
    ),
    FailureKind.AUTHENTICATION: (
        "The provider rejected the credentials; do not retry; report the configuration problem."
    ),
    FailureKind.RATE_LIMIT: (
        "The provider rate-limited the request; wait before retrying and reduce request frequency."
    ),
    FailureKind.PROVIDER: (
        "The provider failed; retry only when the failure is marked retryable, with "
        "bounded attempts."
    ),
    FailureKind.INVALID_OUTPUT: (
        "The model produced invalid output; request the result again in the required format."
    ),
    FailureKind.WORKSPACE_VIOLATION: (
        "The action tried to leave the workspace; do not attempt to work around the boundary."
    ),
    FailureKind.NETWORK_POLICY: (
        "The network policy blocked this target; choose a public destination instead."
    ),
    FailureKind.VERIFICATION: (
        "Verification rejected the result; address the reported gaps before claiming completion."
    ),
    FailureKind.CANCELLATION: (
        "The operation was cancelled; reconcile any side effects before retrying."
    ),
    FailureKind.UNKNOWN: (
        "The failure is unclassified; do not assume it is safe to retry; gather more "
        "evidence first."
    ),
}

RECOVERY_ACTIONS: dict[FailureKind, RecoveryAction] = {
    FailureKind.INVALID_ACTION: RecoveryAction.CORRECT_ARGUMENTS,
    FailureKind.MISSING_CAPABILITY: RecoveryAction.SWITCH_TOOL,
    FailureKind.PERMISSION_DENIED: RecoveryAction.AWAIT_APPROVAL,
    FailureKind.TIMEOUT: RecoveryAction.RETRY_MODIFIED,
    FailureKind.EXTERNAL_DEPENDENCY: RecoveryAction.WAIT_EXTERNAL,
    FailureKind.UNCERTAIN_SIDE_EFFECT: RecoveryAction.SURFACE,
    FailureKind.NONPRODUCTIVE: RecoveryAction.REPLAN,
    FailureKind.RESOURCE_EXHAUSTED: RecoveryAction.STOP,
    FailureKind.TOOL_FAILURE: RecoveryAction.RETRY_MODIFIED,
    FailureKind.IRRECOVERABLE: RecoveryAction.STOP,
    FailureKind.AUTHENTICATION: RecoveryAction.STOP,
    FailureKind.RATE_LIMIT: RecoveryAction.WAIT_EXTERNAL,
    FailureKind.PROVIDER: RecoveryAction.RETRY_MODIFIED,
    FailureKind.INVALID_OUTPUT: RecoveryAction.RETRY_MODIFIED,
    FailureKind.WORKSPACE_VIOLATION: RecoveryAction.STOP,
    FailureKind.NETWORK_POLICY: RecoveryAction.STOP,
    FailureKind.VERIFICATION: RecoveryAction.REPLAN,
    FailureKind.CANCELLATION: RecoveryAction.SURFACE,
    FailureKind.UNKNOWN: RecoveryAction.SURFACE,
}


def tool_failure_action(kind: FailureKind, attempt: int) -> RecoveryAction:
    if kind is FailureKind.TOOL_FAILURE and attempt >= 1:
        return RecoveryAction.SWITCH_TOOL
    return RECOVERY_ACTIONS[kind]


def recovery_strategy(kind: FailureKind, attempt: int = 0) -> tuple[RecoveryAction, str]:
    action = tool_failure_action(kind, attempt)
    return action, RECOVERY_GUIDANCE[kind]


def recovery_note(kind: FailureKind, detail: str, attempt: int = 0) -> str:
    action, guidance = recovery_strategy(kind, attempt)
    trimmed = " ".join(detail.split())[:200]
    return f"[{kind.value} -> {action.value}] {trimmed}\nRecovery directive: {guidance}"
