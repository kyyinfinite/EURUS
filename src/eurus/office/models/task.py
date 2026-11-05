from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from eurus.office.errors import OfficeValidationError

MAX_OBJECTIVE_CHARS = 12_000
MAX_CONSTRAINTS = 24
MAX_SOURCE_PATHS = 20
JOB_ID_PATTERN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class OfficeFormat(StrEnum):
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"

    @property
    def extension(self) -> str:
        return f".{self.value}"

    @property
    def label(self) -> str:
        return {
            OfficeFormat.DOCX: "word",
            OfficeFormat.XLSX: "excel",
            OfficeFormat.PPTX: "powerpoint",
        }[self]


FORMAT_ALIASES: dict[str, OfficeFormat] = {
    "word": OfficeFormat.DOCX,
    "docx": OfficeFormat.DOCX,
    "excel": OfficeFormat.XLSX,
    "xlsx": OfficeFormat.XLSX,
    "powerpoint": OfficeFormat.PPTX,
    "pptx": OfficeFormat.PPTX,
}


def format_from_label(label: str) -> OfficeFormat:
    normalized = str(label).strip().lower()
    fmt = FORMAT_ALIASES.get(normalized)
    if fmt is None:
        raise OfficeValidationError(
            f"unknown office format {label!r}; use word/docx, excel/xlsx, or powerpoint/pptx"
        )
    return fmt


class AgentRole(StrEnum):
    CLIENT = "client"
    RESEARCHER = "researcher"
    DESIGNER = "designer"
    PRODUCER = "producer"
    CRITIC = "critic"
    VISUAL_QA = "visual_qa"


class JobStage(StrEnum):
    RECEIVED = "received"
    BRIEF_ANALYSIS = "brief_analysis"
    TEAM_PLANNING = "team_planning"
    RESEARCH = "research"
    DESIGN = "design"
    PRODUCTION = "production"
    STRUCTURAL_QA = "structural_qa"
    RENDERING = "rendering"
    VISUAL_QA = "visual_qa"
    CRITIC_REVIEW = "critic_review"
    CLIENT_ACCEPTANCE = "client_acceptance"
    REVISION = "revision"
    FINALIZATION = "finalization"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REQUIRES_USER_INPUT = "requires_user_input"
    QUALITY_LIMIT_REACHED = "quality_limit_reached"


class StageStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class DefectSeverity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class ResolutionStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    FIXED = "fixed"
    VERIFIED = "verified"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_job_id(job_id: str) -> str:
    if not job_id or len(job_id) > 64 or any(char not in JOB_ID_PATTERN_CHARS for char in job_id):
        raise OfficeValidationError("job_id must be 1-64 characters of letters, digits, _ or -")
    return job_id


