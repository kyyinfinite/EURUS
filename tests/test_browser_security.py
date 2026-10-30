from __future__ import annotations

import asyncio

import pytest

from eurus.browser.security import (
    BrowserPolicyError,
    DnsCache,
    DomainPolicy,
    UrlGuard,
    check_url_syntax,
    domain_matches,
    guarded_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "chrome://settings",
        "chrome-devtools://devtools",
        "about:blank",
        "data:text/html,hi",
        "javascript:alert(1)",
        "blob:https://example.com/x",
        "view-source:http://example.com",
        "ftp://example.com/",
        "ws://example.com/",
        "not a url",
        "",
        "https://" + "a" * 3000 + ".com",
    ],
)
def test_check_url_syntax_rejects(url: str) -> None:
    with pytest.raises(BrowserPolicyError):
        check_url_syntax(url)


def test_check_url_syntax_accepts_http_https() -> None:
    assert check_url_syntax("https://example.com/a?b=c") == "https://example.com/a?b=c"
    assert check_url_syntax("http://example.com").startswith("http://")


def test_guarded_url_blocks_private_and_metadata() -> None:
    for url in (
        "http://localhost/",
        "http://LOCALHOST:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://172.16.0.9/",
        "http://[::1]/",
        "http://[fd00:ec2::254]/",
        "http://0.0.0.0/",
    ):
        with pytest.raises(BrowserPolicyError):
            guarded_url(url)


def test_guarded_url_allows_public() -> None:
    assert guarded_url("https://example.com/") == "https://example.com/"
    assert guarded_url("https://8.8.8.8/dns-query") == "https://8.8.8.8/dns-query"


def test_domain_matches_patterns() -> None:
    assert domain_matches("example.com", "example.com")
    assert domain_matches("sub.example.com", "example.com")
    assert domain_matches("sub.example.com", "*.example.com")
    assert domain_matches("example.com", "*.example.com")
    assert not domain_matches("badexample.com", "example.com")
    assert not domain_matches("example.com", "*.other.com")
    assert domain_matches("anything.org", "*")


def test_domain_policy_allowlist_and_blocklist() -> None:
    policy = DomainPolicy(allowed_domains=("example.com",))
    policy.check("example.com")
    policy.check("docs.example.com")
    with pytest.raises(BrowserPolicyError, match="allowlist"):
        policy.check("other.net")

    blocked = DomainPolicy(blocked_domains=("tracker.io",))
    blocked.check("example.com")
    with pytest.raises(BrowserPolicyError, match="blocked"):
        blocked.check("ads.tracker.io")


def test_url_guard_explicit_local_allowlist_bypass() -> None:
    guard = UrlGuard(domain_policy=DomainPolicy(allowed_domains=("127.0.0.1",)))
    cleaned = guard.check_sync("http://127.0.0.1:8080/dev")
    assert cleaned.startswith("http://127.0.0.1:8080")
    metadata = UrlGuard(domain_policy=DomainPolicy(allowed_domains=("169.254.169.254",)))
    with pytest.raises(BrowserPolicyError, match="metadata"):
        metadata.check_sync("http://169.254.169.254/latest/meta-data/")


def test_url_guard_blocks_unlisted_local_even_with_allowlist() -> None:
    guard = UrlGuard(domain_policy=DomainPolicy(allowed_domains=("example.com",)))
    with pytest.raises(BrowserPolicyError):
        guard.check_sync("http://127.0.0.1:9000/")


def test_dns_cache_resolves_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    calls: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        if host == "private.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
        if host == "broken.example":
            raise OSError("name resolution failed")
        if host == "mixed.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.1", 0)),
            ]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    async def run() -> None:
        cache = DnsCache(ttl_seconds=60)
        first = await cache.resolve("example.com", 443)
        second = await cache.resolve("example.com", 443)
        assert first == second
        assert calls.count("example.com") == 1
        with pytest.raises(BrowserPolicyError, match="forbidden private"):
            await cache.resolve("private.example", 443)
        with pytest.raises(BrowserPolicyError, match="forbidden private"):
            await cache.resolve("mixed.example", 443)
        with pytest.raises(BrowserPolicyError, match="cannot resolve"):
            await cache.resolve("broken.example", 443)

    asyncio.run(run())


def test_dns_cache_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    calls: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    async def run() -> None:
        cache = DnsCache(ttl_seconds=0.05)
        await cache.resolve("example.com", 443)
        await cache.resolve("example.com", 443)
        assert calls.count("example.com") == 1
        import time as time_module

        time_module.sleep(0.08)
        await cache.resolve("example.com", 443)
        assert calls.count("example.com") == 2

    asyncio.run(run())


def test_url_guard_check_returns_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    async def run() -> None:
        guard = UrlGuard()
        result = await guard.check("https://example.com/page")
        assert result.resolved == ("93.184.216.34",)
        literal = await guard.check("https://8.8.8.8/x")
        assert literal.resolved == ("8.8.8.8",)

    asyncio.run(run())


def test_url_guard_rejects_long_and_empty() -> None:
    guard = UrlGuard()
    with pytest.raises(BrowserPolicyError):
        guard.check_sync("")
    with pytest.raises(BrowserPolicyError):
        guard.check_sync("https://" + "a" * 3000)
