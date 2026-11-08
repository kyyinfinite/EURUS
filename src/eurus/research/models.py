from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from eurus.research.urlutils import canonical_url, registrable_domain, url_domain

MAX_QUERY_TEXT = 500


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


class ResearchMode(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class EvidenceType(StrEnum):
    DIRECT = "direct"
    PARTIAL = "partial"
    CONTEXT = "context"
    CONTRADICTORY = "contradictory"
    INFERENCE = "inference"
    UNSUPPORTED = "unsupported"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class QaStatus(StrEnum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    REQUIRES_MORE_RESEARCH = "REQUIRES_MORE_RESEARCH"


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    provider: str
    rank: int = 0
    published_at: str = ""
    source_type: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "provider": self.provider,
            "rank": self.rank,
            "published_at": self.published_at,
            "source_type": self.source_type,
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    url: str
    title: str
    provider: str
    status: str = "pending"
    content: str = ""
    content_chars: int = 0
    published_at: str = ""
    retrieved_at: str = ""
    content_hash: str = ""
    excerpt_hint: str = ""
    error: str = ""

    @property
    def canonical_url(self) -> str:
        try:
            return canonical_url(self.url)
        except ValueError:
            return self.url

    @property
    def domain(self) -> str:
        return url_domain(self.url)

    @property
    def site(self) -> str:
        return registrable_domain(self.url)

    def to_dict(self) -> dict[str, str | int]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "domain": self.domain,
            "site": self.site,
            "title": self.title,
            "provider": self.provider,
            "status": self.status,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "content_hash": self.content_hash,
            "content_chars": self.content_chars,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source_id: str
    claim: str
    excerpt: str
    evidence_type: EvidenceType = EvidenceType.CONTEXT
    confidence: float = 0.0
    location: str = ""
    verification: VerificationStatus = VerificationStatus.PENDING
    topic: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, str | float]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "claim": self.claim,
            "excerpt": self.excerpt,
            "evidence_type": self.evidence_type.value,
            "confidence": round(self.confidence, 3),
            "location": self.location,
            "verification": self.verification.value,
            "topic": self.topic,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CitationRecord:
    source_id: str
    url: str
    title: str
    verified: bool
    method: str = "excerpt_containment"
    reason: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "verified": self.verified,
            "method": self.method,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class ContradictionRecord:
    topic: str
    left_evidence_id: str
    right_evidence_id: str
    left_value: str
    right_value: str
    unit: str

    def to_dict(self) -> dict[str, str]:
        return {
            "topic": self.topic,
            "left_evidence_id": self.left_evidence_id,
            "right_evidence_id": self.right_evidence_id,
            "left_value": self.left_value,
            "right_value": self.right_value,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class PlanQuery:
    text: str
    purpose: str = "main"
    round_hint: int = 1

    def to_dict(self) -> dict[str, str | int]:
        return {"text": self.text, "purpose": self.purpose, "round_hint": self.round_hint}


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    objective: str
    mode: ResearchMode
    main_query: str
    queries: tuple[PlanQuery, ...] = ()
    subquestions: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    time_constraint: str = ""
    language: str = ""
    depth: str = "standard"
    planner: str = "deterministic"
    search_budget: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "mode": self.mode.value,
            "main_query": self.main_query,
            "queries": [item.to_dict() for item in self.queries],
            "subquestions": list(self.subquestions),
            "source_types": list(self.source_types),
            "time_constraint": self.time_constraint,
            "language": self.language,
            "depth": self.depth,
            "planner": self.planner,
            "search_budget": self.search_budget,
        }


@dataclass(frozen=True, slots=True)
class RoundRecord:
    round_number: int
    queries: tuple[str, ...] = ()
    hits: int = 0
    new_sources: int = 0
    fetches: int = 0
    fetch_failed: int = 0
    cache_hits: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, int | str | list[str]]:
        return {
            "round_number": self.round_number,
            "queries": list(self.queries),
            "hits": self.hits,
            "new_sources": self.new_sources,
            "fetches": self.fetches,
            "fetch_failed": self.fetch_failed,
            "cache_hits": self.cache_hits,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    total: int = 0
    answered: tuple[str, ...] = ()
    unanswered: tuple[str, ...] = ()
    ratio: float = 0.0

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.ratio >= 0.999

    def to_dict(self) -> dict[str, float | list[str] | int]:
        return {
            "total": self.total,
            "answered": list(self.answered),
            "unanswered": list(self.unanswered),
            "ratio": round(self.ratio, 3),
        }


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    job_id: str
    mode: ResearchMode
    objective: str
    status: JobStatus
    plan: ResearchPlan
    sources: tuple[SourceRecord, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    citations: tuple[CitationRecord, ...] = ()
    contradictions: tuple[ContradictionRecord, ...] = ()
    rounds: tuple[RoundRecord, ...] = ()
    coverage: CoverageReport = field(default_factory=CoverageReport)
    followups: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    qa_status: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "mode": self.mode.value,
            "objective": self.objective,
            "status": self.status.value,
            "plan": self.plan.to_dict(),
            "sources": [item.to_dict() for item in self.sources],
            "evidence": [item.to_dict() for item in self.evidence],
            "citations": [item.to_dict() for item in self.citations],
            "contradictions": [item.to_dict() for item in self.contradictions],
            "rounds": [item.to_dict() for item in self.rounds],
            "coverage": self.coverage.to_dict(),
            "followups": list(self.followups),
            "warnings": list(self.warnings),
            "qa_status": self.qa_status,
            "error": self.error,
        }
