from __future__ import annotations

import asyncio

import pytest

from eurus.errors import ToolError
from eurus.security.net_guard import (
    NetworkPolicyError,
    ValidatingBackend,
    normalize_redirect,
    public_http_client,
    validate_public_url,
)


class _StopMarker(Exception):
    pass


class _RefusingInner:
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise _StopMarker(host)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise _StopMarker(path)

    async def sleep(self, seconds: float) -> None:
        return None


def test_private_hosts_are_rejected_without_dns() -> None:
    for url in (
        "http://localhost/admin",
        "http://127.0.0.1:8080/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "http://172.16.0.1/x",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ):
        with pytest.raises(ToolError):
            validate_public_url(url)


class _StopMarker(Exception):
    pass


class _RefusingInner:
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise _StopMarker(host)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise _StopMarker(path)

    async def sleep(self, seconds: float) -> None:
        return None


def test_rebinding_resolver_blocks_private_addresses_at_connect_time() -> None:
    calls: list[str] = []

    def rebind(host: str, port: int) -> list[str]:
        calls.append(host)
        if len(calls) == 1:
            return ["93.184.216.34"]
        return ["10.0.0.5"]

    backend = ValidatingBackend(rebind)
    backend._inner = _RefusingInner()  # type: ignore[assignment]
    with pytest.raises(_StopMarker):
        asyncio.run(backend.connect_tcp("evil.example", 80, timeout=1.0))
    with pytest.raises(NetworkPolicyError):
        asyncio.run(backend.connect_tcp("evil.example", 80, timeout=1.0))
    assert len(calls) == 2


def test_backend_pins_validated_address_for_connection() -> None:
    connected: list[tuple[str, int]] = []

    class RecordingBackend(_RefusingInner):
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            connected.append((host, port))
            raise _StopMarker(host)

    backend = ValidatingBackend(lambda host, port: ["93.184.216.34", "93.184.216.35"])
    backend._inner = RecordingBackend()  # type: ignore[assignment]
    with pytest.raises(_StopMarker):
        asyncio.run(backend.connect_tcp("example.com", 443, timeout=1.0))
    assert connected == [("93.184.216.34", 443)]


def test_unix_sockets_are_blocked() -> None:
    backend = ValidatingBackend()
    with pytest.raises(ToolError):
        asyncio.run(backend.connect_unix_socket("/var/run/docker.sock"))


def test_redirect_to_private_destination_is_blocked() -> None:
    with pytest.raises(ToolError):
        normalize_redirect("https://example.com/page", "http://169.254.169.254/meta")
    with pytest.raises(ToolError):
        normalize_redirect("https://example.com/page", "//localhost:9000/secret")
    assert normalize_redirect("https://example.com/page", "/other") == "https://example.com/other"


def test_public_client_uses_validating_transport() -> None:
    client = public_http_client()
    try:
        assert isinstance(client._transport, type(client._transport))
        pool = getattr(client._transport, "_pool", None)
        assert pool is not None
        assert isinstance(getattr(pool, "_network_backend", None), ValidatingBackend)
    finally:
        asyncio.run(client.aclose())


def test_cgnat_range_is_forbidden() -> None:
    import ipaddress

    from eurus.security.net_guard import _forbidden

    for raw in ("100.64.0.1", "100.100.100.200", "100.127.255.254"):
        with pytest.raises(ToolError):
            validate_public_url(f"http://{raw}/x")
        assert _forbidden(ipaddress.ip_address(raw)) is True
    assert _forbidden(ipaddress.ip_address("8.8.8.8")) is False


def test_ipv4_mapped_ipv6_is_unwrapped_before_checking() -> None:
    import ipaddress

    from eurus.security.net_guard import _forbidden, _parse_address

    assert _forbidden(ipaddress.ip_address("::ffff:127.0.0.1")) is True
    assert _forbidden(ipaddress.ip_address("::ffff:10.0.0.5")) is True
    assert _forbidden(ipaddress.ip_address("::ffff:100.64.0.1")) is True
    assert _forbidden(ipaddress.ip_address("::ffff:8.8.8.8")) is False
    assert _parse_address("::ffff:127.0.0.1") == ipaddress.ip_address("127.0.0.1")
    assert _parse_address("8.8.8.8") == ipaddress.ip_address("8.8.8.8")


def test_backend_pins_unwrapped_mapped_address() -> None:
    connected: list[tuple[str, int]] = []

    class RecordingBackend(_RefusingInner):
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            connected.append((host, port))
            raise _StopMarker(host)

    backend = ValidatingBackend(lambda host, port: ["::ffff:93.184.216.34"])
    backend._inner = RecordingBackend()  # type: ignore[assignment]
    with pytest.raises(_StopMarker):
        asyncio.run(backend.connect_tcp("example.com", 443, timeout=1.0))
    assert connected == [("93.184.216.34", 443)]


def test_backend_rejects_mapped_private_resolution() -> None:
    backend = ValidatingBackend(lambda host, port: ["::ffff:127.0.0.1"])
    backend._inner = _RefusingInner()  # type: ignore[assignment]
    with pytest.raises(NetworkPolicyError):
        asyncio.run(backend.connect_tcp("evil.example", 80, timeout=1.0))


def test_httpcore_backend_seam_version_guard() -> None:
    """Version guard: fails with upgrade instructions when httpcore moves the seam.

    DNS-rebinding pinning needs connect-time IP selection with original-host
    SNI, which httpx/httpcore only offer through the private network-backend
    protocol. The single adapter is eurus.security.http_adapter; if this test
    fails, re-implement ValidatingBackend there against the new internals.
    """
    from eurus.security import http_adapter

    assert http_adapter.HTTPCORE_BACKEND_AVAILABLE, (
        f"httpcore no longer exposes {http_adapter.HTTPCORE_BACKEND_SEAM}; "
        "re-implement eurus.security.http_adapter.ValidatingBackend against the "
        f"current httpcore internals. Import error: {http_adapter.SEAM_IMPORT_ERROR}"
    )
    backend = http_adapter.ValidatingBackend()
    assert backend._inner is not None
