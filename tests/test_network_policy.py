from __future__ import annotations

import time

import pytest

from eurus.errors import ToolError
from eurus.execution.envelope import NetworkProfile
from eurus.network.policy import (
    DEFAULT_PACKAGE_HOSTS,
    EgressPolicy,
    NetworkPolicyEngine,
    NetworkPolicyError,
    PolicySettings,
    RateLimiter,
    build_policy_engine,
    host_matches,
)

PUBLIC_IP = "93.184.216.34"


def resolver_returning(*addresses: str):
    def resolver(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolver


def web_engine(resolver=None, **kwargs) -> NetworkPolicyEngine:
    return build_policy_engine(
        PolicySettings(profile=NetworkProfile.WEB_READ, **kwargs), resolver=resolver
    )


class TestProfileSemantics:
    def test_offline_rejects_everything(self) -> None:
        engine = build_policy_engine(PolicySettings(profile=NetworkProfile.OFFLINE))
        with pytest.raises(NetworkPolicyError, match="offline"):
            engine.check_url("https://example.com/")

    def test_web_read_allows_public_host(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver_returning(PUBLIC_IP),
        )
        decision = engine.check_url("https://example.com/docs?x=1")
        assert decision.host == "example.com"
        assert decision.port == 443
        assert decision.resolved == (PUBLIC_IP,)

    def test_private_literal_rejected(self) -> None:
        engine = web_engine()
        for host in (
            "127.0.0.1",
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.9",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "[fd00:ec2::254]",
            "[::ffff:127.0.0.1]",
        ):
            with pytest.raises(NetworkPolicyError):
                engine.check_url(f"http://{host}/")

    def test_metadata_literal_never_allowed_even_with_private_flag(self) -> None:
        engine = web_engine(allow_private_addresses=True)
        with pytest.raises(NetworkPolicyError, match="metadata"):
            engine.check_url("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(NetworkPolicyError, match="metadata"):
            engine.check_url("http://100.100.100.200/")

    def test_metadata_and_local_hostnames_rejected(self) -> None:
        engine = web_engine(resolver=resolver_returning(PUBLIC_IP))
        for host in (
            "localhost",
            "metadata",
            "metadata.google.internal",
            "host.local",
            "box.internal",
            "x.localhost",
        ):
            with pytest.raises(NetworkPolicyError):
                engine.check_url(f"https://{host}/")

    def test_private_allowed_with_explicit_flag(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ, allow_private_addresses=True),
            resolver=resolver_returning("10.1.2.3"),
        )
        decision = engine.check_url("http://registry.internal.local:80/v2/")
        assert decision.resolved == ("10.1.2.3",)

    def test_port_allowlist_enforced(self) -> None:
        engine = web_engine()
        with pytest.raises(NetworkPolicyError, match="port 8080"):
            engine.check_url("http://example.com:8080/")
        allowed = web_engine(egress_ports=(80, 443, 8443))
        decision = allowed.check_url("https://example.com:8443/")
        assert decision.port == 8443

    def test_non_http_scheme_rejected(self) -> None:
        engine = web_engine()
        for url in ("file:///etc/passwd", "ftp://example.com/", "data:text/html,x"):
            with pytest.raises(NetworkPolicyError):
                engine.check_url(url)

    def test_url_length_and_shape_rejected(self) -> None:
        engine = web_engine()
        with pytest.raises(NetworkPolicyError, match="1 to 2048"):
            engine.check_url("https://" + "a" * 3000 + ".com")
        with pytest.raises(NetworkPolicyError, match="http or https"):
            engine.check_url("https:///no-host")

    def test_dns_failure_rejected(self) -> None:
        def failing(host: str, port: int) -> list[str]:
            raise NetworkPolicyError(f"cannot resolve host: {host}")

        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ), resolver=failing
        )
        with pytest.raises(NetworkPolicyError, match="cannot resolve"):
            engine.check_url("https://nonexistent.invalid/")

    def test_dns_resolving_to_private_ip_rejected(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver_returning("192.168.0.10"),
        )
        with pytest.raises(NetworkPolicyError, match="forbidden private"):
            engine.check_url("https://example.com/")

    def test_allowlisted_host_resolving_to_private_ip_rejected(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ, web_read_hosts=("example.com",)),
            resolver=resolver_returning("127.0.0.1"),
        )
        with pytest.raises(NetworkPolicyError, match="forbidden private"):
            engine.check_url("https://example.com/")