@dataclass(frozen=True, slots=True)
class OfficeBrief:
    objective: str
    format: OfficeFormat
    audience: str = ""
    tone: str = ""
    language: str = "en"
    style_hint: str = ""
    constraints: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    output_path: str = ""
    include_toc: bool = True
    external_research: bool = False
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise OfficeValidationError("objective must not be empty")
        if len(self.objective) > MAX_OBJECTIVE_CHARS:
            raise OfficeValidationError(f"objective exceeds {MAX_OBJECTIVE_CHARS} characters")
        if len(self.constraints) > MAX_CONSTRAINTS:
            raise OfficeValidationError(f"constraints exceed {MAX_CONSTRAINTS} entries")
        if len(self.source_paths) > MAX_SOURCE_PATHS:
            raise OfficeValidationError(f"source_paths exceed {MAX_SOURCE_PATHS} entries")
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise OfficeValidationError("deadline_seconds must be positive")
        if not isinstance(self.format, OfficeFormat):
            normalized = str(self.format).strip().lower()
            aliased = FORMAT_ALIASES.get(normalized)
            if aliased is not None:
                object.__setattr__(self, "format", aliased)
                return
            try:
                object.__setattr__(self, "format", OfficeFormat(normalized))
            except ValueError as exc:
                raise OfficeValidationError(f"unknown office format: {self.format!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "format": self.format.value,
            "audience": self.audience,
            "tone": self.tone,
            "language": self.language,
            "style_hint": self.style_hint,
            "constraints": list(self.constraints),
            "source_paths": list(self.source_paths),
            "output_path": self.output_path,
            "include_toc": self.include_toc,
            "external_research": self.external_research,
            "deadline_seconds": self.deadline_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OfficeBrief:
        return cls(
            objective=str(data["objective"]),
            format=OfficeFormat(str(data["format"])),
            audience=str(data.get("audience", "")),
            tone=str(data.get("tone", "")),
            language=str(data.get("language", "en")),
            style_hint=str(data.get("style_hint", "")),
            constraints=tuple(str(item) for item in data.get("constraints", ())),
            source_paths=tuple(str(item) for item in data.get("source_paths", ())),
            output_path=str(data.get("output_path", "")),
            include_toc=bool(data.get("include_toc", True)),
            external_research=bool(data.get("external_research", False)),
            deadline_seconds=(
                float(data["deadline_seconds"])
                if data.get("deadline_seconds") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class OfficeTask:
    task_id: str
    job_id: str
    brief: OfficeBrief
    created_at: str = field(default_factory=utc_now)
    requested_by: str = "main_agent"

    def __post_init__(self) -> None:
        validate_job_id(self.job_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "requested_by": self.requested_by,
            "brief": self.brief.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OfficeTask:
        return cls(
            task_id=str(data["task_id"]),
            job_id=validate_job_id(str(data["job_id"])),
            brief=OfficeBrief.from_dict(dict(data["brief"])),
            created_at=str(data.get("created_at", utc_now())),
            requested_by=str(data.get("requested_by", "main_agent")),
        )


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    role: AgentRole
    objective: str
    instructions: tuple[str, ...] = ()
    budget_model_calls: int = 4
    timeout_seconds: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "objective": self.objective,
            "instructions": list(self.instructions),
            "budget_model_calls": self.budget_model_calls,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class AgentReport:
    role: AgentRole
    job_id: str
    task_id: str
    status: str
    summary: str
    model_calls: int = 0
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    artifacts: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "job_id": self.job_id,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary[:2000],
            "model_calls": self.model_calls,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifacts": list(self.artifacts),
            "error": self.error[:2000],
        }


@dataclass(frozen=True, slots=True)
class StageRecord:
    stage: JobStage
    status: StageStatus
    detail: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "detail": self.detail[:2000],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class QualityScores:
    content: float | None = None
    visual: float | None = None
    structural: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "visual": self.visual, "structural": self.structural}


@dataclass(frozen=True, slots=True)
class ResearchFact:
    statement: str
    kind: str = "verified"
    source: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"verified", "assumption", "uncertain"}:
            raise OfficeValidationError("fact kind must be verified, assumption, or uncertain")


@dataclass(frozen=True, slots=True)
class ResearchPackage:
    objective: str
    facts: tuple[ResearchFact, ...] = ()
    sources: tuple[str, ...] = ()
    outline: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "facts": [
                {"statement": fact.statement, "kind": fact.kind, "source": fact.source}
                for fact in self.facts
            ],
            "sources": list(self.sources),
            "outline": list(self.outline),
            "gaps": list(self.gaps),
        }

    @classmethod
    def from_payload(cls, objective: str, payload: dict[str, Any]) -> ResearchPackage:
        facts: list[ResearchFact] = []
        for item in payload.get("facts", []):
            if isinstance(item, dict) and str(item.get("statement", "")).strip():
                facts.append(
                    ResearchFact(
                        statement=str(item["statement"])[:2000],
                        kind=str(item.get("kind", "verified")),
                        source=str(item.get("source", ""))[:500],
                    )
                )
        return cls(
            objective=objective,
            facts=tuple(facts[:120]),
            sources=tuple(
                str(item)[:500] for item in payload.get("sources", []) if str(item).strip()
            ),
            outline=tuple(
                str(item)[:500] for item in payload.get("outline", []) if str(item).strip()
            ),
            gaps=tuple(str(item)[:500] for item in payload.get("gaps", []) if str(item).strip()),
        )


