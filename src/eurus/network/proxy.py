from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from eurus.network.policy import (
    MAX_URL_LENGTH,
    NetworkPolicyEngine,
    NetworkPolicyError,
)
from eurus.security.redaction import redact_text

__all__ = ["EgressProxyServer", "ProxyLease"]

AuditSink = Callable[[str, dict[str, Any]], None]

HEAD_LIMIT_BYTES = 65536

CONNECT_TIMEOUT_SECONDS = 10.0

_CHUNK = 65536

_HOP_BY_HOP = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "proxy-authenticate",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True, slots=True)
class ProxyLease:
    token: str
    run_id: str
    host: str
    port: int
    expires_at: float

    @property
    def url(self) -> str:
        return f"http://eurus:{self.token}@{self.host}:{self.port}"

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(slots=True)
class _ClientState:
    run_id: str = ""
    responded: bool = False


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    return value


def _parse_head(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    text = raw.decode("iso-8859-1")
    lines = text.split("\r\n")
    request_line = lines[0].strip()
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        headers.append((name.strip().lower(), value.strip()))
    return request_line, headers


def _header(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key == name:
            return value
    return ""


class EgressProxyServer:
    def __init__(
        self,
        engine: NetworkPolicyEngine,
        *,
        bind_host: str = "127.0.0.1",
        port: int = 0,
        audit: AuditSink | None = None,
        lease_ttl_seconds: float = 3600.0,
        max_concurrency_per_lease: int = 8,
    ) -> None:
        self.engine = engine
        self.bind_host = bind_host
        self.requested_port = port
        self.audit = audit
        self.lease_ttl_seconds = lease_ttl_seconds
        self.max_concurrency_per_lease = max_concurrency_per_lease
        self._server: asyncio.Server | None = None
        self._leases: dict[str, ProxyLease] = {}
        self._tokens: dict[str, str] = {}
        self._slots: dict[str, asyncio.Semaphore] = {}

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            socket_address = self._server.sockets[0].getsockname()
            return int(socket_address[1])
        return self.requested_port

    def _emit(self, event: str, **fields: Any) -> None:
        if self.audit is None:
            return
        with contextlib.suppress(Exception):
            self.audit(event, {key: _redact_value(value) for key, value in fields.items()})

    def _prune(self, now: float) -> None:
        expired = [run_id for run_id, lease in self._leases.items() if lease.expired(now)]
        for run_id in expired:
            self.revoke(run_id)

    async def start(self) -> None:
        if self.running:
            return
        server = await asyncio.start_server(
            self._handle_client, self.bind_host, self.requested_port
        )
        self._server = server
        self._emit("network.proxy_started", host=self.bind_host, port=self.port)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        self._leases.clear()
        self._tokens.clear()
        self._slots.clear()
        if server is not None:
            server.close()
            await server.wait_closed()
            self._emit("network.proxy_stopped", host=self.bind_host, port=self.requested_port)

    async def ensure_started(self) -> None:
        if not self.running:
            await self.start()

    def issue_lease(self, run_id: str) -> ProxyLease:
        if not run_id or not run_id.strip():
            raise NetworkPolicyError("a proxy lease requires a run id")
        now = asyncio.get_running_loop().time()
        self._prune(now)
        token = secrets.token_urlsafe(24)
        lease = ProxyLease(
            token=token,
            run_id=run_id,
            host=self.bind_host,
            port=self.port,
            expires_at=now + self.lease_ttl_seconds,
        )
        self._leases[run_id] = lease
        self._tokens[token] = run_id
        self._slots[run_id] = asyncio.Semaphore(self.max_concurrency_per_lease)
        self._emit("network.lease_issued", run_id=run_id, port=lease.port)
        return lease

    def revoke(self, run_id: str) -> None:
        lease = self._leases.pop(run_id, None)
        self._slots.pop(run_id, None)
        if lease is not None:
            self._tokens.pop(lease.token, None)
            self._emit("network.lease_revoked", run_id=run_id)

    def lease_for(self, run_id: str) -> ProxyLease | None:
        return self._leases.get(run_id)

    def active_run_ids(self) -> tuple[str, ...]:
        now = asyncio.get_running_loop().time()
        self._prune(now)
        return tuple(self._leases)

    def _authorize(self, headers: list[tuple[str, str]]) -> str | None:
        now = asyncio.get_running_loop().time()
        self._prune(now)
        supplied = _header(headers, "proxy-authorization")
        if not supplied.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(supplied[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        _, sep, password = decoded.partition(":")
        if not sep:
            return None
        for token, run_id in list(self._tokens.items()):
            if hmac.compare_digest(password.encode("utf-8"), token.encode("utf-8")):
                lease = self._leases.get(run_id)
                if lease is not None and not lease.expired(now):
                    return run_id
                return None
        return None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        state = _ClientState()
        try:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
                OSError,
            ):
                return
            if len(head) > HEAD_LIMIT_BYTES:
                return
            request_line, headers = _parse_head(head)
            run_id = self._authorize(headers)
            state.run_id = run_id or ""
            if run_id is None:
                state.responded = True
                await self._reject(writer, 407, "proxy authentication required")
                self._emit("network.proxy_denied", reason="auth_failed")
                return
            method = request_line.split(" ", 1)[0].upper()
            async with self._slots[run_id]:
                if method == "CONNECT":
                    await self._handle_connect(request_line, reader, writer, state)
                else:
                    await self._handle_http(request_line, headers, reader, writer, state)
        except NetworkPolicyError as exc:
            self._emit("network.proxy_denied", reason=str(exc), run_id=state.run_id)
            if not state.responded:
                state.responded = True
                with contextlib.suppress(Exception):
                    await self._reject(writer, 403, str(exc)[:200])
        except Exception:
            self._emit("network.proxy_denied", reason="internal_error", run_id=state.run_id)
        finally:
            with contextlib.suppress(Exception):
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()

    async def _reject(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = b'{"error": "denied"}'
        lines = [
            f"HTTP/1.1 {status} {reason}".encode("iso-8859-1", errors="replace"),
            b"Content-Type: application/json",
            f"Content-Length: {len(body)}".encode(),
            b"Connection: close",
        ]
        if status == 407:
            lines.append(b'Proxy-Authenticate: Basic realm="eurus-egress"')
        writer.write(b"\r\n".join(lines) + b"\r\n\r\n" + body)
        await writer.drain()

    async def _handle_connect(
        self,
        request_line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        state: _ClientState,
    ) -> None:
        parts = request_line.split(" ")
        if len(parts) < 2 or ":" not in parts[1]:
            raise NetworkPolicyError("malformed CONNECT target")
        target = parts[1]
        host_text, _, port_text = target.rpartition(":")
        if not host_text or not port_text.isdigit():
            raise NetworkPolicyError("malformed CONNECT target")
        host = host_text.strip("[]")
        port = int(port_text)
        addresses = await self.engine.acheck_tunnel(host, port)
        self.engine.acquire(host)
        origin_reader, origin_writer = await self._connect_pinned(addresses, port)
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            state.responded = True
            bytes_out, bytes_in = await self._relay(reader, writer, origin_reader, origin_writer)
            self._emit(
                "network.proxy_tunnel",
                host=host,
                port=port,
                run_id=state.run_id,
                bytes_to_origin=bytes_out,
                bytes_from_origin=bytes_in,
                outcome="completed",
            )
        finally:
            with contextlib.suppress(Exception):
                origin_writer.close()

    async def _handle_http(
        self,
        request_line: str,
        headers: list[tuple[str, str]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        state: _ClientState,
    ) -> None:
        parts = request_line.split(" ")
        if len(parts) < 3:
            raise NetworkPolicyError("malformed request line")
        target = parts[1]
        if len(target) > MAX_URL_LENGTH:
            raise NetworkPolicyError("request target exceeds the URL length limit")
        parsed = urlparse(target)
        if parsed.scheme != "http" or not parsed.hostname:
            raise NetworkPolicyError("only absolute-form http requests are supported")
        decision = await self.engine.acheck_url(target)
        self.engine.acquire(decision.host)
        content_length_text = _header(headers, "content-length")
        body = b""
        if content_length_text:
            try:
                content_length = int(content_length_text)
            except ValueError as exc:
                raise NetworkPolicyError("invalid content-length") from exc
            if content_length < 0 or content_length > self.engine.policy.max_request_body_bytes:
                raise NetworkPolicyError("request body exceeds the allowed size")
            body = await reader.readexactly(content_length)
        origin_reader, origin_writer = await self._connect_pinned(decision.resolved, decision.port)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            forwarded = [f"{parts[0]} {path} HTTP/1.1".encode("iso-8859-1", errors="replace")]
            for name, value in headers:
                if name in _HOP_BY_HOP:
                    continue
                forwarded.append(f"{name}: {value}".encode("iso-8859-1", errors="replace"))
            forwarded.append(b"Connection: close")
            origin_writer.write(b"\r\n".join(forwarded) + b"\r\n\r\n")
            if body:
                origin_writer.write(body)
            await origin_writer.drain()
            status, response_head = await self._read_origin_head(origin_reader)
            writer.write(response_head)
            await writer.drain()
            state.responded = True
            bytes_in = len(response_head)
            bytes_in += await self._pump(
                origin_reader, writer, self.engine.policy.max_response_bytes
            )
            await writer.drain()
            self._emit(
                "network.proxy_request",
                method=parts[0],
                host=decision.host,
                port=decision.port,
                status=status,
                bytes_response=bytes_in,
                run_id=state.run_id,
            )
        finally:
            with contextlib.suppress(Exception):
                origin_writer.close()

    async def _connect_pinned(
        self, addresses: tuple[str, ...], port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: Exception | None = None
        for address in addresses[:4]:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout=CONNECT_TIMEOUT_SECONDS
                )
            except (OSError, TimeoutError) as exc:
                last_error = exc
        raise NetworkPolicyError(f"cannot connect to origin: {last_error}")

    async def _read_origin_head(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise NetworkPolicyError("origin returned an unreadable response") from exc
        if len(head) > HEAD_LIMIT_BYTES:
            raise NetworkPolicyError("origin response head exceeds the size limit")
        status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1])
        except (IndexError, ValueError):
            status = 0
        return status, head

    async def _pump(
        self, source: asyncio.StreamReader, target: asyncio.StreamWriter, limit: int
    ) -> int:
        total = 0
        while True:
            chunk = await source.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise NetworkPolicyError("response exceeds the configured byte budget")
            target.write(chunk)
            await target.drain()
        with contextlib.suppress(Exception):
            if not target.is_closing():
                target.write_eof()
        return total

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        origin_reader: asyncio.StreamReader,
        origin_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        limit = self.engine.policy.max_tunnel_bytes
        out_task = asyncio.create_task(self._pump(client_reader, origin_writer, limit))
        in_task = asyncio.create_task(self._pump(origin_reader, client_writer, limit))
        try:
            bytes_out, bytes_in = await asyncio.gather(out_task, in_task)
            return bytes_out, bytes_in
        except BaseException:
            for task in (out_task, in_task):
                task.cancel()
            await asyncio.gather(out_task, in_task, return_exceptions=True)
            raise