class TestAllowlists:
    def test_web_read_hosts_narrow(self) -> None:
        engine = build_policy_engine(
            PolicySettings(
                profile=NetworkProfile.WEB_READ, web_read_hosts=("example.com", "*.docs.dev")
            ),
            resolver=resolver_returning(PUBLIC_IP),
        )
        engine.check_url("https://example.com/")
        engine.check_url("https://a.docs.dev/")
        with pytest.raises(NetworkPolicyError, match="not permitted"):
            engine.check_url("https://other.com/")

    def test_api_access_empty_allows_public(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.API_ACCESS),
            resolver=resolver_returning(PUBLIC_IP),
        )
        engine.check_url("https://api.example.com/v1")

    def test_api_access_allowlist(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.API_ACCESS, api_hosts=("api.example.com",)),
            resolver=resolver_returning(PUBLIC_IP),
        )
        engine.check_url("https://api.example.com/v1")
        with pytest.raises(NetworkPolicyError, match="not permitted"):
            engine.check_url("https://api.other.com/v1")

    def test_package_install_default_hosts(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.PACKAGE_INSTALL),
            resolver=resolver_returning(PUBLIC_IP),
        )
        assert engine.policy.host_allowlist == DEFAULT_PACKAGE_HOSTS
        engine.check_url("https://pypi.org/simple/pkg/")
        with pytest.raises(NetworkPolicyError, match="not permitted"):
            engine.check_url("https://evil.example.com/pkg.whl")

    def test_restricted_requires_allowlist(self) -> None:
        with pytest.raises(NetworkPolicyError, match="restricted profile requires"):
            build_policy_engine(PolicySettings(profile=NetworkProfile.RESTRICTED))

    def test_restricted_only_allowlisted(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.RESTRICTED, restricted_hosts=("pypi.org",)),
            resolver=resolver_returning(PUBLIC_IP),
        )
        engine.check_url("https://pypi.org/simple/")
        with pytest.raises(NetworkPolicyError, match="not permitted"):
            engine.check_url("https://files.pythonhosted.org/x")

    def test_host_matches_patterns(self) -> None:
        assert host_matches("a.example.com", "*.example.com")
        assert host_matches("example.com", "*.example.com")
        assert host_matches("a.b.example.com", "example.com")
        assert not host_matches("badexample.com", "example.com")
        assert host_matches("any.org", "*")


class TestRedirects:
    def test_follow_resolves_relative_and_revalidates(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ, max_redirects=3),
            resolver=resolver_returning(PUBLIC_IP),
        )
        session = engine.redirect_session()
        next_url = session.follow("https://example.com/a/b", "../c")
        assert next_url == "https://example.com/c"
        assert session.hops == 1

    def test_hop_limit_enforced(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ, max_redirects=2),
            resolver=resolver_returning(PUBLIC_IP),
        )
        session = engine.redirect_session()
        session.follow("https://example.com/a", "https://example.com/b")
        session.follow("https://example.com/b", "https://example.com/c")
        with pytest.raises(NetworkPolicyError, match="too many redirects"):
            session.follow("https://example.com/c", "https://example.com/d")

    def test_empty_location_rejected(self) -> None:
        engine = web_engine(resolver=resolver_returning(PUBLIC_IP))
        session = engine.redirect_session()
        with pytest.raises(NetworkPolicyError, match="location"):
            session.follow("https://example.com/a", "")

    def test_redirect_to_private_rejected(self) -> None:
        seen: dict[str, list[str]] = {}

        def resolver(host: str, port: int) -> list[str]:
            return seen.setdefault(host, [PUBLIC_IP if host == "example.com" else "10.9.9.9"])

        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ), resolver=resolver
        )
        session = engine.redirect_session()
        with pytest.raises(NetworkPolicyError, match="forbidden private"):
            session.follow("https://example.com/a", "http://intranet.example.net/")

    def test_redirect_out_of_restricted_allowlist_rejected(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.RESTRICTED, restricted_hosts=("example.com",)),
            resolver=resolver_returning(PUBLIC_IP),
        )
        session = engine.redirect_session()
        with pytest.raises(NetworkPolicyError, match="not permitted"):
            session.follow("https://example.com/a", "https://other.com/b")


