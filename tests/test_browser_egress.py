from __future__ import annotations

import asyncio
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from eurus.browser.egress import EgressProxy
from eurus.browser.manager import BrowserManager, BrowserSettings
from eurus.browser.security import (
    BLOCKED_HOSTS,
    BrowserPolicyError,
    DnsCache,
    DnsResolutionError,
    DomainPolicy,
    UrlGuard,
)
from eurus.errors import ToolError

pytest.importorskip("playwright")

PLAYWRIGHT_CHROMIUM = Path.home() / ".cache" / "ms-playwright"
SYSTEM_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or ""


def chromium_available() -> bool:
    if SYSTEM_CHROMIUM:
        return True
    if PLAYWRIGHT_CHROMIUM.is_dir():
        for child in PLAYWRIGHT_CHROMIUM.iterdir():
            if child.name.startswith("chromium"):
                return True
    return False


class _Recorder:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.index = 0

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        if self.index >= len(self.outcomes):
            raise AssertionError("unexpected resolver call")
        outcome = self.outcomes[self.index]
        self.index += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _start_echo_server() -> tuple[int, asyncio.AbstractServer]:
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                data = await reader.read(65536)
            except OSError:
                break
            if not data:
                break
            try:
                writer.write(data)
                await writer.drain()
            except OSError:
                break
        writer.close()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return port, server


