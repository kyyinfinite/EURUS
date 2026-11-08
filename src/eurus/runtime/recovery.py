from __future__ import annotations

from enum import StrEnum
from typing import Any

from eurus.runtime.models import TERMINAL_STATUSES, Run, RunStatus


class RecoveryAction(StrEnum):
    START_FRESH = "start_fresh"
    RESUME_CHECKPOINT = "resume_checkpoint"
    RECONCILE_UNKNOWN_OUTCOME = "reconcile_unknown_outcome"
    TERMINAL = "terminal"
    MANUAL_REVIEW = "manual_review"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    INTERRUPTED = "interrupted"
    UNCERTAIN = "uncertain"


TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily",
    "rate limit",
    "ratelimit",
    "429",
    "502",
    "503",
    "504",
    "unavailable",
    "connection reset",
    "eof occurred",
    "overloaded",
)

POLICY_MARKERS = (
    "approval",
    "denied",
    "permission",
    "policy",
    "blocked",
    "unauthorized tool",
    "reconciliation",
)

PERMANENT_MARKERS = (
    "authentication",
    "invalid api key",
    "401",
    "403",
    "not found",
    "schema",
    "configuration",
    "unsupported",
    "malformed",
    "engine_error:ValueError",
)

INTERRUPTED_STOP_REASONS = frozenset(
    {
        "interrupted",
        "cancelled",
        "cancelled_before_start",
        "operator_cancelled",
    }
)

POLICY_STOP_REASONS = frozenset(
    {
        "blocked",
        "approval_denied",
        "approval_unavailable",
    }
)

UNCERTAIN_STOP_REASONS = frozenset(
    {
        "uncertain",
        "unknown_outcome",
    }
)


def classify_failure_kind(
    *,
    stop_reason: str | None = None,
    error: str | None = None,
) -> FailureKind:
    reason = (stop_reason or "").strip()
    lowered = reason.lower()
    if lowered in INTERRUPTED_STOP_REASONS:
        return FailureKind.INTERRUPTED
    if lowered in POLICY_STOP_REASONS:
        return FailureKind.POLICY
    if lowered in UNCERTAIN_STOP_REASONS:
        return FailureKind.UNCERTAIN
    text = " ".join(part for part in (reason, error or "") if part).lower()
    if not text:
        return FailureKind.PERMANENT
    if any(marker in text for marker in POLICY_MARKERS):
        return FailureKind.POLICY
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return FailureKind.TRANSIENT
    if any(marker in text for marker in PERMANENT_MARKERS):
        return FailureKind.PERMANENT
    return FailureKind.PERMANENT


def is_retryable_failure(kind: FailureKind) -> bool:
    return kind is FailureKind.TRANSIENT


def classify_recovery(run: Run, checkpoint: dict[str, Any] | None) -> RecoveryAction:
    if run.status in TERMINAL_STATUSES:
        return RecoveryAction.TERMINAL
    if run.status == RunStatus.QUEUED:
        return RecoveryAction.START_FRESH
    if checkpoint is None:
        return RecoveryAction.MANUAL_REVIEW
    state = checkpoint.get("state", {})
    if not isinstance(state, dict):
        return RecoveryAction.MANUAL_REVIEW
    if state.get("in_flight_side_effect") is True:
        return RecoveryAction.RECONCILE_UNKNOWN_OUTCOME
    side_effect_status = state.get("side_effect_status")
    if isinstance(side_effect_status, str) and side_effect_status in {
        "started",
        "unknown",
        "pending",
    }:
        return RecoveryAction.RECONCILE_UNKNOWN_OUTCOME
    if state.get("tool_execution") == "in_flight":
        return RecoveryAction.RECONCILE_UNKNOWN_OUTCOME
    if run.status in {
        RunStatus.RUNNING,
        RunStatus.INTERRUPTED,
        RunStatus.RETRY_WAIT,
        RunStatus.WAITING_INPUT,
    }:
        return RecoveryAction.RESUME_CHECKPOINT
    return RecoveryAction.MANUAL_REVIEW
