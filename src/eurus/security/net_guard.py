from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx

from eurus.errors import ToolError
from eurus.security.http_adapter import ValidatingBackend, ValidatingTransport

__all__ = [
    "NetworkPolicyError",
    "ValidatingBackend",
    "ValidatingTransport",
    "avalidate_public_url",
    "anormalize_redirect",
    "normalize_redirect",
    "public_http_client",
    "validate_public_hostname",
    "validate_public_url",
]

MAX_REDIRECTS = 6
MAX_CONNECT_ATTEMPTS = 4
DEFAULT_TIMEOUT = 15.0
Resolver = Callable[[str, int], list[str]]


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
