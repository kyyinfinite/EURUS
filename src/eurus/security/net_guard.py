from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from eurus.errors import ToolError

MAX_REDIRECTS = 6
MAX_CONNECT_ATTEMPTS = 4
DEFAULT_TIMEOUT = 15.0
Resolver = Callable[[str, int], list[str]]
SocketOption = Any


class NetworkPolicyError(ToolError):
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


def validate_public_hostname(host: str, port: int) -> list[str]:
    cleaned = host.rstrip(".").lower()
    if cleaned in {"localhost", "localhost.localdomain"} or cleaned.endswith(".localhost"):
        raise NetworkPolicyError("local and private network URLs are not allowed")
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    else:
        if _forbidden(address):
            raise NetworkPolicyError("local and private network URLs are not allowed")
        return [cleaned]
    try:
        infos = socket.getaddrinfo(cleaned, port)
    except OSError as exc:
        raise NetworkPolicyError(f"cannot resolve website host: {cleaned}") from exc
    resolved: list[str] = []
    for info in infos:
        resolved_address = _parse_address(str(info[4][0]))
        if resolved_address is None:
            continue
        if _forbidden(resolved_address):
            raise NetworkPolicyError(
                "website host resolves to a forbidden private or local address"
            )
        if str(resolved_address) not in resolved:
            resolved.append(str(resolved_address))
    if not resolved:
        raise NetworkPolicyError(f"cannot resolve website host: {cleaned}")
    return resolved


def validate_public_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkPolicyError("URL must use http or https")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    validate_public_hostname(parsed.hostname, port)
    return cleaned


def normalize_redirect(current_url: str, location: str) -> str:
    target = urljoin(current_url, location)
    return validate_public_url(target)


async def avalidate_public_url(url: str) -> str:
    return await asyncio.to_thread(validate_public_url, url)


async def anormalize_redirect(current_url: str, location: str) -> str:
    return await asyncio.to_thread(normalize_redirect, current_url, location)


class ValidatingBackend(AsyncNetworkBackend):
    def __init__(self, resolver: Resolver = validate_public_hostname) -> None:
        self._inner: AsyncNetworkBackend = AutoBackend()
        self._resolver = resolver

    def _validated_addresses(self, host: str, port: int) -> list[str]:
        resolved = self._resolver(host, port)
        validated: list[str] = []
        for raw in resolved:
            address = _parse_address(str(raw))
            if address is None:
                raise NetworkPolicyError(f"resolver returned a non-address for {host!r}")
            if _forbidden(address):
                raise NetworkPolicyError(
                    "website host resolves to a forbidden private or local address"
                )
            if str(address) not in validated:
                validated.append(str(address))
        if not validated:
            raise NetworkPolicyError(f"cannot resolve website host: {host}")
        return validated

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> AsyncNetworkStream:
        addresses = await asyncio.to_thread(self._validated_addresses, host, port)
        last_error: Exception | None = None
        for pinned in addresses[:MAX_CONNECT_ATTEMPTS]:
            try:
                return await self._inner.connect_tcp(
                    pinned,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> AsyncNetworkStream:
        raise NetworkPolicyError("unix socket connections are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class ValidatingTransport(httpx.AsyncHTTPTransport):
    def __init__(self, resolver: Resolver = validate_public_hostname, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        original = getattr(self, "_pool", None)
        if original is None:
            raise RuntimeError("httpx transport internals changed; cannot install validator")
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=getattr(original, "_ssl_context", None),
            max_connections=getattr(original, "_max_connections", None),
            max_keepalive_connections=getattr(original, "_max_keepalive_connections", None),
            keepalive_expiry=getattr(original, "_keepalive_expiry", None),
            http1=getattr(original, "_http1", True),
            http2=getattr(original, "_http2", False),
            retries=getattr(original, "_retries", 0),
            local_address=getattr(original, "_local_address", None),
            uds=getattr(original, "_uds", None),
            network_backend=ValidatingBackend(resolver),
            socket_options=getattr(original, "_socket_options", None),
        )


def public_http_client(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    resolver: Resolver = validate_public_hostname,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
        transport=ValidatingTransport(resolver),
    )
