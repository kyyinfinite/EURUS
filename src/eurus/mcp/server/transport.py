"""Transports for the built-in Eurus MCP server.

- STDIO: local usage, no authentication overhead.
- Streamable HTTP: binds 127.0.0.1 by default and requires a bearer token;
  unauthenticated endpoints are never started.
"""

from __future__ import annotations

import contextlib
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from eurus.mcp.server.access import is_loopback, tokens_match
from eurus.mcp.server.tools import EurusToolServer

MCP_PATH = "/mcp"


class _RawAsgi:
    """Adapter so Starlette treats the session manager as a raw ASGI app."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._handler(scope, receive, send)


class BearerAuthMiddleware:
    """Raw ASGI middleware enforcing a bearer token on every HTTP request."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        scheme, _, provided = authorization.partition(" ")
        if scheme.lower() != "bearer" or not tokens_match(self.token, provided.strip()):
            response = JSONResponse(
                {"error": "unauthorized: a valid bearer token is required"}, status_code=401
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def run_stdio(tool_server: EurusToolServer) -> None:
    """Serve over stdio; intended for local parent-process usage."""
    import mcp.server.stdio as stdio_server

    options = tool_server.server.create_initialization_options()
    async with stdio_server.stdio_server() as (read_stream, write_stream):
        await tool_server.server.run(read_stream, write_stream, options, raise_exceptions=False)


def build_http_app(tool_server: EurusToolServer, token: str) -> Any:
    """Build the streamable-HTTP ASGI app (with bearer authentication)."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(
        app=tool_server.server, event_store=None, json_response=True
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        async with session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route(
                MCP_PATH,
                _RawAsgi(session_manager.handle_request),
                methods=["GET", "POST", "DELETE"],
            )
        ],
        lifespan=lifespan,
    )
    return BearerAuthMiddleware(app, token)


class HTTPServerError(RuntimeError):
    pass


async def serve_http(
    tool_server: EurusToolServer,
    *,
    host: str,
    port: int,
    token: str,
) -> None:
    """Serve over streamable HTTP with graceful shutdown."""
    app = build_http_app(tool_server, token)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        with contextlib.suppress(Exception):
            await server.shutdown()


def validate_http_security(host: str, token: str, *, auth_disabled: bool) -> None:
    """Fail-closed checks before binding an HTTP endpoint."""
    loopback = is_loopback(host)
    if not loopback:
        if auth_disabled:
            raise HTTPServerError(
                f"refusing to expose an unauthenticated MCP endpoint on {host}; "
                "authentication can only be disabled on loopback addresses"
            )
        if not token:
            raise HTTPServerError(
                f"refusing to expose the MCP endpoint on {host} without a token; "
                "pass --token or set EURUS_MCP_SERVER_TOKEN"
            )
