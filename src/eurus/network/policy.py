from __future__ import annotations

import ipaddress
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from eurus.errors import ToolError
from eurus.execution.envelope import NetworkProfile

__all__ = [
    "DEFAULT_PACKAGE_HOSTS",
    "EgressPolicy",
    "METADATA_ENDPOINTS",
    "METADATA_HOST_PATTERNS",
    "NetworkPolicyEngine",
    "NetworkPolicyError",
    "PolicyDecision",
    "PolicySettings",
    "RateLimiter",
    "RedirectSession",
    "build_policy_engine",
    "host_matches",
    "is_forbidden_address",
    "parse_ip",
]

METADATA_ENDPOINTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "100.100.100.200",
    }
)

METADATA_HOST_PATTERNS = ("169.254.169.254", "metadata")

DEFAULT_PACKAGE_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")

MAX_URL_LENGTH = 2048

DNS_TTL_SECONDS = 60.0

DNS_MAX_ENTRIES = 1024

RATE_WINDOW_SECONDS = 60.0

_LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".arpa")

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})

Resolver = Callable[[str, int], list[str]]


class NetworkPolicyError(ToolError):
    pass


def parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(raw.split("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(not address.is_global or address.is_multicast or address.is_unspecified)


def host_matches(host: str, pattern: str) -> bool:
    import fnmatch

    host = host.rstrip(".").lower()
    pattern = pattern.strip().lower().lstrip(".")
    if not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    if fnmatch.fnmatch(host, pattern):
        return True
    return host.endswith("." + pattern)


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as exc:
        raise NetworkPolicyError(f"cannot resolve host: {host}") from exc
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    profile: NetworkProfile
    max_redirects: int = 6
    max_response_bytes: int = 2_000_000
    max_tunnel_bytes: int = 64_000_000
    max_request_body_bytes: int = 1_000_000
    requests_per_minute: int = 60
    allowed_ports: frozenset[int] = frozenset({80, 443})
    allow_private_addresses: bool = False
    host_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, NetworkProfile):
            raise NetworkPolicyError("egress policy profile must be a NetworkProfile value")
        if not 0 <= self.max_redirects <= 20:
            raise NetworkPolicyError("egress policy max_redirects must be between 0 and 20")
        if not 1024 <= self.max_response_bytes <= 100_000_000:
            raise NetworkPolicyError("egress policy max_response_bytes is out of range")
        if not 1024 <= self.max_tunnel_bytes <= 1_000_000_000:
            raise NetworkPolicyError("egress policy max_tunnel_bytes is out of range")
        if not 1024 <= self.max_request_body_bytes <= 100_000_000:
            raise NetworkPolicyError("egress policy max_request_body_bytes is out of range")
        if not 0 <= self.requests_per_minute <= 10_000:
            raise NetworkPolicyError(
                "egress policy requests_per_minute must be between 0 and 10000"
            )
        if not self.allowed_ports:
            raise NetworkPolicyError("egress policy must allow at least one port")
        for port in self.allowed_ports:
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise NetworkPolicyError(f"invalid egress port: {port!r}")
        object.__setattr__(self, "allowed_ports", frozenset(self.allowed_ports))
        object.__setattr__(self, "host_allowlist", tuple(self.host_allowlist))
        if self.profile is NetworkProfile.RESTRICTED and not self.host_allowlist:
            raise NetworkPolicyError("the restricted profile requires a non-empty host allowlist")

    def permits_host(self, host: str) -> bool:
        if not self.host_allowlist:
            return self.profile is not NetworkProfile.RESTRICTED
        return any(host_matches(host, pattern) for pattern in self.host_allowlist)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    url: str
    scheme: str
    host: str
    port: int
    resolved: tuple[str, ...]


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        window_seconds: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = requests_per_minute
        self.window = window_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = {}

    def acquire(self, host: str) -> None:
        if self.limit <= 0:
            return
        now = self.clock()
        events = self._events.setdefault(host, deque())
        horizon = now - self.window
        while events and events[0] <= horizon:
            events.popleft()
        if len(events) >= self.limit:
            raise NetworkPolicyError(
                f"rate limit of {self.limit} requests per {int(self.window)}s exceeded "
                f"for host '{host}'"
            )
        events.append(now)


class RedirectSession:
    def __init__(self, engine: NetworkPolicyEngine, *, max_redirects: int | None = None) -> None:
        self.engine = engine
        self.limit = engine.policy.max_redirects if max_redirects is None else max_redirects
        self.hops = 0

    def follow(self, current_url: str, location: str) -> str:
        if not location or not location.strip():
            raise NetworkPolicyError("redirect response did not include a location")
        if self.hops >= self.limit:
            raise NetworkPolicyError(f"too many redirects: more than {self.limit} redirect hops")
        target = urljoin(current_url, location.strip())
        decision = self.engine.check_url(target)
        self.hops += 1
        return decision.url


class NetworkPolicyEngine:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._resolver = resolver or _default_resolver
        self._clock = clock
        self.rate_limiter = RateLimiter(policy.requests_per_minute, clock=clock)
        self._dns_cache: dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}

    def _validated_addresses(self, host: str, port: int) -> tuple[str, ...]:
        cleaned = host.rstrip(".").lower()
        literal = parse_ip(cleaned)
        if literal is not None:
            self._require_address(literal, cleaned)
            return (cleaned,)
        now = self._clock()
        cached = self._dns_cache.get((cleaned, port))
        if cached is not None and cached[0] > now:
            return cached[1]
        raw_addresses = self._resolver(cleaned, port)
        validated: list[str] = []
        for raw in raw_addresses:
            address = parse_ip(str(raw))
            if address is None:
                continue
            self._require_address(address, host)
            if str(address) not in validated:
                validated.append(str(address))
        if not validated:
            raise NetworkPolicyError(f"cannot resolve host: {host}")
        if len(self._dns_cache) >= DNS_MAX_ENTRIES:
            for stale in list(self._dns_cache)[
                : max(0, len(self._dns_cache) - DNS_MAX_ENTRIES + 1)
            ]:
                self._dns_cache.pop(stale, None)
        self._dns_cache[(cleaned, port)] = (now + DNS_TTL_SECONDS, tuple(validated))
        return tuple(validated)

    def _require_address(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str
    ) -> None:
        if str(address) in METADATA_ENDPOINTS:
            raise NetworkPolicyError(
                f"host '{host}' resolves to a cloud metadata endpoint, which is never reachable"
            )
        if is_forbidden_address(address) and not self.policy.allow_private_addresses:
            raise NetworkPolicyError(
                f"host '{host}' resolves to a forbidden private or local address"
            )

    def _check_host(self, host: str) -> str:
        cleaned = host.rstrip(".").lower()
        if not cleaned or len(cleaned) > 253:
            raise NetworkPolicyError(f"invalid hostname: {host!r}")
        literal = parse_ip(cleaned)
        if literal is None:
            metadata_host = cleaned in METADATA_HOST_PATTERNS or cleaned.startswith("metadata.")
            local_name = cleaned in _LOCAL_NAMES or any(
                cleaned.endswith(suffix) for suffix in _LOCAL_NAME_SUFFIXES
            )
            if metadata_host or (local_name and not self.policy.allow_private_addresses):
                raise NetworkPolicyError("local, private, and metadata hostnames are not reachable")
        else:
            self._require_address(literal, cleaned)
        if not self.policy.permits_host(cleaned):
            allowed = ", ".join(self.policy.host_allowlist[:6]) or "none"
            raise NetworkPolicyError(
                f"host '{cleaned}' is not permitted by the {self.policy.profile.value} "
                f"network profile (allowed: {allowed})"
            )
        return cleaned

    def check_url(self, url: str) -> PolicyDecision:
        cleaned = url.strip()
        if not cleaned or len(cleaned) > MAX_URL_LENGTH:
            raise NetworkPolicyError("URL must be 1 to 2048 characters")
        if self.policy.profile is NetworkProfile.OFFLINE:
            raise NetworkPolicyError("network egress is disabled by the offline profile")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NetworkPolicyError("URL must use http or https")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in self.policy.allowed_ports:
            allowed = ", ".join(str(item) for item in sorted(self.policy.allowed_ports))
            raise NetworkPolicyError(
                f"port {port} is not permitted for egress (allowed: {allowed})"
            )
        host = self._check_host(parsed.hostname)
        resolved = self._validated_addresses(host, port)
        return PolicyDecision(
            url=cleaned, scheme=parsed.scheme, host=host, port=port, resolved=resolved
        )

    def check_tunnel(self, host: str, port: int) -> tuple[str, ...]:
        if self.policy.profile is NetworkProfile.OFFLINE:
            raise NetworkPolicyError("network egress is disabled by the offline profile")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise NetworkPolicyError(f"invalid tunnel port: {port!r}")
        if port not in self.policy.allowed_ports:
            allowed = ", ".join(str(item) for item in sorted(self.policy.allowed_ports))
            raise NetworkPolicyError(
                f"port {port} is not permitted for egress (allowed: {allowed})"
            )
        cleaned = self._check_host(host)
        return self._validated_addresses(cleaned, port)

    def redirect_session(self, *, max_redirects: int | None = None) -> RedirectSession:
        return RedirectSession(self, max_redirects=max_redirects)

    def acquire(self, host: str) -> None:
        self.rate_limiter.acquire(host)

    async def acheck_url(self, url: str) -> PolicyDecision:
        import asyncio

        return await asyncio.to_thread(self.check_url, url)

    async def acheck_tunnel(self, host: str, port: int) -> tuple[str, ...]:
        import asyncio

        return await asyncio.to_thread(self.check_tunnel, host, port)

    def address_resolver(self) -> Resolver:
        def resolve(host: str, port: int) -> list[str]:
            return list(self._validated_addresses(host, port))

        return resolve


