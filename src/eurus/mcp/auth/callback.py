from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from eurus.mcp.auth.oauth import OAuthFlowError


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: str
    state: str


class CallbackServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._finished = asyncio.Event()
        self._result: CallbackResult | None = None
        self._error: str = ""

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise OAuthFlowError("callback server is not running")
        sockets = getattr(self._server, "sockets", None) or ()
        for socket in sockets:
            if socket.getsockname():
                return int(socket.getsockname()[1])
        raise OAuthFlowError("callback server has no bound socket")

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.bound_port}/callback"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait_for_callback(self, timeout: float) -> CallbackResult:
        if self._server is None:
            raise OAuthFlowError("callback server is not running")
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise OAuthFlowError(
                f"timed out waiting for the browser login callback ({timeout:g}s); "
                "open the authorization URL again, or re-run the login with "
                "'--paste' to paste the redirect URL manually"
            ) from exc
        if self._error:
            raise OAuthFlowError(f"authorization was not granted: {self._error}")
        assert self._result is not None
        return self._result

    def complete_manually(self, pasted: str) -> CallbackResult:
        from eurus.mcp.auth.oauth import StateMismatchError

        text = pasted.strip()
        query: dict[str, list[str]] = {}
        if "://" in text or text.startswith("/"):
            parts = urlsplit(text)
            query = parse_qs(parts.query)
        elif "?" in text:
            query = parse_qs(text.split("?", 1)[1])
        codes = query.get("code", [])
        states = query.get("state", [])
        code = codes[0].strip() if codes else text
        if not code:
            raise OAuthFlowError("no authorization code found in the pasted text")
        error = query.get("error", [""])[0]
        if error:
            raise OAuthFlowError(f"authorization was not granted: {error}")
        state = states[0].strip() if states else ""
        if not state:
            raise StateMismatchError("pasted URL is missing the OAuth state parameter")
        result = CallbackResult(code=code, state=state)
        if self._result is None and not self._finished.is_set():
            self._result = result
            self._finished.set()
        return result

    async def __aenter__(self) -> CallbackServer:
        self._finished = asyncio.Event()
        self._result = None
        self._error = ""
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.stop()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            if not request_line or not request_line.strip():
                return
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            parts = request_line.decode("latin-1").split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            if urlsplit(target).path != "/callback":
                self._write_response(writer, 404, b"Unknown path. Expecting /callback.")
                return
            query = parse_qs(urlsplit(target).query)
            error_values = query.get("error", [])
            if error_values:
                self._error = str(error_values[0])
                self._write_response(
                    writer,
                    200,
                    b"<html><body><h3>Authorization was not granted.</h3>"
                    b"You can close this window and try again.</body></html>",
                )
                self._finished.set()
                return
            codes = query.get("code", [])
            states = query.get("state", [])
            if not codes or not states:
                self._write_response(writer, 400, b"Missing code or state parameter.")
                return
            self._result = CallbackResult(code=str(codes[0]), state=str(states[0]))
            self._write_response(
                writer,
                200,
                b"<html><body><h3>Authorization complete.</h3>"
                b"You can close this window and return to Eurus.</body></html>",
            )
            self._finished.set()
        except (TimeoutError, ConnectionError):
            return
        except Exception:
            with suppress(Exception):
                self._write_response(writer, 400, b"Bad request.")
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _write_response(self, writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}
        head = (
            f"HTTP/1.1 {status} {reason.get(status, 'OK')}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
