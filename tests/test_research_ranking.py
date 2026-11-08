from __future__ import annotations

import time

import pytest

from eurus.research.models import SearchHit
from eurus.research.ranking import (
    content_fingerprint,
    domain_diversity,
    parse_published_date,
    rank_hits,
)


def hit(
    url: str,
    title: str = "title",
    snippet: str = "snippet",
    provider: str = "ddgs",
    rank: int = 1,
    published: str = "",
) -> SearchHit:
    return SearchHit(
        title=title, url=url, snippet=snippet, provider=provider, rank=rank, published_at=published
    )


def test_dedup_by_exact_url() -> None:
    ranked = rank_hits(
        [
            hit("https://a.com/1", snippet="long snippet with many words to be rich"),
            hit("https://a.com/1", snippet="long snippet with many words to be rich"),
        ],
        "a topic",
        domain_cap=0,
    )
    assert len(ranked) == 1


def test_dedup_by_canonical_url() -> None:
    ranked = rank_hits(
        [
            hit("https://a.com/x?utm_source=news"),
            hit("https://a.com/x/"),
        ],
        "topic",
        domain_cap=0,
    )
    assert len(ranked) == 1
    assert ranked[0].providers == ("ddgs",)


def test_dedup_merges_provider_agreement() -> None:
    ranked = rank_hits(
        [
            hit("https://a.com/x", provider="ddgs"),
            hit("https://a.com/x", provider="brave"),
        ],
        "topic",
        domain_cap=0,
        provider_names=("ddgs", "brave"),
    )
    assert len(ranked) == 1
    assert ranked[0].providers == ("ddgs", "brave")


def test_domain_diversity_caps_same_site() -> None:
    ranked = rank_hits(
        [
            hit("https://a.com/1"),
            hit("https://a.com/2"),
            hit("https://a.com/3"),
            hit("https://b.com/1"),
        ],
        "topic",
        domain_cap=2,
    )
    sites = [item.canonical.split("/")[2] for item in ranked]
    assert sites.count("a.com") <= 2
    assert "b.com" in sites


def test_ranking_prefers_term_coverage() -> None:
    ranked = rank_hits(
        [
            hit("https://a.com/1", title="solid state battery", snippet="solid state battery 2024"),
            hit("https://b.com/1", title="unrelated", snippet="something else entirely"),
        ],
        "solid state battery",
        domain_cap=0,
    )
    assert ranked[0].hit.url == "https://a.com/1"
    assert ranked[0].covered_terms > ranked[1].covered_terms


def test_ranking_freshness_requires_real_dates() -> None:
    now_days = time.time() / 86_400.0
    recent = rank_hits(
        [hit("https://a.com/1", published="2026-10-30")],
        "topic",
        domain_cap=0,
        now_epoch_days=now_days,
    )
    undated = rank_hits([hit("https://a.com/1")], "topic", domain_cap=0, now_epoch_days=now_days)
    assert recent[0].score > undated[0].score


def test_parse_published_date_supports_iso_and_relative() -> None:
    now = 20_000.0
    assert parse_published_date("2026-10-30", now_epoch_days=now) is not None
    assert parse_published_date("3 days ago", now_epoch_days=now) == pytest.approx(now - 3)
    assert parse_published_date("", now_epoch_days=now) is None
    assert parse_published_date("no date here", now_epoch_days=now) is None
    assert parse_published_date("3 days ago", now_epoch_days=0) is None


def test_content_fingerprint_ignores_whitespace() -> None:
    assert content_fingerprint("a  b\nc") == content_fingerprint("a b \n c")
    assert content_fingerprint("a b") != content_fingerprint("b a")


def test_domain_diversity_metric() -> None:
    assert domain_diversity([]) == 0.0
    assert domain_diversity(["https://a.com/1", "https://a.com/2"]) == 0.5
    assert domain_diversity(["https://a.com/1", "https://b.com/2"]) == 1.0
