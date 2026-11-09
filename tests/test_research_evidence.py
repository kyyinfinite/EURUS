from __future__ import annotations

from eurus.research.evidence import EvidenceEngine, parse_numeric
from eurus.research.models import (
    EvidenceItem,
    ResearchMode,
    ResearchPlan,
    SourceRecord,
    VerificationStatus,
)
from eurus.research.settings import ResearchSettings

PLAN = ResearchPlan(
    objective="solid state battery progress",
    mode=ResearchMode.DEEP,
    main_query="solid state battery progress",
    subquestions=(
        "energy density of solid state batteries",
        "production timeline of solid state batteries",
    ),
)


def source(source_id: str, url: str, content: str, status: str = "fetched") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        url=url,
        title=f"Source {source_id}",
        provider="ddgs",
        status=status,
        content=content,
        content_chars=len(content),
    )


CONTENT_A = (
    "Solid state batteries reached 400 Wh/kg energy density in 2026. "
    "Pilot lines run at small scale today.\n\n"
    "Mass production of solid state batteries is planned for 2027 by several makers. "
    "Costs remain the main blocker for the production timeline."
)
CONTENT_B = (
    "Analysts report 320 Wh/kg energy density for solid state batteries in recent tests. "
    "Production timeline extends beyond 2030 for volume manufacturing."
)


def make_engine() -> EvidenceEngine:
    return EvidenceEngine(settings=ResearchSettings())


def test_build_evidence_extracts_windows_per_subquestion() -> None:
    engine = make_engine()
    evidence = engine.build_evidence(PLAN, [source("src-001", "https://a.com/1", CONTENT_A)])
    topics = {item.topic for item in evidence}
    assert topics == set(PLAN.subquestions)
    assert all(item.verification is VerificationStatus.PENDING for item in evidence)
    assert all(item.location == "" for item in evidence)
    assert all(item.excerpt for item in evidence)


def test_build_evidence_skips_unfetched_sources() -> None:
    engine = make_engine()
    evidence = engine.build_evidence(
        PLAN,
        [
            source("src-001", "https://a.com/1", "", status="fetch_failed"),
            source("src-002", "https://a.com/2", "", status="snippet_only"),
        ],
    )
    assert evidence == []


def test_validate_citations_accepts_real_excerpts() -> None:
    engine = make_engine()
    sources = [source("src-001", "https://a.com/1", CONTENT_A)]
    evidence = engine.build_evidence(PLAN, sources)
    updated, citations = engine.validate_citations(evidence, sources)
    assert all(item.verification is VerificationStatus.VERIFIED for item in updated)
    assert all(record.verified for record in citations)
    assert all(record.url == "https://a.com/1" for record in citations)


def test_validate_citations_flags_unknown_source() -> None:
    engine = make_engine()
    unknown_source = EvidenceItem(
        evidence_id="ev-0001",
        source_id="src-404",
        claim="energy density of solid state batteries",
        excerpt="400 Wh/kg energy density",
    )
    records = [source("src-001", "https://a.com/1", CONTENT_A)]
    updated, citations = engine.validate_citations([unknown_source], records)
    assert updated[0].verification is VerificationStatus.FAILED
    assert citations[0].verified is False
    assert citations[0].reason == "source not found"


def test_validate_citations_flags_forged_excerpt() -> None:
    engine = make_engine()
    sources = [source("src-001", "https://a.com/1", CONTENT_A)]
    forged = EvidenceItem(
        evidence_id="ev-0001",
        source_id="src-001",
        claim="energy density of solid state batteries",
        excerpt="solid state batteries reached 900 Wh/kg which is invented",
    )
    updated, citations = engine.validate_citations([forged], sources)
    assert updated[0].verification is VerificationStatus.FAILED
    assert citations[0].reason == "excerpt not found in source content"


def test_validate_citations_flags_irrelevant_claims() -> None:
    engine = make_engine()
    sources = [source("src-001", "https://a.com/1", CONTENT_A)]
    off_topic = EvidenceItem(
        evidence_id="ev-0001",
        source_id="src-001",
        claim="unrelated quantum computing claim",
        excerpt="Solid state batteries reached 400 Wh/kg energy density in 2026.",
    )
    updated, citations = engine.validate_citations([off_topic], sources)
    assert updated[0].verification is VerificationStatus.FAILED
    assert citations[0].reason == "claim is not supported by the excerpt"


def test_detect_contradictions_flags_numeric_conflicts() -> None:
    engine = make_engine()
    sources = [
        source("src-001", "https://a.com/1", CONTENT_A),
        source("src-002", "https://b.com/2", CONTENT_B),
    ]
    evidence = engine.build_evidence(PLAN, sources)
    evidence, _ = engine.validate_citations(evidence, sources)
    contradictions = engine.detect_contradictions(evidence, sources)
    assert contradictions
    assert all(record.unit == "energy" for record in contradictions)
    assert any("400 Wh" in record.left_value for record in contradictions)
    assert any("320 Wh" in record.right_value for record in contradictions)


def test_detect_contradictions_ignores_same_domain() -> None:
    engine = make_engine()
    sources = [
        source("src-001", "https://a.com/1", CONTENT_A),
        source("src-002", "https://a.com/2", CONTENT_B),
    ]
    evidence = engine.build_evidence(PLAN, sources)
    evidence, _ = engine.validate_citations(evidence, sources)
    assert engine.detect_contradictions(evidence, sources) == []


def test_parse_numeric_handles_formats() -> None:
    assert parse_numeric("400") == 400.0
    assert parse_numeric("1,234.5") == 1234.5
    assert parse_numeric("1.234,5") == 1234.5
    assert parse_numeric("12,345") == 12345.0
    assert parse_numeric("") is None
    assert parse_numeric("abc") is None