@dataclass(frozen=True, slots=True)
class DesignSpecification:
    direction: str = "editorial"
    document_style: str = "business"
    guidance: tuple[str, ...] = ()
    palette_notes: str = ""
    typography_notes: str = ""
    layout_plan: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "document_style": self.document_style,
            "guidance": list(self.guidance),
            "palette_notes": self.palette_notes,
            "typography_notes": self.typography_notes,
            "layout_plan": list(self.layout_plan),
        }


@dataclass(frozen=True, slots=True)
class OfficeJobState:
    job_id: str
    task_id: str
    brief: OfficeBrief
    stage: JobStage = JobStage.RECEIVED
    stage_history: tuple[StageRecord, ...] = ()
    revision_count: int = 0
    quality: QualityScores = field(default_factory=QualityScores)
    warnings: tuple[str, ...] = ()
    artifact_path: str = ""
    preview_path: str = ""
    qa_report_path: str = ""
    updated_at: str = field(default_factory=utc_now)

    def with_stage(self, stage: JobStage, detail: str = "") -> OfficeJobState:
        record = StageRecord(stage=stage, status=StageStatus.RUNNING, detail=detail)
        return replace(
            self,
            stage=stage,
            stage_history=(*self.stage_history, record),
            updated_at=utc_now(),
        )

    def with_stage_finished(self, status: StageStatus, detail: str = "") -> OfficeJobState:
        if not self.stage_history:
            raise OfficeValidationError("cannot finish a stage before entering one")
        latest = self.stage_history[-1]
        closed = replace(latest, status=status, detail=detail, finished_at=utc_now())
        return replace(self, stage_history=(*self.stage_history[:-1], closed), updated_at=utc_now())

    def with_revision(self) -> OfficeJobState:
        return replace(self, revision_count=self.revision_count + 1, updated_at=utc_now())

    def with_quality(self, quality: QualityScores) -> OfficeJobState:
        return replace(self, quality=quality, updated_at=utc_now())

    def with_artifacts(
        self,
        artifact_path: str = "",
        preview_path: str = "",
        qa_report_path: str = "",
    ) -> OfficeJobState:
        return replace(
            self,
            artifact_path=artifact_path or self.artifact_path,
            preview_path=preview_path or self.preview_path,
            qa_report_path=qa_report_path or self.qa_report_path,
            updated_at=utc_now(),
        )

    def with_warnings(self, warnings: tuple[str, ...]) -> OfficeJobState:
        merged = dict.fromkeys((*self.warnings, *warnings))
        return replace(self, warnings=tuple(merged), updated_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "brief": self.brief.to_dict(),
            "stage": self.stage.value,
            "stage_history": [record.to_dict() for record in self.stage_history],
            "revision_count": self.revision_count,
            "quality": self.quality.to_dict(),
            "warnings": list(self.warnings),
            "artifact_path": self.artifact_path,
            "preview_path": self.preview_path,
            "qa_report_path": self.qa_report_path,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OfficeJobState:
        quality_raw = data.get("quality", {})
        return cls(
            job_id=validate_job_id(str(data["job_id"])),
            task_id=str(data["task_id"]),
            brief=OfficeBrief.from_dict(dict(data["brief"])),
            stage=JobStage(str(data.get("stage", "received"))),
            stage_history=tuple(
                StageRecord(
                    stage=JobStage(str(record["stage"])),
                    status=StageStatus(str(record["status"])),
                    detail=str(record.get("detail", "")),
                    started_at=str(record.get("started_at", "")),
                    finished_at=str(record.get("finished_at", "")),
                )
                for record in data.get("stage_history", [])
            ),
            revision_count=int(data.get("revision_count", 0)),
            quality=QualityScores(
                content=(
                    float(quality_raw["content"])
                    if quality_raw.get("content") is not None
                    else None
                ),
                visual=(
                    float(quality_raw["visual"]) if quality_raw.get("visual") is not None else None
                ),
                structural=(
                    float(quality_raw["structural"])
                    if quality_raw.get("structural") is not None
                    else None
                ),
            ),
            warnings=tuple(str(item) for item in data.get("warnings", ())),
            artifact_path=str(data.get("artifact_path", "")),
            preview_path=str(data.get("preview_path", "")),
            qa_report_path=str(data.get("qa_report_path", "")),
            updated_at=str(data.get("updated_at", utc_now())),
        )
