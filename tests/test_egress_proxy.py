from __future__ import annotations

import asyncio
import base64

import pytest

from eurus.execution.envelope import NetworkProfile
from eurus.network.policy import EgressPolicy, NetworkPolicyEngine
from eurus.network.proxy import EgressProxyServer, ProxyLease

BODY = b"hello-from-origin"


class Origin:
    def __init__(self, *, body: bytes = BODY) -> None:
        self.requests: list[dict[str, str]] = []
        self.server: asyncio.Server | None = None
        self.port = 0
        self.body = body

    async def start(self, *, mode: str = "http") -> None:
        self.server = await asyncio.start_server(
            lambda r, w: self._handle(r, w, mode), "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, mode: str
    ) -> None:
        try:
            if mode == "http":
                head = await reader.readuntil(b"\r\n\r\n")
                text = head.decode("iso-8859-1")
                lines = text.split("\r\n")
                self.requests.append(
                    {
                        "request_line": lines[0],
                        "headers": "\n".join(lines[1:]),
                    }
                )
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    + f"Content-Length: {len(self.body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + self.body
                )
                await writer.drain()
            else:
                data = await reader.read(4096)
                writer.write(data)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()


def static_resolver(address: str):
    def resolver(host: str, port: int) -> list[str]:
        return [address]

    return resolver


def make_engine(
    origin_port: int,
    *,
    profile: NetworkProfile = NetworkProfile.WEB_READ,
    allow_private: bool = True,
    **policy_kwargs,
) -> NetworkPolicyEngine:
    policy = EgressPolicy(
        profile=profile,
        allowed_ports=frozenset({80, 443, origin_port}),
        allow_private_addresses=allow_private,
        **policy_kwargs,
    )
    return NetworkPolicyEngine(policy, resolver=static_resolver("127.0.0.1"))


def proxy_headers(lease: ProxyLease) -> str:
    userinfo = lease.url.split("//", 1)[1].split("@", 1)[0]
    token = userinfo.split(":", 1)[1]
    encoded = base64.b64encode(f"eurus:{token}".encode()).decode()
    return f"Proxy-Authorization: Basic {encoded}\r\n"


class ProxyFixture:
    def __init__(self, server: EgressProxyServer) -> None:
        self.server = server
        self.events: list[tuple[str, dict[str, object]]] = []
        server.audit = self._record

    def _record(self, event: str, fields: dict[str, object]) -> None:
        self.events.append((event, fields))

    @property
    def address(self) -> tuple[str, int]:
        return ("127.0.0.1", self.server.port)

    def lease(self, run_id: str = "run-1") -> ProxyLease:
        return self.server.issue_lease(run_id)

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(*self.address)


@pytest.fixture
async def proxy_factory():
    servers: list[EgressProxyServer] = []

    async def build(engine: NetworkPolicyEngine, **kwargs) -> ProxyFixture:
        server = EgressProxyServer(engine, bind_host="127.0.0.1", port=0, **kwargs)
        fixture = ProxyFixture(server)
        servers.append(server)
        await server.start()
        return fixture

    yield build

    for server in servers:
        await server.stop()


@pytest.fixture
async def origin_factory():
    origins: list[Origin] = []

    async def build(mode: str = "http") -> Origin:
        origin = Origin()
        origins.append(origin)
        await origin.start(mode=mode)
        return origin

    yield build

    for origin in origins:
        await origin.stop()