@dataclass(frozen=True, slots=True)
class PolicySettings:
    profile: NetworkProfile
    max_redirects: int = 6
    max_response_mb: int = 2
    max_tunnel_mb: int = 64
    requests_per_minute: int = 60
    egress_ports: tuple[int, ...] = (80, 443)
    allow_private_addresses: bool = False
    web_read_hosts: tuple[str, ...] = ()
    api_hosts: tuple[str, ...] = ()
    package_hosts: tuple[str, ...] = DEFAULT_PACKAGE_HOSTS
    restricted_hosts: tuple[str, ...] = ()


def _allowlist_for(settings: PolicySettings) -> tuple[str, ...]:
    if settings.profile is NetworkProfile.WEB_READ:
        return settings.web_read_hosts
    if settings.profile is NetworkProfile.API_ACCESS:
        return settings.api_hosts
    if settings.profile is NetworkProfile.PACKAGE_INSTALL:
        return settings.package_hosts
    if settings.profile is NetworkProfile.RESTRICTED:
        return settings.restricted_hosts
    return ()


def build_policy_engine(
    settings: PolicySettings,
    *,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> NetworkPolicyEngine:
    policy = EgressPolicy(
        profile=settings.profile,
        max_redirects=settings.max_redirects,
        max_response_bytes=settings.max_response_mb * 1_000_000,
        max_tunnel_bytes=settings.max_tunnel_mb * 1_000_000,
        requests_per_minute=settings.requests_per_minute,
        allowed_ports=frozenset(settings.egress_ports),
        allow_private_addresses=settings.allow_private_addresses,
        host_allowlist=_allowlist_for(settings),
    )
    return NetworkPolicyEngine(policy, resolver=resolver, clock=clock)
