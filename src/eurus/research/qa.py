from __future__ import annotations

from dataclasses import dataclass, field

from eurus.research.models import (
    CitationRecord,
    CoverageReport,
    EvidenceItem,
    EvidenceType,
    QaStatus,
    SourceRecord,
    VerificationStatus,
)
from eurus.research.ranking import domain_diversity
from eurus.research.urlutils import registrable_domain

MIN_SOURCES_DEEP = 3


@dataclass(frozen=True, slots=True)
class QaReport:
    status: QaStatus
    source_findings: tuple[str, ...] = ()
    evidence_findings: tuple[str, ...] = ()
    citation_findings: tuple[str, ...] = ()
    coverage_findings: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    diversity: float = 0.0
    verified_evidence: int = 0
    failed_evidence: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "source_findings": list(self.source_findings),
            "evidence_findings": list(self.evidence_findings),
            "citation_findings": list(self.citation_findings),
            "coverage_findings": list(self.coverage_findings),
            "warnings": list(self.warnings),
            "diversity": round(self.diversity, 3),
            "verified_evidence": self.verified_evidence,
            "failed_evidence": self.failed_evidence,
        }


@dataclass
class QaContext:
    sources: list[SourceRecord] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    citations: list[CitationRecord] = field(default_factory=list)
    coverage: CoverageReport | None = None
    fetch_failures: int = 0
    contradictions: int = 0
    coverage_target: float = 0.8
    diversity_min: float = 0.5
    incomplete: bool = False
    deep_mode: bool = False


def evaluate_quality(context: QaContext) -> QaReport:
    source_findings: list[str] = []
    evidence_findings: list[str] = []
    citation_findings: list[str] = []
    coverage_findings: list[str] = []
    warnings: list[str] = []
    fetched = [item for item in context.sources if item.status == "fetched"]
    domains = [registrable_domain(item.url) for item in fetched]
    diversity = domain_diversity([item.url for item in fetched])
    verified = [
        item for item in context.evidence if item.verification is VerificationStatus.VERIFIED
    ]
    failed = [item for item in context.evidence if item.verification is VerificationStatus.FAILED]
    failed_citations = [item for item in context.citations if not item.verified]
    direct = [item for item in verified if item.evidence_type is EvidenceType.DIRECT]
    context_only = [item for item in verified if item.evidence_type is EvidenceType.CONTEXT]
    if not context.sources:
        source_findings.append("no sources were collected")
    else:
        if len(fetched) < (MIN_SOURCES_DEEP if context.deep_mode else 2):
            source_findings.append(f"only {len(fetched)} source(s) were successfully fetched")
        if len(domains) != len(set(domains)) and diversity < context.diversity_min:
            source_findings.append(
                f"domain diversity {diversity:.2f} is below the {context.diversity_min:.2f} minimum"
            )
        snippet_only = [item for item in context.sources if item.status == "snippet_only"]
        if snippet_only:
            source_findings.append(
                f"{len(snippet_only)} source(s) could only be represented by search snippets"
            )
    if context.fetch_failures:
        warnings.append(f"{context.fetch_failures} fetch attempt(s) failed")
    if context.contradictions:
        warnings.append(f"{context.contradictions} contradictory evidence pair(s) detected")
        evidence_findings.append("contradictory evidence requires manual review")
    if not context.evidence:
        evidence_findings.append("no evidence was extracted")
    else:
        if not verified:
            evidence_findings.append("no evidence passed citation validation")
        else:
            if not direct:
                evidence_findings.append("no direct evidence; all verified evidence is weaker")
            if context_only and len(context_only) == len(verified):
                evidence_findings.append("verified evidence is context-only")
    if failed_citations:
        citation_findings.append(f"{len(failed_citations)} citation(s) failed validation")
        for item in failed_citations[:5]:
            citation_findings.append(f"{item.source_id}: {item.reason}")
    coverage = context.coverage
    if coverage is not None and coverage.total:
        if coverage.unanswered:
            coverage_findings.append(f"{len(coverage.unanswered)} subquestion(s) remain unanswered")
        if coverage.ratio < context.coverage_target:
            coverage_findings.append(
                f"coverage {coverage.ratio:.2f} is below the {context.coverage_target:.2f} target"
            )
    else:
        coverage_findings.append("no coverage evaluation was possible")
    if context.incomplete:
        status = QaStatus.INCOMPLETE
    elif coverage is not None and coverage.total and coverage.ratio < context.coverage_target:
        status = QaStatus.REQUIRES_MORE_RESEARCH
    elif not context.sources or not verified:
        status = QaStatus.FAILED
    elif source_findings or evidence_findings or citation_findings:
        status = QaStatus.PASSED_WITH_WARNINGS
    else:
        status = QaStatus.PASSED
    return QaReport(
        status=status,
        source_findings=tuple(source_findings),
        evidence_findings=tuple(evidence_findings),
        citation_findings=tuple(citation_findings),
        coverage_findings=tuple(coverage_findings),
        warnings=tuple(warnings),
        diversity=diversity,
        verified_evidence=len(verified),
        failed_evidence=len(failed),
    )
