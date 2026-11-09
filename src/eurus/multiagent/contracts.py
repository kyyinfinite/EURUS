from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MISSION_STATUSES = frozenset(
    {
        "created",
        "planning",
        "planned",
        "running",
        "verifying",
        "awaiting_approval",
        "completed",
        "completed_unverified",
        "failed",
        "cancelled",
        "blocked",
    }
)
TERMINAL_MISSION_STATUSES = frozenset(
    {"completed", "completed_unverified", "failed", "cancelled", "blocked"}
)

TASK_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "dispatched",
        "running",
        "review_pending",
        "succeeded",
        "blocked",
        "failed",
        "cancelled",
        "skipped",
        "revision_required",
        "reassigned",
    }
)
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "skipped"})

SEVERITIES = frozenset({"blocker", "critical", "major", "minor", "info"})
BLOCKING_SEVERITIES = frozenset({"blocker", "critical", "major"})
FINDING_CATEGORIES = frozenset(
    {
        "correctness",
        "security",
        "performance",
        "maintainability",
        "accessibility",
        "compatibility",
    }
)
FINDING_DISPOSITIONS = frozenset({"open", "accepted_risk", "fixed", "not_applicable"})

VERIFICATION_STATUSES = frozenset({"passed", "failed", "skipped", "not_available", "timed_out"})
PASSED_VERIFICATION_STATUSES = frozenset({"passed"})
FAILED_VERIFICATION_STATUSES = frozenset({"failed", "timed_out"})
UNAVAILABLE_VERIFICATION_STATUSES = frozenset({"skipped", "not_available"})

