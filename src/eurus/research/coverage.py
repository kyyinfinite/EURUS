from __future__ import annotations

from dataclasses import dataclass

from eurus.research.models import CoverageReport, EvidenceItem, EvidenceType, VerificationStatus


@dataclass(frozen=True, slots=True)
class CoverageBreakdown:
    report: CoverageReport
    answered_evidence: dict[str, list[str]]


def evaluate_coverage(
    subquestions: list[str],
    evidence: list[EvidenceItem],
    *,
    target: float = 0.8,
) -> CoverageBreakdown:
    if not subquestions:
        empty = CoverageReport()
        return CoverageBreakdown(report=empty, answered_evidence={})
    answered: list[str] = []
    unanswered: list[str] = []
    mapping: dict[str, list[str]] = {}
    for question in subquestions:
        supporting = [
            item.evidence_id
            for item in evidence
            if item.topic == question
            and item.verification is VerificationStatus.VERIFIED
            and item.evidence_type in {EvidenceType.DIRECT, EvidenceType.PARTIAL}
        ]
        if supporting:
            answered.append(question)
            mapping[question] = supporting
        else:
            unanswered.append(question)
    ratio = len(answered) / len(subquestions)
    report = CoverageReport(
        total=len(subquestions),
        answered=tuple(answered),
        unanswered=tuple(unanswered),
        ratio=ratio,
    )
    return CoverageBreakdown(report=report, answered_evidence=mapping)


def coverage_reached(report: CoverageReport, *, target: float) -> bool:
    if report.total == 0:
        return True
    return report.ratio >= target


def remaining_gaps(report: CoverageReport) -> list[str]:
    return list(report.unanswered)