class _HttpUpstreamHandler(BaseHTTPRequestHandler):
    received: list[bytes] = []

    def do_GET(self) -> None:
        type(self).received.append(self.requestline.encode() + b"\n" + str(self.headers).encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        body = b"upstream-ok"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


async def _client_connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def _read_response_head(reader: asyncio.StreamReader) -> bytes:
    data = await reader.readuntil(b"\r\n\r\n")
    return data


@pytest.mark.asyncio
async def test_tunnel_connects_to_validated_address():
    echo_port, server = await _start_echo_server()
    recorder = _Recorder([("127.0.0.1",)])
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        reader, writer = await _client_connect(proxy.port)
        writer.write(
            f"CONNECT target.example:{echo_port} HTTP/1.1\r\nHost: target.example\r\n\r\n".encode()
        )
        await writer.drain()
        head = await _read_response_head(reader)
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping-through-tunnel")
        await writer.drain()
        echoed = await reader.readexactly(len(b"ping-through-tunnel"))
        assert echoed == b"ping-through-tunnel"
        writer.close()
        await asyncio.sleep(0.05)
        assert proxy.connections == 1
        assert proxy.refusals == 0
    finally:
        await proxy.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_policy_refusal_closes_tunnel():
    recorder = _Recorder([BrowserPolicyError("resolves to a forbidden address")])
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        reader, writer = await _client_connect(proxy.port)
        writer.write(b"CONNECT metadata.example:443 HTTP/1.1\r\nHost: metadata.example\r\n\r\n")
        await writer.drain()
        head = await _read_response_head(reader)
        assert head.startswith(b"HTTP/1.1 403")
        body = await reader.read()
        assert b"browser policy" in body
        await asyncio.sleep(0.05)
        assert proxy.refusals == 1
        assert proxy.connections == 0
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_dns_failure_reports_bad_gateway():
    recorder = _Recorder([DnsResolutionError("cannot resolve website host: rebinding.test")])
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        reader, writer = await _client_connect(proxy.port)
        writer.write(b"CONNECT rebinding.test:443 HTTP/1.1\r\nHost: rebinding.test\r\n\r\n")
        await writer.drain()
        head = await _read_response_head(reader)
        assert head.startswith(b"HTTP/1.1 502")
        await asyncio.sleep(0.05)
        assert proxy.failures == 1
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_upstream_unreachable_reports_bad_gateway():
    recorder = _Recorder([("127.0.0.2",)])
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        reader, writer = await _client_connect(proxy.port)
        writer.write(b"CONNECT silent.example:443 HTTP/1.1\r\nHost: silent.example\r\n\r\n")
        await writer.drain()
        head = await _read_response_head(reader)
        assert head.startswith(b"HTTP/1.1 502")
        await asyncio.sleep(0.05)
        assert proxy.failures == 1
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_rebinding_is_refused_on_the_next_connection():
    echo_port, server = await _start_echo_server()
    recorder = _Recorder(
        [
            ("127.0.0.1",),
            BrowserPolicyError("rebound to a forbidden private address"),
        ]
    )
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        first_reader, first_writer = await _client_connect(proxy.port)
        first_writer.write(
            f"CONNECT target.example:{echo_port} HTTP/1.1\r\nHost: target.example\r\n\r\n".encode()
        )
        await first_writer.drain()
        first_head = await _read_response_head(first_reader)
        assert first_head.startswith(b"HTTP/1.1 200")
        first_writer.close()

        second_reader, second_writer = await _client_connect(proxy.port)
        second_writer.write(
            f"CONNECT target.example:{echo_port} HTTP/1.1\r\nHost: target.example\r\n\r\n".encode()
        )
        await second_writer.drain()
        second_head = await _read_response_head(second_reader)
        assert second_head.startswith(b"HTTP/1.1 403")
        await asyncio.sleep(0.05)
        assert proxy.connections == 1
        assert proxy.refusals == 1
    finally:
        await proxy.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_plain_http_request_is_forwarded_with_origin_form():
    handler_server = HTTPServer(("127.0.0.1", 0), _HttpUpstreamHandler)
    upstream_port = int(handler_server.server_address[1])
    serving = asyncio.get_running_loop().run_in_executor(None, handler_server.serve_forever)
    recorder = _Recorder([("127.0.0.1",)])
    proxy = EgressProxy(recorder)
    try:
        await proxy.start()
        reader, writer = await _client_connect(proxy.port)
        request = (
            f"GET http://plain.example:{upstream_port}/upstream?x=1 HTTP/1.1\r\n".encode()
            + b"Host: plain.example\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"User-Agent: egress-test\r\n"
            b"\r\n"
        )
        writer.write(request)
        await writer.drain()
        head = await _read_response_head(reader)
        assert b"200 OK" in head
        body = await reader.readexactly(len(b"upstream-ok"))
        assert body == b"upstream-ok"
        writer.close()
        await asyncio.sleep(0.05)
        assert proxy.connections == 1
        seen = b"".join(_HttpUpstreamHandler.received)
        assert b"GET /upstream?x=1 HTTP/1.1" in seen
        assert b"Proxy-Connection" not in seen
        assert b"Connection: close" in seen
    finally:
        await proxy.stop()
        handler_server.shutdown()
        handler_server.server_close()
        await asyncio.wait_for(serving, timeout=5.0)
        _HttpUpstreamHandler.received.clear()


@pytest.mark.asyncio
async def test_stop_cancels_active_tunnels():
    echo_port, server = await _start_echo_server()
    recorder = _Recorder([("127.0.0.1",)])
    proxy = EgressProxy(recorder)
    await proxy.start()
    reader, writer = await _client_connect(proxy.port)
    writer.write(
        f"CONNECT idle.example:{echo_port} HTTP/1.1\r\nHost: idle.example\r\n\r\n".encode()
    )
    await writer.drain()
    head = await _read_response_head(reader)
    assert head.startswith(b"HTTP/1.1 200")
    await proxy.stop()
    await asyncio.sleep(0.1)
    with pytest.raises((asyncio.IncompleteReadError, OSError)):
        await reader.readexactly(16)
    server.close()
    await server.wait_closed()

    with pytest.raises(ToolError):
        await proxy.start()


def _real_browser_settings(**overrides: object) -> BrowserSettings:
    base: dict[str, object] = dict(
        enabled=True,
        backend="playwright",
        headless=True,
        allowed_domains=("127.0.0.1",),
        allow_private_addresses=True,
        navigation_timeout=15.0,
        interaction_timeout=8.0,
        startup_timeout=30.0,
        profile_dir="",
        download_dir="",
        max_download_mb=20,
    )
    base.update(overrides)
    return BrowserSettings(**base)


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<html><body><h1>EGRESS-OK</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.mark.asyncio
async def test_real_chromium_traffic_flows_through_proxy():
    if not chromium_available():
        pytest.skip("no chromium available for egress verification")
    handler_server = HTTPServer(("127.0.0.1", 0), _PageHandler)
    port = int(handler_server.server_address[1])
    serving = asyncio.get_running_loop().run_in_executor(None, handler_server.serve_forever)
    manager = BrowserManager(_real_browser_settings())
    try:
        session = await manager.session_for("egress-ok")
        async with session.lock:
            await session.navigate(f"http://127.0.0.1:{port}/redirect")
            text = await session.page_text(500)
        assert "EGRESS-OK" in text
        assert manager.egress_proxy.connections >= 1
        assert manager.egress_proxy.refusals == 0
    finally:
        await manager.close_all()
        handler_server.shutdown()
        handler_server.server_close()
        await asyncio.wait_for(serving, timeout=5.0)


@pytest.mark.asyncio
async def test_real_chromium_rebinding_is_blocked_by_proxy():
    if not chromium_available():
        pytest.skip("no chromium available for egress verification")
    handler_server = HTTPServer(("127.0.0.1", 0), _PageHandler)
    port = int(handler_server.server_address[1])
    serving = asyncio.get_running_loop().run_in_executor(None, handler_server.serve_forever)
    manager = BrowserManager(_real_browser_settings(navigation_timeout=10.0))
    calls: list[int] = []

    async def rebound(host: str, query_port: int) -> tuple[str, ...]:
        calls.append(query_port)
        if len(calls) <= 1:
            return ("127.0.0.1",)
        raise BrowserPolicyError("rebound to a forbidden private address")

    manager.dns_cache.resolve = rebound
    try:
        session = await manager.session_for("egress-rebind")
        async with session.lock:
            first = await session.navigate(f"http://127.0.0.1:{port}/")
            second = await session.navigate(f"http://127.0.0.1:{port}/redirect")
            text = await session.page_text(1000)
        assert first["status"] == 200
        assert second["status"] == 403
        assert "rejected by browser policy" in text
        assert manager.egress_proxy.refusals >= 1
        assert manager.egress_proxy.connections >= 1
    finally:
        await manager.close_all()
        handler_server.shutdown()
        handler_server.server_close()
        await asyncio.wait_for(serving, timeout=5.0)


@pytest.mark.asyncio
async def test_guard_blocks_metadata_hostname_before_navigation():
    guard = UrlGuard(domain_policy=DomainPolicy(), dns_cache=DnsCache())
    for host in ("169.254.169.254", "metadata.google.internal", "localhost"):
        with pytest.raises(BrowserPolicyError):
            await guard.check(f"http://{host}/")
    assert "localhost" in BLOCKED_HOSTS
