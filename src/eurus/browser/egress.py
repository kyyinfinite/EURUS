from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Sequence
from urllib.parse import urlsplit

from eurus.browser.security import BrowserPolicyError, DnsCache, DnsResolutionError
from eurus.errors import ToolError

MAX_REQUEST_LINE_BYTES = 16384
MAX_HEADER_BYTES = 65536
MAX_BODY_CHUNK = 262144
MAX_DECLARED_BODY_BYTES = 1073741824
HOP_BY_HOP_REQUEST_HEADERS = frozenset(
    {"proxy-connection", "proxy-authorization", "proxy-authenticate", "keep-alive"}
)
INVALID_PORT = -1


class EgressProxy:
    def __init__(
        self,
        resolver: DnsCache,
        *,
        connect_timeout: float = 10.0,
        header_timeout: float = 15.0,
        listen_host: str = "127.0.0.1",
    ) -> None:
        self._resolver = resolver
        self._connect_timeout = connect_timeout
        self._header_timeout = header_timeout
        self._listen_host = listen_host
        self._server: asyncio.Server | None = None
        self._tunnels: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._connections = 0
        self._refusals = 0
        self._failures = 0

    @property
    def port(self) -> int:
        server = self._server
        if server is None:
            raise ToolError("the egress proxy is not running")
        sockets: Sequence[socket.socket] = server.sockets or ()
        if not sockets:
            raise ToolError("the egress proxy has no listening socket")
        return int(sockets[0].getsockname()[1])

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def refusals(self) -> int:
        return self._refusals

    @property
    def failures(self) -> int:
        return self._failures

    async def start(self) -> int:
        async with self._lock:
            if self._closed:
                raise ToolError("the egress proxy was stopped and cannot be restarted")
            if self._server is not None:
                return self.port
            self._server = await asyncio.start_server(
                self._handle_client,
                host=self._listen_host,
                port=0,
                backlog=128,
                limit=MAX_BODY_CHUNK,
            )
            return self.port

    async def stop(self) -> None:
        async with self._lock:
            server, self._server = self._server, None
            self._closed = True
            tunnels = list(self._tunnels)
            self._tunnels.clear()
        if server is not None:
            server.close()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(server.wait_closed(), timeout=5.0)
        for task in tunnels:
            task.cancel()
        if tunnels:
            await asyncio.gather(*tunnels, return_exceptions=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=self._header_timeout)
        except (TimeoutError, asyncio.IncompleteReadError, OSError):
            self._drop(writer)
            return
        if len(request_line) > MAX_REQUEST_LINE_BYTES:
            self._refuse(writer, b"HTTP/1.1 431 Request Header Fields Too Large")
            return
        parts = request_line.decode("latin-1", "replace").strip().split()
        if len(parts) != 3:
            self._refuse(writer, b"HTTP/1.1 400 Bad Request")
            return
        method, target, version = parts[0].upper(), parts[1], parts[2]
        if not version.startswith("HTTP/1."):
            self._refuse(writer, b"HTTP/1.1 505 HTTP Version Not Supported")
            return
        headers = await self._read_headers(reader, writer)
        if headers is None:
            return
        if method == "CONNECT":
            await self._connect_tunnel(reader, writer, target)
            return
        if target.startswith(("http://", "https://")) and method in {
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        }:
            await self._forward_request(reader, writer, method, target, headers)
            return
        self._refuse(writer, b"HTTP/1.1 405 Method Not Allowed")

    async def _read_headers(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> list[tuple[str, str]] | None:
        headers: list[tuple[str, str]] = []
        total = 0
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self._header_timeout)
            except (TimeoutError, asyncio.IncompleteReadError, OSError):
                self._drop(writer)
                return None
            total += len(line)
            if total > MAX_HEADER_BYTES:
                self._refuse(writer, b"HTTP/1.1 431 Request Header Fields Too Large")
                return None
            if line in (b"\r\n", b"\n", b""):
                return headers
            text = line.decode("latin-1", "replace").rstrip("\r\n")
            if ":" not in text:
                self._refuse(writer, b"HTTP/1.1 400 Bad Request")
                return None
            name, _, value = text.partition(":")
            headers.append((name.strip(), value.strip()))

    def _header_value(self, headers: list[tuple[str, str]], name: str) -> str | None:
        lowered = name.lower()
        for key, value in headers:
            if key.lower() == lowered:
                return value
        return None

    def _content_length(self, headers: list[tuple[str, str]]) -> int:
        raw = self._header_value(headers, "Content-Length")
        if raw is None or not raw.strip().isdigit():
            return -1
        return int(raw.strip())

    def _split_target(self, target: str, default_port: int) -> tuple[str, int] | None:
        if target.startswith("["):
            end = target.find("]")
            if end == -1:
                return None
            host = target[1:end]
            rest = target[end + 1 :]
            port = self._parse_port(rest[1:]) if rest.startswith(":") else default_port
        elif ":" in target:
            host, _, port_text = target.partition(":")
            port = self._parse_port(port_text)
            if port == INVALID_PORT:
                port = default_port
        else:
            host, port = target, default_port
        if not host or port == INVALID_PORT:
            return None
        return host, port

    def _parse_port(self, text: str) -> int:
        if not text.isdigit():
            return INVALID_PORT
        port = int(text)
        if port < 1 or port > 65535:
            return INVALID_PORT
        return port

    async def _resolve_egress(
        self, writer: asyncio.StreamWriter, host: str, port: int
    ) -> tuple[str, ...] | None:
        try:
            addresses = await self._resolver.resolve(host, port)
        except DnsResolutionError as exc:
            self._failures += 1
            self._refuse(writer, b"HTTP/1.1 502 Bad Gateway", f"{exc}".encode("utf-8", "replace"))
            return None
        except (BrowserPolicyError, OSError, ToolError) as exc:
            self._refusals += 1
            self._refuse(
                writer,
                b"HTTP/1.1 403 Forbidden",
                f"egress destination rejected by browser policy: {exc}".encode("utf-8", "replace"),
            )
            return None
        if not addresses:
            self._refusals += 1
            self._refuse(writer, b"HTTP/1.1 403 Forbidden", b"egress destination rejected")
            return None
        return addresses

    async def _connect_upstream(
        self, addresses: tuple[str, ...], port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout=self._connect_timeout
                )
            except (TimeoutError, OSError):
                continue
        return None

    async def _connect_tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        split = self._split_target(target, 443)
        if split is None:
            self._refuse(writer, b"HTTP/1.1 400 Bad Request")
            return
        host, port = split
        addresses = await self._resolve_egress(writer, host, port)
        if addresses is None:
            return
        upstream = await self._connect_upstream(addresses, port)
        if upstream is None:
            self._failures += 1
            self._refuse(writer, b"HTTP/1.1 502 Bad Gateway", b"egress upstream unreachable")
            return
        upstream_reader, upstream_writer = upstream
        self._connections += 1
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        except OSError:
            upstream_writer.close()
            return
        await self._tunnel_bytes(reader, writer, upstream_reader, upstream_writer)

    async def _forward_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
    ) -> None:
        parsed = urlsplit(target)
        host = parsed.hostname or ""
        port = parsed.port or 80
        if not host:
            self._refuse(writer, b"HTTP/1.1 400 Bad Request")
            return
        addresses = await self._resolve_egress(writer, host, port)
        if addresses is None:
            return
        upstream = await self._connect_upstream(addresses, port)
        if upstream is None:
            self._failures += 1
            self._refuse(writer, b"HTTP/1.1 502 Bad Gateway", b"egress upstream unreachable")
            return
        upstream_reader, upstream_writer = upstream
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        upgrade = self._header_value(headers, "Upgrade") is not None
        length = self._content_length(headers)
        if length > MAX_DECLARED_BODY_BYTES:
            self._refuse(writer, b"HTTP/1.1 413 Payload Too Large")
            return
        chunked = "chunked" in (self._header_value(headers, "Transfer-Encoding") or "").lower()
        request_head = [f"{method} {path} HTTP/1.1"]
        for name, value in headers:
            lowered = name.lower()
            if lowered in HOP_BY_HOP_REQUEST_HEADERS:
                continue
            if lowered == "connection":
                continue
            request_head.append(f"{name}: {value}")
        if not upgrade:
            request_head.append("Connection: close")
        head_bytes = ("\r\n".join(request_head) + "\r\n\r\n").encode("latin-1")
        try:
            upstream_writer.write(head_bytes)
            await upstream_writer.drain()
        except OSError:
            self._failures += 1
            self._refuse(writer, b"HTTP/1.1 502 Bad Gateway", b"egress upstream write failed")
            return
        if upgrade:
            self._connections += 1
            await self._tunnel_bytes(reader, writer, upstream_reader, upstream_writer)
            return
        if length > 0:
            forwarded = await self._forward_body_exact(reader, upstream_writer, length)
            if not forwarded:
                writer.close()
                upstream_writer.close()
                return
        elif chunked:
            forwarded = await self._forward_body_chunked(reader, upstream_writer)
            if not forwarded:
                writer.close()
                upstream_writer.close()
                return
        self._connections += 1
        await self._relay_response(upstream_reader, writer)

    async def _forward_body_exact(
        self, reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter, length: int
    ) -> bool:
        remaining = length
        while remaining > 0:
            try:
                chunk = await reader.read(min(remaining, MAX_BODY_CHUNK))
            except OSError:
                return False
            if not chunk:
                return False
            try:
                upstream_writer.write(chunk)
                await upstream_writer.drain()
            except OSError:
                return False
            remaining -= len(chunk)
        return True

    async def _forward_body_chunked(
        self, reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter
    ) -> bool:
        while True:
            try:
                size_line = await asyncio.wait_for(reader.readline(), timeout=self._header_timeout)
            except (TimeoutError, asyncio.IncompleteReadError, OSError):
                return False
            if len(size_line) > MAX_REQUEST_LINE_BYTES:
                return False
            try:
                size = int(size_line.split(b";")[0].strip(), 16)
            except ValueError:
                return False
            if size < 0:
                return False
            try:
                upstream_writer.write(size_line)
                if size == 0:
                    trailers = await reader.readuntil(b"\r\n\r\n")
                    upstream_writer.write(trailers)
                else:
                    body = await reader.readexactly(size)
                    trailer = await reader.readexactly(2)
                    upstream_writer.write(body)
                    upstream_writer.write(trailer)
                await upstream_writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                return False
            if size == 0:
                return True

    async def _relay_response(
        self, upstream_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        while True:
            try:
                chunk = await upstream_reader.read(MAX_BODY_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            try:
                client_writer.write(chunk)
                await client_writer.drain()
            except OSError:
                break
        client_writer.close()

    async def _tunnel_bytes(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        downstream = asyncio.ensure_future(self._pump(reader, upstream_writer))
        upstream = asyncio.ensure_future(self._pump(upstream_reader, writer))
        task = asyncio.current_task()
        if task is not None:
            self._tunnels.add(task)
        try:
            done, pending = await asyncio.wait(
                {downstream, upstream}, return_when=asyncio.FIRST_COMPLETED
            )
            for item in pending:
                item.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            if task is not None:
                self._tunnels.discard(task)
            upstream_writer.close()
            writer.close()

    async def _pump(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        while True:
            try:
                chunk = await source.read(MAX_BODY_CHUNK)
            except OSError:
                return
            if not chunk:
                return
            try:
                sink.write(chunk)
                await sink.drain()
            except OSError:
                return

    def _refuse(
        self, writer: asyncio.StreamWriter, status_line: bytes, message: bytes = b""
    ) -> None:
        body = message + b"\r\n" if message else b""
        response = (
            status_line
            + b"\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        with contextlib.suppress(OSError):
            writer.write(response)
        writer.close()

    def _drop(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