class TestRateLimitsAndCache:
    def test_rate_limiter_window(self) -> None:
        now = [100.0]

        def clock() -> float:
            return now[0]

        limiter = RateLimiter(2, clock=clock)
        limiter.acquire("example.com")
        limiter.acquire("example.com")
        with pytest.raises(NetworkPolicyError, match="rate limit"):
            limiter.acquire("example.com")
        now[0] += 61.0
        limiter.acquire("example.com")

    def test_rate_limiter_disabled_at_zero(self) -> None:
        limiter = RateLimiter(0)
        for _ in range(50):
            limiter.acquire("example.com")

    def test_engine_acquire_uses_policy(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ, requests_per_minute=1),
            resolver=resolver_returning(PUBLIC_IP),
        )
        engine.acquire("example.com")
        with pytest.raises(NetworkPolicyError, match="rate limit"):
            engine.acquire("example.com")

    def test_dns_cache_within_ttl(self) -> None:
        calls: list[str] = []

        def resolver(host: str, port: int) -> list[str]:
            calls.append(host)
            return [PUBLIC_IP]

        now = [1000.0]
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver,
            clock=lambda: now[0],
        )
        engine.check_url("https://example.com/")
        engine.check_url("https://example.com/other")
        assert calls == ["example.com"]
        now[0] += 61.0
        engine.check_url("https://example.com/")
        assert calls == ["example.com", "example.com"]

    def test_address_resolver_returns_validated(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver_returning(PUBLIC_IP, "10.0.0.1"),
        )
        engine_web = engine
        with pytest.raises(NetworkPolicyError, match="forbidden private"):
            engine_web.address_resolver()("mixed.example.com", 443)

    def test_address_resolver_all_valid(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver_returning(PUBLIC_IP),
        )
        assert engine.address_resolver()("example.com", 443) == [PUBLIC_IP]


class TestPolicyValidation:
    def test_egress_policy_rejects_bad_values(self) -> None:
        with pytest.raises(NetworkPolicyError):
            EgressPolicy(profile="web_read", max_redirects=99)
        with pytest.raises(NetworkPolicyError):
            EgressPolicy(profile=NetworkProfile.WEB_READ, allowed_ports=frozenset({0}))
        with pytest.raises(NetworkPolicyError):
            EgressPolicy(profile=NetworkProfile.WEB_READ, allowed_ports=frozenset())
        with pytest.raises(NetworkPolicyError):
            EgressPolicy(profile=NetworkProfile.WEB_READ, requests_per_minute=10**6)
        with pytest.raises(NetworkPolicyError):
            EgressPolicy(profile=NetworkProfile.WEB_READ, max_response_bytes=1)

    def test_policy_settings_defaults(self) -> None:
        engine = build_policy_engine(
            PolicySettings(profile=NetworkProfile.WEB_READ),
            resolver=resolver_returning(PUBLIC_IP),
        )
        assert engine.policy.max_redirects == 6
        assert engine.policy.requests_per_minute == 60
        assert engine.policy.max_response_bytes == 2_000_000
        assert engine.policy.max_tunnel_bytes == 64_000_000

    def test_policy_error_is_tool_error(self) -> None:
        assert issubclass(NetworkPolicyError, ToolError)

    def test_rate_limiter_no_thread_leak(self) -> None:
        limiter = RateLimiter(5)
        before = time.monotonic()
        limiter.acquire("host-a")
        assert time.monotonic() - before < 1.0
