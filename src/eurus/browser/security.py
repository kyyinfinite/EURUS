from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from eurus.errors import ToolError

FORBIDDEN_SCHEMES = (
    "file:",
    "chrome:",
    "chrome-devtools:",
    "devtools:",
    "about:",
    "data:",
    "javascript:",
    "blob:",
    "view-source:",
    "vbscript:",
    "ws:",
    "wss:",
)

BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.goog",
    }
)

BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".arpa")

METADATA_ENDPOINTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "100.100.100.200",
    }
)

METADATA_HOST_PATTERNS = ("169.254.169.254", "metadata")

MAX_URL_LENGTH = 2048


class BrowserPolicyError(ToolError):
    pass


def _forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    return bool(not address.is_global or address.is_multicast or address.is_unspecified)


def _parse_address(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(raw.split("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def _hostname_allowed(host: str) -> bool:
    cleaned = host.rstrip(".").lower()
    if cleaned in BLOCKED_HOSTS or cleaned.endswith(BLOCKED_HOST_SUFFIXES):
        return False
    return not (not cleaned or len(cleaned) > 253)


def _address_allowed(address_text: str) -> bool:
    parsed = _parse_address(address_text)
    if parsed is None:
        return False
    if str(parsed) in METADATA_ENDPOINTS:
        return False
    return not _forbidden(parsed)


def check_hostname_literal(host: str) -> None:
    cleaned = host.rstrip(".").lower()
    if not _hostname_allowed(cleaned):
        raise BrowserPolicyError("local, private, and metadata hostnames are not allowed")
    address = _parse_address(cleaned)
    if address is not None and not _address_allowed(str(address)):
        raise BrowserPolicyError(
            "navigation to private, local, or cloud metadata addresses is not allowed"
        )


def domain_matches(host: str, pattern: str) -> bool:
    host = host.rstrip(".").lower()
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    if fnmatch.fnmatch(host, pattern):
        return True
    return host.endswith("." + pattern)


class DomainPolicy:
    def __init__(
        self,
        *,
        allowed_domains: tuple[str, ...] = (),
        blocked_domains: tuple[str, ...] = (),
    ) -> None:
        self.allowed_domains = tuple(item.strip().lower().lstrip(".") for item in allowed_domains)
        self.blocked_domains = tuple(item.strip().lower().lstrip(".") for item in blocked_domains)

    def check(self, host: str) -> None:
        cleaned = host.rstrip(".").lower()
        for pattern in self.blocked_domains:
            if domain_matches(cleaned, pattern):
                raise BrowserPolicyError(f"domain '{cleaned}' is blocked by the browser policy")
        if not self.allowed_domains:
            return
        for pattern in self.allowed_domains:
            if domain_matches(cleaned, pattern):
                return
        allowed = ", ".join(self.allowed_domains[:6])
        raise BrowserPolicyError(
            f"domain '{cleaned}' is outside the configured browser allowlist ({allowed})"
        )


class DnsCache:
    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 1024) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[tuple[str, int], asyncio.Future[tuple[str, ...]]] = {}

    def _cached(self, key: tuple[str, int]) -> tuple[str, ...] | None:
        stamp, value = self._entries.get(key, (0.0, ()))
        if stamp > time.monotonic():
            return value
        self._entries.pop(key, None)
        return None

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        cleaned = host.rstrip(".").lower()
        key = (cleaned, port)
        cached = self._cached(key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached(key)
            if cached is not None:
                return cached
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                task = asyncio.create_task(self._resolve(cleaned, port))
                task.add_done_callback(lambda _task: None)
            else:
                task = None
        try:
            addresses = await asyncio.shield(future)
        except BaseException:
            if task is not None:
                task.cancel()
            raise
        finally:
            async with self._lock:
                self._inflight.pop(key, None)
        return addresses

    async def _resolve(self, host: str, port: int) -> None:
        key = (host, port)
        future = self._inflight.get(key)
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port)
        except OSError as exc:
            error = BrowserPolicyError(f"cannot resolve website host: {host}")
            if future is not None and not future.done():
                future.set_exception(error)
            raise error from exc
        addresses: list[str] = []
        for info in infos:
            raw = str(info[4][0])
            parsed = _parse_address(raw)
            if parsed is None or str(parsed) in addresses:
                continue
            if not _address_allowed(str(parsed)):
                error = BrowserPolicyError(
                    f"website host '{host}' resolves to a forbidden private or metadata address"
                )
                if future is not None and not future.done():
                    future.set_exception(error)
                return
            addresses.append(str(parsed))
        if not addresses:
            error = BrowserPolicyError(f"cannot resolve website host: {host}")
            if future is not None and not future.done():
                future.set_exception(error)
            return
        if len(self._entries) >= self.max_entries:
            for stale in list(self._entries)[: max(0, len(self._entries) - self.max_entries + 1)]:
                self._entries.pop(stale, None)
        self._entries[key] = (time.monotonic() + self.ttl_seconds, tuple(addresses))
        if future is not None and not future.done():
            future.set_result(tuple(addresses))


@dataclass(frozen=True, slots=True)
class UrlCheck:
    url: str
    host: str
    port: int
    resolved: tuple[str, ...]


class UrlGuard:
    def __init__(
        self,
        *,
        domain_policy: DomainPolicy | None = None,
        dns_cache: DnsCache | None = None,
    ) -> None:
        self.domain_policy = domain_policy or DomainPolicy()
        self.dns_cache = dns_cache or DnsCache()

    def _host_explicitly_allowed(self, host: str) -> bool:
        patterns = self.domain_policy.allowed_domains
        if not patterns:
            return False
        cleaned = host.rstrip(".").lower()
        return any(domain_matches(cleaned, pattern) for pattern in patterns)

    def check_sync(self, url: str) -> str:
        cleaned = check_url_syntax(url)
        parsed = urlparse(cleaned)
        host = parsed.hostname or ""
        self.domain_policy.check(host)
        cleaned_host = host.rstrip(".").lower()
        if (
            _parse_address(cleaned_host) is not None
            and str(_parse_address(cleaned_host)) in METADATA_ENDPOINTS
        ):
            raise BrowserPolicyError(
                "navigation to private, local, or cloud metadata addresses is not allowed"
            )
        metadata_host = cleaned_host in METADATA_HOST_PATTERNS
        if metadata_host and not self._host_explicitly_allowed(cleaned_host):
            raise BrowserPolicyError(
                "navigation to private, local, or cloud metadata addresses is not allowed"
            )
        if not self._host_explicitly_allowed(cleaned_host):
            check_hostname_literal(host)
        return cleaned

    async def check(self, url: str) -> UrlCheck:
        cleaned = self.check_sync(url)
        parsed = urlparse(cleaned)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if _parse_address(host) is not None:
            return UrlCheck(url=cleaned, host=host, port=port, resolved=(host,))
        if self._host_explicitly_allowed(host):
            return UrlCheck(url=cleaned, host=host, port=port, resolved=())
        resolved = await self.dns_cache.resolve(host, port)
        return UrlCheck(url=cleaned, host=host, port=port, resolved=resolved)


def check_url_syntax(url: str) -> str:
    cleaned = url.strip()
    if not cleaned or len(cleaned) > MAX_URL_LENGTH:
        raise BrowserPolicyError("URL must be 1 to 2048 characters")
    lowered = cleaned.lower()
    if lowered.startswith(FORBIDDEN_SCHEMES):
        raise BrowserPolicyError("this URL scheme is not allowed in the browser sandbox")
    if " " in cleaned.split("?")[0].split("#")[0] and not lowered.startswith("http"):
        raise BrowserPolicyError("URL is not valid")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserPolicyError("URL must use http or https")
    return cleaned


def guarded_url(url: str) -> str:
    cleaned = check_url_syntax(url)
    parsed = urlparse(cleaned)
    host = parsed.hostname or ""
    check_hostname_literal(host)
    return cleaned
