from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx

Resolver = Callable[[str, int], list[str]]
SocketOption = Any

HTTPCORE_BACKEND_SEAM = "httpcore._backends (network backend protocol)"

try:
    from httpcore._backends.auto import AutoBackend
    from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

    HTTPCORE_BACKEND_AVAILABLE = True
    SEAM_IMPORT_ERROR: str | None = None
except ImportError as exc:
    HTTPCORE_BACKEND_AVAILABLE = False
    SEAM_IMPORT_ERROR = str(exc)
    AutoBackend = None
    AsyncNetworkBackend = object
    AsyncNetworkStream = Any

__all__ = [
    "HTTPCORE_BACKEND_AVAILABLE",
    "HTTPCORE_BACKEND_SEAM",
    "SEAM_IMPORT_ERROR",
    "ValidatingBackend",
    "ValidatingTransport",
    "require_backend_seam",
]


def require_backend_seam() -> None:
    if not HTTPCORE_BACKEND_AVAILABLE:
        raise RuntimeError(
            "httpcore removed the private network-backend API "
            f"({HTTPCORE_BACKEND_SEAM}); DNS-rebinding pinning needs a new "
            "adapter. Re-implement ValidatingBackend against the current "
            f"httpcore internals. Import error: {SEAM_IMPORT_ERROR}"
        )


class ValidatingBackend(AsyncNetworkBackend):
    def __init__(
        self,
        resolver: Resolver | None = None,
        inner: Any | None = None,
        *,
        strict_public: bool = True,
    ) -> None:
        if not HTTPCORE_BACKEND_AVAILABLE:
            require_backend_seam()
        self._resolver = resolver
        self._inner = inner if inner is not None else AutoBackend()
        self._strict_public = strict_public

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
            if self._strict_public and _forbidden(address):
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
    def __init__(self, resolver: Resolver, *, strict_public: bool = True, **kwargs: Any) -> None:
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
            network_backend=ValidatingBackend(resolver, strict_public=strict_public),
            socket_options=getattr(original, "_socket_options", None),
        )
