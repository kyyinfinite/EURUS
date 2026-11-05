from __future__ import annotations

from eurus.research.models import (
    CitationRecord,
    CoverageReport,
    EvidenceItem,
    EvidenceType,
    SourceRecord,
    VerificationStatus,
)
from eurus.research.qa import QaContext, evaluate_quality


def fetched(url: str, source_id: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        url=url,
        title=f"source {source_id}",
        provider="ddgs",
        status="fetched",
        content="some content",
        content_chars=12,
    )


def evidence(item_id: str, topic: str, source_id: str, kind: EvidenceType) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=item_id,
        source_id=source_id,
        claim=topic,
        excerpt=f"excerpt about {topic}",
        evidence_type=kind,
        confidence=0.8,
        topic=topic,
        verification=VerificationStatus.VERIFIED,
    )


def citation(source_id: str, url: str, verified: bool) -> CitationRecord:
    return CitationRecord(
        source_id=source_id,
        url=url,
        title="t",
        verified=verified,
        reason="" if verified else "excerpt not found in source content",
    )


def test_passed_when_coverage_and_quality_are_good() -> None:
    context = QaContext(
        sources=[fetched("https://a.com/1", "src-001"), fetched("https://b.com/2", "src-002")],
        evidence=[evidence("ev-0001", "topic one", "src-001", EvidenceType.DIRECT)],
        citations=[citation("src-001", "https://a.com/1", True)],
        coverage=CoverageReport(total=1, answered=("topic one",), unanswered=(), ratio=1.0),
        coverage_target=0.8,
    )
    report = evaluate_quality(context)
    assert report.status.value == "PASSED"
    assert report.verified_evidence == 1


def test_requires_more_research_when_coverage_is_short() -> None:
    context = QaContext(
        sources=[fetched("https://a.com/1", "src-001")],
        evidence=[evidence("ev-0001", "topic one", "src-001", EvidenceType.DIRECT)],
        citations=[citation("src-001", "https://a.com/1", True)],
        coverage=CoverageReport(
            total=3, answered=("topic one",), unanswered=("topic two", "topic three"), ratio=1 / 3
        ),
        coverage_target=0.8,
    )
    report = evaluate_quality(context)
    assert report.status.value == "REQUIRES_MORE_RESEARCH"
    assert any("unanswered" in item for item in report.coverage_findings)


def test_incomplete_when_research_stopped_early() -> None:
    context = QaContext(
        sources=[fetched("https://a.com/1", "src-001")],
        evidence=[evidence("ev-0001", "topic one", "src-001", EvidenceType.DIRECT)],
        citations=[citation("src-001", "https://a.com/1", True)],
        coverage=CoverageReport(total=2, answered=("topic one",), unanswered=("t2",), ratio=0.5),
        coverage_target=0.8,
        incomplete=True,
    )
    report = evaluate_quality(context)
    assert report.status.value == "INCOMPLETE"


def test_failed_when_no_verified_evidence() -> None:
    context = QaContext(
        sources=[fetched("https://a.com/1", "src-001")],
        evidence=[],
        citations=[],
        coverage=CoverageReport(total=1, answered=("t",), unanswered=(), ratio=1.0),
        coverage_target=0.8,
    )
    report = evaluate_quality(context)
    assert report.status.value == "FAILED"
    assert any("no evidence" in item for item in report.evidence_findings)


def test_warnings_for_failed_citations_and_diversity() -> None:
    context = QaContext(
        sources=[
            fetched("https://same.com/1", "src-001"),
            fetched("https://same.com/2", "src-002"),
            fetched("https://same.com/3", "src-003"),
        ],
        evidence=[evidence("ev-0001", "topic one", "src-001", EvidenceType.DIRECT)],
        citations=[
            citation("src-001", "https://same.com/1", True),
            citation("src-002", "https://same.com/2", False),
        ],
        coverage=CoverageReport(total=1, answered=("topic one",), unanswered=(), ratio=1.0),
        coverage_target=0.8,
        fetch_failures=2,
        contradictions=1,
        diversity_min=0.5,
    )
    report = evaluate_quality(context)
    assert report.status.value == "PASSED_WITH_WARNINGS"
    assert report.diversity < 0.5
    assert any("failed validation" in item for item in report.citation_findings)
    assert any("contradictory" in item for item in report.evidence_findings)
    assert any("fetch attempt" in item for item in report.warnings)


def test_context_only_evidence_is_flagged() -> None:
    context = QaContext(
        sources=[fetched("https://a.com/1", "src-001"), fetched("https://b.com/2", "src-002")],
        evidence=[evidence("ev-0001", "topic one", "src-001", EvidenceType.CONTEXT)],
        citations=[citation("src-001", "https://a.com/1", True)],
        coverage=CoverageReport(total=1, answered=("topic one",), unanswered=(), ratio=1.0),
        coverage_target=0.8,
    )
    report = evaluate_quality(context)
    assert report.status.value == "PASSED_WITH_WARNINGS"
    assert any("context-only" in item for item in report.evidence_findings)
