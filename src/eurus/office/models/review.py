from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eurus.office.errors import OfficeValidationError
from eurus.office.models.task import (
    AgentRole,
    DefectSeverity,
    OfficeFormat,
    QualityScores,
    ResolutionStatus,
    format_from_label,
    utc_now,
)

VISUAL_MODES = ("deterministic", "model", "unavailable")
QUALITY_STATUSES = ("passed", "passed_with_warnings", "rejected")
DEFECT_LAYERS = ("structural", "visual", "content")


@dataclass(frozen=True, slots=True)
class DefectReport:
    defect_id: str
    severity: DefectSeverity
    artifact_location: str
    description: str
    evidence: str = ""
    responsible_agent: AgentRole = AgentRole.PRODUCER
    recommended_fix: str = ""
    resolution_status: ResolutionStatus = ResolutionStatus.OPEN
    layer: str = "structural"

    def __post_init__(self) -> None:
        if self.layer not in DEFECT_LAYERS:
            raise OfficeValidationError("defect layer must be structural, visual, or content")

    def with_status(self, status: ResolutionStatus) -> DefectReport:
        return DefectReport(
            defect_id=self.defect_id,
            severity=self.severity,
            artifact_location=self.artifact_location,
            description=self.description,
            evidence=self.evidence,
            responsible_agent=self.responsible_agent,
            recommended_fix=self.recommended_fix,
            resolution_status=status,
            layer=self.layer,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "defect_id": self.defect_id,
            "severity": self.severity.value,
            "artifact_location": self.artifact_location,
            "description": self.description,
            "evidence": self.evidence[:1000],
            "responsible_agent": self.responsible_agent.value,
            "recommended_fix": self.recommended_fix[:1000],
            "resolution_status": self.resolution_status.value,
            "layer": self.layer,
        }

    @classmethod
    def from_payload(cls, index: int, payload: dict[str, Any]) -> DefectReport:
        description = str(payload.get("description", "")).strip()
        if not description:
            raise OfficeValidationError("defect description must not be empty")
        severity_raw = str(payload.get("severity", "major")).strip().lower()
        severity = (
            DefectSeverity(severity_raw)
            if severity_raw in DefectSeverity.__members__
            else DefectSeverity.MAJOR
        )
        agent_raw = str(payload.get("responsible_agent", "producer")).strip().lower()
        role = AgentRole(agent_raw) if agent_raw in AgentRole.__members__ else AgentRole.PRODUCER
        return cls(
            defect_id=f"defect-{index:03d}",
            severity=severity,
            artifact_location=str(payload.get("artifact_location", ""))[:500],
            description=description[:2000],
            evidence=str(payload.get("evidence", ""))[:1000],
            responsible_agent=role,
            recommended_fix=str(payload.get("recommended_fix", ""))[:1000],
            layer=str(payload.get("layer", "content"))
            if str(payload.get("layer", "content")) in DEFECT_LAYERS
            else "content",
        )


@dataclass(frozen=True, slots=True)
class QAReport:
    job_id: str
    task_id: str
    structural_score: float | None = None
    visual_score: float | None = None
    content_score: float | None = None
    defects: tuple[DefectReport, ...] = ()
    visual_mode: str = "unavailable"
    vision_model_used: bool = False
    passed: bool = False
    notes: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.visual_mode not in VISUAL_MODES:
            raise OfficeValidationError("visual_mode must be deterministic, model, or unavailable")

    @property
    def unresolved_critical(self) -> bool:
        return any(
            defect.severity is DefectSeverity.CRITICAL
            and defect.resolution_status not in {ResolutionStatus.FIXED, ResolutionStatus.VERIFIED}
            for defect in self.defects
        )

    def open_defects(self) -> tuple[DefectReport, ...]:
        return tuple(
            defect
            for defect in self.defects
            if defect.resolution_status not in {ResolutionStatus.FIXED, ResolutionStatus.VERIFIED}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "structural_score": self.structural_score,
            "visual_score": self.visual_score,
            "content_score": self.content_score,
            "defects": [defect.to_dict() for defect in self.defects],
            "visual_mode": self.visual_mode,
            "vision_model_used": self.vision_model_used,
            "passed": self.passed,
            "notes": list(self.notes),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    job_id: str
    task_id: str
    accepted: bool
    content_score: float = 0.0
    reasons: tuple[str, ...] = ()
    missing_content: tuple[str, ...] = ()
    client_summary: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "accepted": self.accepted,
            "content_score": self.content_score,
            "reasons": list(self.reasons),
            "missing_content": list(self.missing_content),
            "client_summary": self.client_summary[:2000],
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class FinalArtifactPackage:
    job_id: str
    status: str
    format: OfficeFormat
    quality_status: str
    artifact_path: str = ""
    preview_path: str = ""
    qa_report_path: str = ""
    revision_count: int = 0
    summary: str = ""
    warnings: tuple[str, ...] = ()
    scores: QualityScores = field(default_factory=QualityScores)

    def __post_init__(self) -> None:
        if not isinstance(self.format, OfficeFormat):
            object.__setattr__(self, "format", format_from_label(self.format))
        if self.quality_status not in QUALITY_STATUSES:
            raise OfficeValidationError(
                "quality_status must be passed, passed_with_warnings, or rejected"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "format": self.format.value,
            "quality_status": self.quality_status,
            "artifact_path": self.artifact_path,
            "preview_path": self.preview_path,
            "qa_report_path": self.qa_report_path,
            "revision_count": self.revision_count,
            "summary": self.summary[:4000],
            "warnings": list(self.warnings),
            "scores": self.scores.to_dict(),
        }
