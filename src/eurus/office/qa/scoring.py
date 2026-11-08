from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eurus.office.models.review import AcceptanceReport, DefectReport, QAReport
from eurus.office.models.task import DefectSeverity


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    content_min: float = 85.0
    visual_min: float = 85.0
    structural_min: float = 95.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_min": self.content_min,
            "visual_min": self.visual_min,
            "structural_min": self.structural_min,
        }


AUTO_REJECT_FRAGMENTS = (
    "corrupt",
    "missing image",
    "text clipping",
    "out of bounds",
    "markdown leakage",
    "no text content",
    "no slides",
    "no native word tables",
)


@dataclass(frozen=True, slots=True)
class AcceptanceDecision:
    accepted: bool
    quality_status: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "quality_status": self.quality_status,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def auto_reject_defects(defects: tuple[DefectReport, ...]) -> tuple[str, ...]:
    reasons: list[str] = []
    for defect in defects:
        if defect.resolution_status in {"fixed", "verified"}:
            continue
        if defect.severity is not DefectSeverity.CRITICAL:
            continue
        text = f"{defect.description} {defect.artifact_location}".lower()
        if any(fragment in text for fragment in AUTO_REJECT_FRAGMENTS):
            reasons.append(f"auto-reject on critical defect: {defect.description[:200]}")
        else:
            reasons.append(f"unresolved critical defect: {defect.description[:200]}")
    return tuple(dict.fromkeys(reasons))


def evaluate_acceptance(
    qa_report: QAReport,
    acceptance: AcceptanceReport,
    thresholds: AcceptanceThresholds,
) -> AcceptanceDecision:
    reasons: list[str] = []
    warnings: list[str] = []
    if qa_report.structural_score is None:
        reasons.append("structural QA did not produce a score")
    elif qa_report.structural_score < thresholds.structural_min:
        reasons.append(
            f"structural score {qa_report.structural_score} is below {thresholds.structural_min}"
        )
    if qa_report.visual_mode == "unavailable" or qa_report.visual_score is None:
        warnings.append("visual inspection could not produce a score for this artifact")
    elif qa_report.visual_score < thresholds.visual_min:
        reasons.append(f"visual score {qa_report.visual_score} is below {thresholds.visual_min}")
    if qa_report.content_score is None:
        reasons.append("content review did not produce a score")
    elif qa_report.content_score < thresholds.content_min:
        reasons.append(f"content score {qa_report.content_score} is below {thresholds.content_min}")
    reasons.extend(auto_reject_defects(qa_report.defects))
    if not acceptance.accepted:
        reasons.append(f"client agent rejected the result: {'; '.join(acceptance.reasons[:5])}")
    if qa_report.unresolved_critical:
        reasons.append("unresolved critical defects remain in the QA report")
    accepted = not reasons
    quality_status = ("passed_with_warnings" if warnings else "passed") if accepted else "rejected"
    return AcceptanceDecision(
        accepted=accepted,
        quality_status=quality_status,
        reasons=tuple(dict.fromkeys(reasons)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def merge_defects(*groups: tuple[DefectReport, ...]) -> tuple[DefectReport, ...]:
    seen: dict[str, DefectReport] = {}
    for group in groups:
        for defect in group:
            key = f"{defect.layer}:{defect.artifact_location}:{defect.description[:120]}"
            if key not in seen:
                seen[key] = defect
    return tuple(seen.values())
