"""Shared fixtures for MCP integration tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_http_mock() -> FastMCP:
    server = FastMCP("mock-http", host="127.0.0.1")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @server.tool()
    def greet(name: str) -> str:
        """Greet someone."""
        return f"hello {name}"

    return server


@asynccontextmanager
async def running_http_mock() -> AsyncIterator[dict[str, Any]]:
    server = build_http_mock()
    app = server.streamable_http_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    task = asyncio.create_task(uvicorn_server.serve())
    for _ in range(100):
        if uvicorn_server.started:
            break
        await asyncio.sleep(0.05)
    else:
        uvicorn_server.should_exit = True
        raise RuntimeError("mock HTTP MCP server failed to start")
    try:
        yield {"url": f"http://127.0.0.1:{port}/mcp", "server": uvicorn_server}
    finally:
        uvicorn_server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()


@pytest.fixture
async def http_mock_server() -> AsyncIterator[dict[str, Any]]:
    async with running_http_mock() as info:
        yield info


@pytest.fixture
def mock_server_path() -> Path:
    return Path(__file__).parent / "mcp_stdio_mock.py"


# ---------------------------------------------------------------------------
# Mock Google OAuth server
# ---------------------------------------------------------------------------


class MockGoogleOAuth:
    """Local stand-in for accounts.google.com with PKCE verification."""

    def __init__(self) -> None:
        self.valid_codes: dict[str, str] = {}
        self.used_codes: set[str] = set()
        self.refresh_tokens: dict[str, str] = {}
        self.access_email: dict[str, str] = {}
        self.rotate_refresh = False
        self.reject_refresh = False
        self.authorization_denied = False
        self.users: dict[str, str] = {}
        self.next_email = ""

    def issue_code(self, code: str) -> None:
        self.valid_codes[code] = code

    async def handle(self, scope: dict, receive: Any, send: Any) -> None:
        request = Request(scope, receive, send)
        path = request.url.path
        if path == "/authorize":
            await self._handle_authorize(request, scope, receive, send)
        elif path == "/token":
            form = dict(await request.form())
            grant = form.get("grant_type", "")
            if grant == "authorization_code":
                await self._handle_code_exchange(form, scope, receive, send)
            elif grant == "refresh_token":
                await self._handle_refresh(form, scope, receive, send)
            else:
                await _json({"error": "unsupported_grant_type"}, 400, scope, receive, send)
        elif path == "/userinfo":
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            email = self.access_email.get(token)
            if email is None:
                await _json({"error": "invalid_token"}, 401, scope, receive, send)
            else:
                await _json({"email": email, "sub": "42"}, 200, scope, receive, send)
        elif path == "/revoke":
            await _json({}, 200, scope, receive, send)
        else:
            await _json({"error": "not_found"}, 404, scope, receive, send)

    async def _handle_authorize(
        self, request: Request, scope: dict, receive: Any, send: Any
    ) -> None:
        params = dict(request.query_params)
        required = {"client_id", "redirect_uri", "response_type", "scope", "state"}
        if not required <= set(params):
            await _json({"error": "invalid_request"}, 400, scope, receive, send)
            return
        if self.authorization_denied:
            target = f"{params['redirect_uri']}?error=access_denied&state={params['state']}"
        else:
            code = f"mock-code-{secrets.token_urlsafe(6)}"
            self.valid_codes[code] = params.get("code_challenge", "")
            target = f"{params['redirect_uri']}?code={code}&state={params['state']}"
        await RedirectResponse(target)(scope, receive, send)

    async def _handle_code_exchange(self, form: dict, scope: dict, receive: Any, send: Any) -> None:
        code = str(form.get("code", ""))
        verifier = str(form.get("code_verifier", ""))
        challenge = self.valid_codes.get(code, "")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if code in self.used_codes or code not in self.valid_codes:
            await _json({"error": "invalid_grant"}, 400, scope, receive, send)
            return
        if challenge and expected != challenge:
            await _json({"error": "invalid_grant"}, 400, scope, receive, send)
            return
        self.used_codes.add(code)
        del self.valid_codes[code]
        access = f"at-{secrets.token_urlsafe(12)}"
        refresh = f"rt-{secrets.token_urlsafe(12)}"
        self.refresh_tokens[refresh] = "ok"
        email = self.next_email or self.users.get(code, "tester@example.com")
        self.access_email[access] = email
        await _json(
            {
                "access_token": access,
                "expires_in": 3600,
                "refresh_token": refresh,
                "scope": "openid email",
                "token_type": "Bearer",
            },
            200,
            scope,
            receive,
            send,
        )

    async def _handle_refresh(self, form: dict, scope: dict, receive: Any, send: Any) -> None:
        refresh = str(form.get("refresh_token", ""))
        state = self.refresh_tokens.get(refresh, "")
        if self.reject_refresh or state != "ok":
            await _json(
                {"error": "invalid_grant", "error_description": "refresh token revoked"},
                400,
                scope,
                receive,
                send,
            )
            return
        self.refresh_tokens.pop(refresh, None)
        new_refresh = f"rt-{secrets.token_urlsafe(12)}" if self.rotate_refresh else refresh
        self.refresh_tokens[new_refresh] = "ok"
        access = f"at-{secrets.token_urlsafe(12)}"
        self.access_email[access] = "tester@example.com"
        await _json(
            {
                "access_token": access,
                "expires_in": 3600,
                "refresh_token": new_refresh,
                "scope": "openid email",
                "token_type": "Bearer",
            },
            200,
            scope,
            receive,
            send,
        )


async def _json(payload: dict, status: int, scope: dict, receive: Any, send: Any) -> None:
    await JSONResponse(payload, status_code=status)(scope, receive, send)


class _OAuthAsgi:
    """ASGI-3 adapter so uvicorn routes to the mock correctly."""

    def __init__(self, mock: MockGoogleOAuth) -> None:
        self._mock = mock

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self._mock.handle(scope, receive, send)


@pytest.fixture
def mock_google() -> MockGoogleOAuth:
    return MockGoogleOAuth()


ASGIApp = Callable[[dict, Any, Any], Awaitable[None]]


@asynccontextmanager
async def running_oauth_mock(mock: MockGoogleOAuth) -> AsyncIterator[dict[str, str]]:
    port = _free_port()
    config = uvicorn.Config(_OAuthAsgi(mock), host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    task = asyncio.create_task(uvicorn_server.serve())
    for _ in range(100):
        if uvicorn_server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("mock OAuth server failed to start")
    base = f"http://127.0.0.1:{port}"
    try:
        yield {
            "base": base,
            "auth": f"{base}/authorize",
            "token": f"{base}/token",
            "userinfo": f"{base}/userinfo",
            "revoke": f"{base}/revoke",
        }
    finally:
        uvicorn_server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
