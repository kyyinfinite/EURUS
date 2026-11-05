from __future__ import annotations

import pytest

from eurus.research.urlutils import (
    canonical_url,
    normalize_url,
    registrable_domain,
    strip_tracking_params,
    url_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.com/a/", "https://example.com/a/"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
    ],
)
def test_normalize_url_keeps_meaningful_parts(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://example.com/a/?utm_source=x&utm_medium=y&id=42",
            "https://example.com/a/?id=42",
        ),
        (
            "https://example.com/p?fbclid=abc&gclid=xyz&keep=1",
            "https://example.com/p?keep=1",
        ),
    ],
)
def test_normalize_url_strips_tracking_parameters(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_keeps_content_parameters() -> None:
    kept = strip_tracking_params("ref=main&page=2&utm_campaign=z")
    assert ("ref", "main") in kept
    assert ("page", "2") in kept
    assert all(name != "utm_campaign" for name, _ in kept)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.com/x/", "https://a.com/x"),
        ("https://a.com", "https://a.com"),
        ("https://a.com/", "https://a.com"),
        ("https://a.com/x?b=2&a=1", "https://a.com/x?a=1&b=2"),
        ("https://a.com/x#fragment", "https://a.com/x"),
    ],
)
def test_canonical_url_normalizes_for_dedup(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_canonical_url_keeps_content_parameters_separate() -> None:
    assert canonical_url("https://a.com/x?b=1") != canonical_url("https://a.com/x?b=2")


def test_canonical_url_merges_trailing_slash_variants() -> None:
    assert canonical_url("https://a.com/x") == canonical_url("https://a.com/x/")
    assert canonical_url("https://a.com/x?utm_term=q") == canonical_url("https://a.com/x")
    assert canonical_url("https://a.com/x?b=1&c=2") == canonical_url("https://a.com/x?c=2&b=1")


def test_normalize_url_rejects_invalid_targets() -> None:
    with pytest.raises(ValueError, match="no host"):
        normalize_url("https:///path")
    with pytest.raises(ValueError, match="2048"):
        normalize_url("https://example.com/" + "a" * 2100)


def test_url_domain_and_registrable_domain() -> None:
    assert url_domain("https://News.Example.co.uk/x") == "news.example.co.uk"
    assert registrable_domain("https://news.example.co.uk/x") == "example.co.uk"
    assert registrable_domain("https://example.com/x") == "example.com"
    assert registrable_domain("https://127.0.0.1/x") == "127.0.0.1"
    assert registrable_domain("https://deep.sub.example.com/x") == "example.com"
