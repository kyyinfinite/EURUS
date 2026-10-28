from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ToolExecutionStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    UNKNOWN = "unknown"


MUTATION_RISK_STATUSES = frozenset(
    {
        ToolExecutionStatus.TIMEOUT,
        ToolExecutionStatus.CANCELLED,
        ToolExecutionStatus.UNKNOWN,
    }
)


class VerificationStatus(StrEnum):
    NOT_RUN = "not_run"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class LoopDecision(StrEnum):
    CONTINUE = "continue"
    EXECUTE_TOOLS = "execute_tools"
    DELEGATE = "delegate"
    REPLAN = "replan"
    AWAIT_APPROVAL = "await_approval"
    WAIT_EXTERNAL = "wait_external"
    RECOVER = "recover"
    SUSPEND = "suspend"
    COMPLETED_UNVERIFIED = "completed_unverified"
    VERIFIED_SUCCESS = "verified_success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


STOP_REASON_DECISIONS: dict[str, LoopDecision] = {
    "completed": LoopDecision.VERIFIED_SUCCESS,
    "completed_unverified": LoopDecision.COMPLETED_UNVERIFIED,
    "verification_failed": LoopDecision.FAILED,
    "interrupted": LoopDecision.CANCELLED,
    "cancelled": LoopDecision.CANCELLED,
    "timeout": LoopDecision.FAILED,
    "deadline_exceeded": LoopDecision.BUDGET_EXHAUSTED,
    "max_steps": LoopDecision.BUDGET_EXHAUSTED,
    "iteration_budget": LoopDecision.BUDGET_EXHAUSTED,
    "tool_budget": LoopDecision.BUDGET_EXHAUSTED,
    "model_call_budget": LoopDecision.BUDGET_EXHAUSTED,
    "budget_exhausted": LoopDecision.BUDGET_EXHAUSTED,
    "truncated": LoopDecision.FAILED,
    "filtered": LoopDecision.FAILED,
    "max_model_calls": LoopDecision.BUDGET_EXHAUSTED,
    "no_progress": LoopDecision.FAILED,
    "suspended": LoopDecision.SUSPEND,
    "blocked": LoopDecision.WAIT_EXTERNAL,
}


def decision_for_stop_reason(stop_reason: str) -> LoopDecision:
    return STOP_REASON_DECISIONS.get(stop_reason, LoopDecision.CONTINUE)


def utc_timestamp() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    output: str = ""
    is_error: bool = False
    uncertain: bool = False
    call_key: str = ""
    attempt_id: str = ""
    run_id: str | None = None
    risk_level: str = "low"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status is ToolExecutionStatus.UNKNOWN and not self.uncertain:
            raise ValueError("unknown tool outcomes must be marked uncertain")
        if not self.uncertain and self.status in MUTATION_RISK_STATUSES and self.detail == "":
            raise ValueError(f"{self.status.value} outcomes must carry reconciliation detail")

    @property
    def side_effect_unresolved(self) -> bool:
        return self.uncertain or self.status in MUTATION_RISK_STATUSES

    def to_payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "is_error": self.is_error,
            "uncertain": self.uncertain,
            "call_key": self.call_key,
            "attempt_id": self.attempt_id,
            "risk_level": self.risk_level,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail[:500],
        }


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    status: VerificationStatus
    summary: str = ""
    evidence: tuple[str, ...] = ()
    checked_at: datetime = field(default_factory=utc_timestamp)

    def __post_init__(self) -> None:
        if self.status is VerificationStatus.VERIFIED and not self.evidence:
            raise ValueError("verified outcomes require recorded evidence")

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary[:500],
            "evidence": [item[:200] for item in self.evidence],
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RunOutcome:
    decision: LoopDecision
    stop_reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.stop_reason.strip():
            raise ValueError("stop_reason must be non-empty")


TERMINAL_DECISIONS = frozenset(
    {
        LoopDecision.COMPLETED_UNVERIFIED,
        LoopDecision.VERIFIED_SUCCESS,
        LoopDecision.FAILED,
        LoopDecision.CANCELLED,
        LoopDecision.BUDGET_EXHAUSTED,
        LoopDecision.SUSPEND,
    }
)


def is_terminal_decision(decision: LoopDecision) -> bool:
    return decision in TERMINAL_DECISIONS