ARTIFACT_TYPES = frozenset(
    {
        "patch",
        "diff",
        "report",
        "design",
        "test_result",
        "review",
        "research",
        "document",
        "root_cause",
        "log",
        "data",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Mission:
    mission_id: str
    owner_id: str
    objective: str
    status: str = "created"
    root_run_id: str | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.mission_id.strip() or not self.owner_id.strip():
            raise ValueError("mission_id and owner_id are required")
        if not self.objective.strip():
            raise ValueError("mission objective must not be empty")
        if self.status not in MISSION_STATUSES:
            raise ValueError(f"unknown mission status: {self.status!r}")
        if self.version < 1:
            raise ValueError("mission version must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "owner_id": self.owner_id,
            "objective": self.objective,
            "status": self.status,
            "root_run_id": self.root_run_id,
            "plan": dict(self.plan),
            "acceptance_criteria": list(self.acceptance_criteria),
            "budget": dict(self.budget),
            "version": self.version,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "completed_at": _iso(self.completed_at) if self.completed_at else None,
        }


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    mission_id: str
    objective: str
    task_key: str
    title: str = ""
    parent_task_id: str | None = None
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    preferred_role: str | None = None
    input_contract: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: tuple[str, ...] = ()
    status: str = "pending"
    priority: int = 5
    attempt: int = 0
    assignment_id: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.mission_id.strip():
            raise ValueError("task_id and mission_id are required")
        if not self.task_key.strip():
            raise ValueError("task_key must not be empty")
        if not self.objective.strip():
            raise ValueError("task objective must not be empty")
        if self.status not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {self.status!r}")
        if not 1 <= self.priority <= 9:
            raise ValueError("task priority must be between 1 and 9")
        if self.task_id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("dependencies must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "parent_task_id": self.parent_task_id,
            "task_key": self.task_key,
            "title": self.title,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "required_capabilities": list(self.required_capabilities),
            "preferred_role": self.preferred_role,
            "input_contract": dict(self.input_contract),
            "output_contract": dict(self.output_contract),
            "acceptance_criteria": list(self.acceptance_criteria),
            "status": self.status,
            "priority": self.priority,
            "attempt": self.attempt,
            "assignment_id": self.assignment_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class AssignmentContract:
    assignment_id: str
    mission_id: str
    task_id: str
    objective: str
    role: str
    owner_id: str
    allowed_tools: tuple[str, ...] = ()
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    output_contract: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: tuple[str, ...] = ()
    tool_policy_version: int = 1
    profile_version: int = 1
    budget: dict[str, Any] = field(default_factory=dict)
    workspace_policy: str = "isolated"

    def __post_init__(self) -> None:
        if not self.assignment_id.strip() or not self.task_id.strip():
            raise ValueError("assignment_id and task_id are required")
        if not self.objective.strip():
            raise ValueError("contract objective must not be empty")
        for ref in self.input_artifact_refs:
            if not isinstance(ref, ArtifactRef):
                raise ValueError("input_artifact_refs must contain ArtifactRef instances")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "role": self.role,
            "owner_id": self.owner_id,
            "allowed_tools": list(self.allowed_tools),
            "input_artifact_refs": [ref.to_dict() for ref in self.input_artifact_refs],
            "output_contract": dict(self.output_contract),
            "acceptance_criteria": list(self.acceptance_criteria),
            "tool_policy_version": self.tool_policy_version,
            "profile_version": self.profile_version,
            "budget": dict(self.budget),
            "workspace_policy": self.workspace_policy,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str
    version: int = 1
    content_hash: str = ""
    media_type: str = "text/plain"
    owner_id: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("artifact_id is required")
        if not self.artifact_type.strip():
            raise ValueError("artifact_type is required")
        if self.version < 1:
            raise ValueError("artifact version must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "owner_id": self.owner_id,
        }


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    assignment_id: str
    task_id: str
    status: str
    summary: str
    artifact_refs: tuple[ArtifactRef, ...] = ()
    verification: VerificationEvidence | None = None
    blockers: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.assignment_id.strip() or not self.task_id.strip():
            raise ValueError("assignment_id and task_id are required")
        if self.status not in {"succeeded", "failed", "blocked"}:
            raise ValueError(f"unknown specialist result status: {self.status!r}")
        if not self.summary.strip():
            raise ValueError("specialist result summary must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "verification": self.verification.to_dict() if self.verification else None,
            "blockers": list(self.blockers),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    finding_id: str
    severity: str
    category: str
    location: str = ""
    evidence: str = ""
    suggested_remediation: str = ""
    disposition: str = "open"
    revision_task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.finding_id.strip():
            raise ValueError("finding_id is required")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown finding severity: {self.severity!r}")
        if self.category not in FINDING_CATEGORIES:
            raise ValueError(f"unknown finding category: {self.category!r}")
        if self.disposition not in FINDING_DISPOSITIONS:
            raise ValueError(f"unknown finding disposition: {self.disposition!r}")

    @property
    def is_blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES and self.disposition == "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "category": self.category,
            "location": self.location,
            "evidence": self.evidence,
            "suggested_remediation": self.suggested_remediation,
            "disposition": self.disposition,
            "revision_task_id": self.revision_task_id,
        }


@dataclass(frozen=True, slots=True)
class ReviewResult:
    review_id: str
    mission_id: str
    task_id: str
    reviewer_assignment_id: str | None
    status: str
    findings: tuple[ReviewFinding, ...] = ()
    summary: str = ""
    cycle: int = 1
    created_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.review_id.strip() or not self.mission_id.strip():
            raise ValueError("review_id and mission_id are required")
        if self.status not in {"open", "approved", "changes_requested", "rejected"}:
            raise ValueError(f"unknown review status: {self.status!r}")
        for finding in self.findings:
            if not isinstance(finding, ReviewFinding):
                raise ValueError("findings must contain ReviewFinding instances")

    @property
    def has_blocking_findings(self) -> bool:
        return any(finding.is_blocking for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "reviewer_assignment_id": self.reviewer_assignment_id,
            "status": self.status,
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": self.summary,
            "cycle": self.cycle,
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    check_name: str
    status: str
    category: str = "test"
    command_argv: tuple[str, ...] = ()
    working_directory: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    mission_id: str = ""
    task_id: str = ""
    artifact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.check_name.strip():
            raise ValueError("check_name is required")
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"unknown verification status: {self.status!r}")
        if self.category not in {"test", "lint", "typecheck", "build", "security", "custom"}:
            raise ValueError(f"unknown verification category: {self.category!r}")

    @property
    def passed(self) -> bool:
        return self.status in PASSED_VERIFICATION_STATUSES

    @property
    def failed(self) -> bool:
        return self.status in FAILED_VERIFICATION_STATUSES

    @property
    def unavailable(self) -> bool:
        return self.status in UNAVAILABLE_VERIFICATION_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "status": self.status,
            "category": self.category,
            "command_argv": list(self.command_argv),
            "working_directory": self.working_directory,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail[-2000:],
            "stderr_tail": self.stderr_tail[-2000:],
            "environment": dict(self.environment),
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceDecision:
    mission_id: str
    decision: str
    reasons: tuple[str, ...] = ()
    blocking_finding_ids: tuple[str, ...] = ()
    failed_check_names: tuple[str, ...] = ()
    unavailable_check_names: tuple[str, ...] = ()
    evaluated_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.decision not in {"completed", "completed_unverified", "blocked"}:
            raise ValueError(f"unknown acceptance decision: {self.decision!r}")

    @property
    def verified(self) -> bool:
        return self.decision == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "blocking_finding_ids": list(self.blocking_finding_ids),
            "failed_check_names": list(self.failed_check_names),
            "unavailable_check_names": list(self.unavailable_check_names),
            "evaluated_at": _iso(self.evaluated_at),
        }
