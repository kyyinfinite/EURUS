from __future__ import annotations

import re
from dataclasses import replace

from eurus.research.models import (
    CitationRecord,
    ContradictionRecord,
    EvidenceItem,
    EvidenceType,
    ResearchPlan,
    SourceRecord,
    VerificationStatus,
)
from eurus.research.ranking import content_tokens
from eurus.research.settings import ResearchSettings

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
NUMERIC_VALUE_RE = re.compile(
    r"(\d[\d.,\s]*)\s*(%|usd|eur|gbp|idr|jpy|km|kg|mw|gwh|wh|tons?|years?|hours?|"
    r"celsius|°c|billion|million|trillion|miliar|juta)\b",
    re.IGNORECASE,
)
UNIT_CLASSES = {
    "%": "percent",
    "usd": "currency",
    "eur": "currency",
    "gbp": "currency",
    "idr": "currency",
    "jpy": "currency",
    "km": "distance",
    "kg": "mass",
    "mw": "power",
    "gwh": "energy",
    "wh": "energy",
    "tons": "mass",
    "ton": "mass",
    "years": "time",
    "year": "time",
    "hours": "time",
    "hour": "time",
    "celsius": "temperature",
    "°c": "temperature",
    "billion": "count",
    "million": "count",
    "trillion": "count",
    "miliar": "count",
    "juta": "count",
}
MIN_CONTRADICTION_GAP = 0.1


def normalize_ws(text: str) -> str:
    return " ".join(text.split()).casefold()


def parse_numeric(raw: str) -> float | None:
    cleaned = raw.strip().replace(" ", "")
    if not cleaned:
        return None
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        cleaned = head + tail if len(tail) == 3 and head else cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _numeric_units(text: str) -> list[tuple[float, str, str]]:
    found: list[tuple[float, str, str]] = []
    for match in NUMERIC_VALUE_RE.finditer(text):
        value = parse_numeric(match.group(1))
        if value is None:
            continue
        unit_token = match.group(2).lower()
        unit_class = UNIT_CLASSES.get(unit_token, unit_token)
        found.append((value, unit_class, match.group(0)))
    return found