class TestConnectTunnel:
    async def test_tunnel_relays_bytes_and_audits(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-tunnel")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        reply = await reader.readexactly(len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert reply.startswith(b"HTTP/1.1 200")
        writer.write(b"ping-through-tunnel")
        await writer.drain()
        writer.write_eof()
        echoed = await reader.read(-1)
        assert echoed == b"ping-through-tunnel"
        writer.close()
        kinds = [event for event, _fields in fixture.events]
        assert "network.proxy_started" in kinds
        assert "network.lease_issued" in kinds
        tunnel = [fields for event, fields in fixture.events if event == "network.proxy_tunnel"]
        assert tunnel and tunnel[0]["bytes_to_origin"] == len(b"ping-through-tunnel")
        assert tunnel[0]["run_id"] == "run-tunnel"

    async def test_tunnel_via_hostname_uses_pinned_dns(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        engine = make_engine(origin.port)
        engine._resolver = static_resolver("127.0.0.1")
        fixture = await proxy_factory(engine)
        lease = fixture.lease("run-dns")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT origin.test:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        reply = await reader.readexactly(len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert reply.startswith(b"HTTP/1.1 200")
        writer.close()

    async def test_missing_auth_gets_407(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port))
        fixture.lease("run-auth")
        reader, writer = await fixture.open()
        writer.write(f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 407")
        assert b"Proxy-Authenticate" in response
        writer.close()

    async def test_wrong_token_gets_407(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-wrong")
        reader, writer = await fixture.open()
        forged = (
            "Proxy-Authorization: Basic "
            + base64.b64encode(f"eurus:{lease.token[:-3]}abc".encode()).decode()
            + "\r\n"
        )
        writer.write(f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{forged}\r\n".encode())
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 407")
        writer.close()

    async def test_revoked_lease_gets_407(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-revoked")
        fixture.server.revoke("run-revoked")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 407")
        writer.close()

    async def test_expired_lease_gets_407(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port), lease_ttl_seconds=0.05)
        lease = fixture.lease("run-expired")
        await asyncio.sleep(0.08)
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 407")
        writer.close()

    async def test_private_target_denied_when_policy_strict(
        self, proxy_factory, origin_factory
    ) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port, allow_private=False))
        lease = fixture.lease("run-strict")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()

    async def test_offline_profile_denies_tunnel(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port, profile=NetworkProfile.OFFLINE))
        lease = fixture.lease("run-offline")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()

    async def test_disallowed_port_denied(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        engine_policy = EgressPolicy(
            profile=NetworkProfile.WEB_READ,
            allowed_ports=frozenset({443}),
            allow_private_addresses=True,
        )
        fixture = await proxy_factory(
            NetworkPolicyEngine(engine_policy, resolver=static_resolver("127.0.0.1"))
        )
        lease = fixture.lease("run-port")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()


class TestHttpForward:
    async def test_forward_rewrites_to_origin_form_and_strips_proxy_auth(
        self, proxy_factory, origin_factory
    ) -> None:
        origin = await origin_factory(mode="http")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-http")
        reader, writer = await fixture.open()
        request = (
            f"GET http://127.0.0.1:{origin.port}/some/path?q=1 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin.port}\r\n"
            f"{proxy_headers(lease)}"
            f"X-Custom: yes\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        response = await reader.read(-1)
        assert BODY in response
        writer.close()
        assert origin.requests, "origin saw no request"
        seen = origin.requests[0]
        assert seen["request_line"].startswith("GET /some/path?q=1 HTTP/1.1")
        assert "proxy-authorization" not in seen["headers"].lower()
        assert "x-custom: yes" in seen["headers"].lower()
        forwarded = [fields for event, fields in fixture.events if event == "network.proxy_request"]
        assert forwarded and forwarded[0]["status"] == 200
        assert forwarded[0]["host"] == "127.0.0.1"

    async def test_https_absolute_form_rejected(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="http")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-https")
        reader, writer = await fixture.open()
        request = f"GET https://127.0.0.1:{origin.port}/x HTTP/1.1\r\n{proxy_headers(lease)}\r\n"
        writer.write(request.encode())
        await writer.drain()
        response = await reader.read(-1)
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()

    async def test_response_byte_cap_truncates(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="http")
        origin.body = b"x" * 65536
        fixture = await proxy_factory(make_engine(origin.port, max_response_bytes=2048))
        lease = fixture.lease("run-cap")
        reader, writer = await fixture.open()
        request = f"GET http://127.0.0.1:{origin.port}/big HTTP/1.1\r\n{proxy_headers(lease)}\r\n"
        writer.write(request.encode())
        await writer.drain()
        received = await reader.read(-1)
        assert received.startswith(b"HTTP/1.1 200")
        assert len(received) < 65536
        writer.close()
        denied = [fields for event, fields in fixture.events if event == "network.proxy_denied"]
        assert denied and "byte budget" in str(denied[0]["reason"])

    async def test_rate_limit_blocks_second_request(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="http")
        fixture = await proxy_factory(make_engine(origin.port, requests_per_minute=1))
        lease = fixture.lease("run-rate")
        for index in range(2):
            reader, writer = await fixture.open()
            request = (
                f"GET http://127.0.0.1:{origin.port}/r{index} HTTP/1.1\r\n"
                f"{proxy_headers(lease)}\r\n"
            )
            writer.write(request.encode())
            await writer.drain()
            response = await reader.read(-1)
            if index == 0:
                assert response.startswith(b"HTTP/1.1 200")
            else:
                assert response.startswith(b"HTTP/1.1 403")
            writer.close()


class TestLeasesAndAudit:
    async def test_lease_shape_and_expiry(self, proxy_factory) -> None:
        engine = make_engine(443)
        fixture = await proxy_factory(engine, lease_ttl_seconds=3600.0)
        lease = fixture.lease("run-shape")
        assert lease.url.startswith("http://eurus:")
        assert lease.url.endswith("@127.0.0.1:" + str(lease.port))
        assert not lease.expired(asyncio.get_running_loop().time())
        assert lease.expired(lease.expires_at + 1)
        assert fixture.server.lease_for("run-shape") is lease
        assert fixture.server.active_run_ids() == ("run-shape",)

    async def test_issue_lease_requires_run_id(self, proxy_factory) -> None:
        fixture = await proxy_factory(make_engine(443))
        with pytest.raises(Exception, match="run id"):
            fixture.lease("")

    async def test_audit_events_never_contain_token(self, proxy_factory, origin_factory) -> None:
        origin = await origin_factory(mode="echo")
        fixture = await proxy_factory(make_engine(origin.port))
        lease = fixture.lease("run-redact")
        reader, writer = await fixture.open()
        writer.write(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n{proxy_headers(lease)}\r\n".encode()
        )
        await writer.drain()
        await reader.readexactly(len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        writer.write_eof()
        await reader.read(-1)
        writer.close()
        denied_reader, denied_writer = await fixture.open()
        denied_writer.write(f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n\r\n".encode())
        await denied_writer.drain()
        await denied_reader.read(-1)
        denied_writer.close()
        dumped = repr(fixture.events)
        assert lease.token not in dumped
        assert "Proxy-Authorization" not in dumped
        denied = [fields for event, fields in fixture.events if event == "network.proxy_denied"]
        assert denied and denied[0]["reason"] == "auth_failed"

    async def test_redaction_applies_to_string_fields(self, proxy_factory) -> None:
        from eurus.network.proxy import _redact_value

        assert _redact_value("bearer sk-abcdefghijklmnop1234") != "bearer sk-abcdefghijklmnop1234"
        assert _redact_value(42) == 42
