"""Single adapter over httpcore's network-backend extension point.

DNS-rebinding protection must connect to a validated IP while the TLS
handshake still uses the original hostname (SNI/Host). httpx and httpcore do
not expose a public connect-time IP-pinning API: the URL would have to carry
the pinned IP, which changes the SNI and breaks certificate verification, and
no public httpx transport hook runs between DNS resolution and TCP connect.

The only supported seam is httpcore's network-backend protocol, currently
reachable via ``httpcore._backends``. Those modules are private, so every
private-API touchpoint lives in this file. ``httpcore_backend_available``
reports whether the seam still exists for the installed httpcore version;
``tests/test_httpx_hardening.py::test_httpcore_backend_seam_version_guard``
fails with upgrade instructions when upstream moves it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx

Resolver = Callable[[str, int], list[str]]
SocketOption = Any

HTTPCORE_BACKEND_SEAM = "httpcore._backends (network backend protocol)"

try:  # pragma: no cover - the guard test documents the failure path
    from httpcore._backends.auto import AutoBackend
    from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

    HTTPCORE_BACKEND_AVAILABLE = True
    SEAM_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover
    HTTPCORE_BACKEND_AVAILABLE = False
    SEAM_IMPORT_ERROR = str(exc)
    AutoBackend = None  # type: ignore[assignment,misc]
    AsyncNetworkBackend = object  # type: ignore[assignment,misc]
    AsyncNetworkStream = Any  # type: ignore[misc,assignment]

__all__ = [
    "HTTPCORE_BACKEND_AVAILABLE",
    "HTTPCORE_BACKEND_SEAM",
    "SEAM_IMPORT_ERROR",
    "ValidatingBackend",
    "ValidatingTransport",
    "require_backend_seam",
]


def require_backend_seam() -> None:
    """Raise a precise error when httpcore no longer exposes the backend seam."""
    if not HTTPCORE_BACKEND_AVAILABLE:
        raise RuntimeError(
            "httpcore removed the private network-backend API "
            f"({HTTPCORE_BACKEND_SEAM}); DNS-rebinding pinning needs a new "
            "adapter. Re-implement ValidatingBackend against the current "
            f"httpcore internals. Import error: {SEAM_IMPORT_ERROR}"
        )


class ValidatingBackend(AsyncNetworkBackend):
    """Connects to validated, pinned IPs; forbids unix sockets."""

    def __init__(self, resolver: Resolver | None = None, inner: Any | None = None) -> None:
        if not HTTPCORE_BACKEND_AVAILABLE:
            require_backend_seam()
        self._resolver = resolver
        self._inner = inner if inner is not None else AutoBackend()

    def validated_addresses(self, host: str, port: int) -> list[str]:
        from eurus.security.net_guard import NetworkPolicyError, _forbidden, _parse_address

        resolver = self._resolver
        if resolver is None:
            from eurus.security.net_guard import validate_public_hostname

            resolver = validate_public_hostname
        resolved = resolver(host, port)
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
    ) -> Any:
        import asyncio

        addresses = await asyncio.to_thread(self.validated_addresses, host, port)
        last_error: Exception | None = None
        for pinned in addresses[:4]:
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
    ) -> Any:
        from eurus.security.net_guard import NetworkPolicyError

        raise NetworkPolicyError("unix socket connections are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class ValidatingTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool pins validated DNS results."""

    def __init__(self, resolver: Resolver, **kwargs: Any) -> None:
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
