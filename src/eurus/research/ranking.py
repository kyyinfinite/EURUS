from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from eurus.research.models import SearchHit
from eurus.research.urlutils import canonical_url, normalize_url, registrable_domain

STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "for",
        "in",
        "on",
        "to",
        "and",
        "or",
        "vs",
        "versus",
        "about",
        "what",
        "how",
        "why",
        "is",
        "are",
        "with",
        "by",
        "at",
        "from",
        "as",
        "it",
        "its",
        "be",
        "this",
        "that",
        "these",
        "those",
        "apakah",
        "bagaimana",
        "kenapa",
        "mengapa",
        "yang",
        "dan",
        "atau",
        "dengan",
        "untuk",
        "pada",
        "dari",
        "di",
        "ke",
        "adalah",
        "itu",
        "ini",
    }
)

TOKEN_RE = re.compile(r"[a-z0-9]+")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
RELATIVE_DAYS_RE = re.compile(r"(\d+)\s*(?:day|days|hari)", re.IGNORECASE)
RELATIVE_HOURS_RE = re.compile(r"(\d+)\s*(?:hour|hours|jam)", re.IGNORECASE)

FINGERPRINT_PREFIX_CHARS = 40_000


@dataclass(frozen=True, slots=True)
class RankedHit:
    hit: SearchHit
    score: float
    canonical: str
    providers: tuple[str, ...]
    covered_terms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.hit.url,
            "canonical_url": self.canonical,
            "title": self.hit.title,
            "provider": self.hit.provider,
            "providers": list(self.providers),
            "score": round(self.score, 4),
            "covered_terms": round(self.covered_terms, 3),
        }


def content_tokens(text: str) -> list[str]:
    lowered = text.lower()
    return [
        token for token in TOKEN_RE.findall(lowered) if len(token) > 2 and token not in STOPWORDS
    ]


def all_tokens(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.lower()) if token]


def content_fingerprint(text: str) -> str:
    normalized = " ".join(text.split())[:FINGERPRINT_PREFIX_CHARS]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_published_date(raw: str, *, now_epoch_days: float = 0.0) -> float | None:
    value = (raw or "").strip()
    if not value:
        return None
    iso = ISO_DATE_RE.search(value)
    if iso:
        import datetime as dt

        try:
            parsed = dt.datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=dt.UTC
            )
        except ValueError:
            return None
        return parsed.timestamp() / 86_400.0
    days = RELATIVE_DAYS_RE.search(value)
    if days:
        if now_epoch_days <= 0:
            return None
        return now_epoch_days - float(days.group(1))
    hours = RELATIVE_HOURS_RE.search(value)
    if hours:
        if now_epoch_days <= 0:
            return None
        return now_epoch_days - float(hours.group(1)) / 24.0
    return None


def _coverage(query_tokens: list[str], title: str, snippet: str) -> float:
    if not query_tokens:
        return 0.0
    title_tokens = set(all_tokens(title))
    snippet_tokens = set(all_tokens(snippet))
    weight = 0.0
    for token in query_tokens:
        if token in title_tokens:
            weight += 2.0
        elif token in snippet_tokens:
            weight += 1.0
    return min(1.0, weight / (2.0 * len(query_tokens)))


def _freshness(published_at: str, *, now_epoch_days: float) -> float:
    if now_epoch_days <= 0:
        return 0.0
    parsed = parse_published_date(published_at, now_epoch_days=now_epoch_days)
    if parsed is None:
        return 0.0
    age_days = max(0.0, now_epoch_days - parsed)
    if age_days <= 30:
        return 1.0
    if age_days <= 180:
        return 0.5
    return 0.0


def _snippet_richness(snippet: str) -> float:
    return min(1.0, len(snippet.strip()) / 300.0)


def canonical_key(hit: SearchHit) -> str:
    try:
        return canonical_url(hit.url)
    except ValueError:
        return hit.url.strip().lower()


def normalized_display_url(hit: SearchHit) -> str:
    try:
        return normalize_url(hit.url)
    except ValueError:
        return hit.url


def dedupe_hits(hits: list[SearchHit]) -> dict[str, RankedHit]:
    merged: dict[str, RankedHit] = {}
    order: list[str] = []
    for hit in hits:
        key = canonical_key(hit)
        existing = merged.get(key)
        if existing is None:
            merged[key] = RankedHit(
                hit=hit,
                score=0.0,
                canonical=key,
                providers=(hit.provider,),
                covered_terms=0.0,
            )
            order.append(key)
            continue
        providers = existing.providers
        if hit.provider not in providers:
            providers = (*providers, hit.provider)
        kept = existing.hit
        best = SearchHit(
            title=kept.title,
            url=kept.url,
            snippet=kept.snippet,
            provider=kept.provider,
            rank=min(kept.rank, hit.rank),
            published_at=kept.published_at or hit.published_at,
            source_type=kept.source_type or hit.source_type,
        )
        merged[key] = RankedHit(
            hit=best, score=0.0, canonical=key, providers=providers, covered_terms=0.0
        )
    return merged


def rank_hits(
    hits: list[SearchHit],
    query: str,
    *,
    domain_cap: int = 2,
    now_epoch_days: float = 0.0,
    provider_names: tuple[str, ...] = (),
) -> list[RankedHit]:
    merged = dedupe_hits(hits)
    query_tokens = content_tokens(query)
    provider_count = max(1, len(provider_names))
    ranked: list[RankedHit] = []
    for key, entry in merged.items():
        coverage = _coverage(query_tokens, entry.hit.title, entry.hit.snippet)
        rank_score = 1.0 / max(1, entry.hit.rank)
        agreement = min(1.0, (len(entry.providers) - 1) * 0.5)
        freshness = _freshness(entry.hit.published_at, now_epoch_days=now_epoch_days)
        richness = _snippet_richness(entry.hit.snippet)
        score = (
            0.45 * coverage
            + 0.15 * rank_score
            + 0.10 * agreement
            + 0.10 * freshness
            + 0.20 * richness
        )
        if provider_count > 1:
            score += 0.05 * min(1.0, len(entry.providers) / provider_count)
        ranked.append(
            RankedHit(
                hit=entry.hit,
                score=score,
                canonical=key,
                providers=entry.providers,
                covered_terms=coverage,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.canonical))
    if domain_cap <= 0:
        return ranked
    selected: list[RankedHit] = []
    per_domain: dict[str, int] = {}
    for item in ranked:
        site = registrable_domain(item.hit.url)
        count = per_domain.get(site, 0)
        if count >= domain_cap:
            continue
        per_domain[site] = count + 1
        selected.append(item)
    return selected


def hit_domains(hits: list[SearchHit]) -> list[str]:
    return [registrable_domain(hit.url) for hit in hits]


def domain_diversity(urls: list[str]) -> float:
    if not urls:
        return 0.0
    sites = {registrable_domain(url) for url in urls}
    return len(sites) / len(urls)