def _best_window(text: str, topic_tokens: list[str]) -> tuple[str, float]:
    if not topic_tokens:
        return "", 0.0
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    best_excerpt = ""
    best_overlap = 0.0
    for paragraph in paragraphs:
        sentences = [item for item in SENTENCE_SPLIT_RE.split(paragraph) if item.strip()]
        if not sentences:
            continue
        for index, _sentence in enumerate(sentences):
            window_parts = sentences[max(0, index - 1) : index + 2]
            window = " ".join(window_parts)
            window_tokens = set(content_tokens(window))
            overlap = sum(1 for token in topic_tokens if token in window_tokens) / len(topic_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                best_excerpt = window
    return best_excerpt, best_overlap


class EvidenceEngine:
    def __init__(self, *, settings: ResearchSettings) -> None:
        self.settings = settings

    def build_evidence(
        self,
        plan: ResearchPlan,
        sources: list[SourceRecord],
        *,
        timestamp: str = "",
    ) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        fetched = [source for source in sources if source.status == "fetched" and source.content]
        topics: list[str] = list(plan.subquestions) or [plan.objective]
        counter = 0
        for topic in topics:
            topic_tokens = content_tokens(topic)
            if not topic_tokens:
                continue
            for source in fetched:
                excerpt, overlap = _best_window(source.content, topic_tokens)
                if not excerpt or overlap <= 0:
                    continue
                if overlap >= self.settings.claim_term_overlap_direct:
                    kind = EvidenceType.DIRECT
                    confidence = 0.7 + 0.3 * min(1.0, overlap)
                elif overlap >= self.settings.claim_term_overlap_partial:
                    kind = EvidenceType.PARTIAL
                    confidence = 0.4 + 0.3 * min(1.0, overlap)
                else:
                    kind = EvidenceType.CONTEXT
                    confidence = 0.2 + 0.2 * min(1.0, overlap)
                counter += 1
                items.append(
                    EvidenceItem(
                        evidence_id=f"ev-{counter:04d}",
                        source_id=source.source_id,
                        claim=topic,
                        excerpt=excerpt.strip()[: self.settings.evidence_max_excerpt_chars],
                        evidence_type=kind,
                        confidence=round(min(0.99, confidence), 3),
                        location="",
                        verification=VerificationStatus.PENDING,
                        topic=topic,
                        created_at=timestamp,
                    )
                )
        return items

    def validate_citations(
        self, evidence: list[EvidenceItem], sources: list[SourceRecord]
    ) -> tuple[list[EvidenceItem], list[CitationRecord]]:
        by_id = {source.source_id: source for source in sources}
        updated: list[EvidenceItem] = []
        citations: list[CitationRecord] = []
        for item in evidence:
            source = by_id.get(item.source_id)
            if source is None:
                citations.append(
                    CitationRecord(
                        source_id=item.source_id,
                        url="",
                        title="",
                        verified=False,
                        reason="source not found",
                    )
                )
                updated.append(replace(item, verification=VerificationStatus.FAILED))
                continue
            if normalize_ws(item.excerpt) not in normalize_ws(source.content):
                citations.append(
                    CitationRecord(
                        source_id=item.source_id,
                        url=source.url,
                        title=source.title,
                        verified=False,
                        reason="excerpt not found in source content",
                    )
                )
                updated.append(replace(item, verification=VerificationStatus.FAILED))
                continue
            claim_tokens = content_tokens(item.claim)
            excerpt_tokens = set(content_tokens(item.excerpt))
            if claim_tokens:
                relevance = sum(1 for token in claim_tokens if token in excerpt_tokens) / len(
                    claim_tokens
                )
            else:
                relevance = 0.0
            if relevance < self.settings.citation_relevance_min:
                citations.append(
                    CitationRecord(
                        source_id=item.source_id,
                        url=source.url,
                        title=source.title,
                        verified=False,
                        reason="claim is not supported by the excerpt",
                    )
                )
                updated.append(replace(item, verification=VerificationStatus.FAILED))
                continue
            citations.append(
                CitationRecord(
                    source_id=item.source_id,
                    url=source.url,
                    title=source.title,
                    verified=True,
                )
            )
            updated.append(replace(item, verification=VerificationStatus.VERIFIED))
        return updated, citations

    def detect_contradictions(
        self, evidence: list[EvidenceItem], sources: list[SourceRecord]
    ) -> list[ContradictionRecord]:
        by_id = {source.source_id: source for source in sources}
        verified = [item for item in evidence if item.verification is VerificationStatus.VERIFIED]
        records: list[ContradictionRecord] = []
        seen_pairs: set[tuple[str, str]] = set()
        for index, left in enumerate(verified):
            left_source = by_id.get(left.source_id)
            if left_source is None:
                continue
            for right in verified[index + 1 :]:
                if left.topic != right.topic:
                    continue
                right_source = by_id.get(right.source_id)
                if right_source is None or right_source.domain == left_source.domain:
                    continue
                first_id, second_id = sorted((left.evidence_id, right.evidence_id))
                pair = (first_id, second_id)
                if pair in seen_pairs:
                    continue
                for left_value, unit_class, left_raw in _numeric_units(left.excerpt):
                    for right_value, right_class, right_raw in _numeric_units(right.excerpt):
                        if unit_class != right_class:
                            continue
                        gap = abs(left_value - right_value) / max(
                            abs(left_value), abs(right_value), 1e-9
                        )
                        if gap < MIN_CONTRADICTION_GAP:
                            continue
                        if left_value == 0 and right_value == 0:
                            continue
                        seen_pairs.add(pair)
                        records.append(
                            ContradictionRecord(
                                topic=left.topic,
                                left_evidence_id=left.evidence_id,
                                right_evidence_id=right.evidence_id,
                                left_value=left_raw,
                                right_value=right_raw,
                                unit=unit_class,
                            )
                        )
                        break
                    if pair in seen_pairs:
                        break
                if len(records) >= self.settings.deep_max_contradictions:
                    return records
        return records
