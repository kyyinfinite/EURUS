"""Built-in Eurus MCP server: expose Eurus tools to external MCP clients."""

from eurus.mcp.server.access import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    TOKEN_ENV_VAR,
    ServerAccessPolicy,
    default_access_path,
    is_loopback,
    tokens_match,
)
from eurus.mcp.server.runtime import EurusMCPServer, EurusToolServer
from eurus.mcp.server.tools import default_context_factory
from eurus.mcp.server.transport import (
    HTTPServerError,
    build_http_app,
    run_stdio,
    serve_http,
    validate_http_security,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EurusMCPServer",
    "TOKEN_ENV_VAR",
    "EurusToolServer",
    "HTTPServerError",
    "ServerAccessPolicy",
    "build_http_app",
    "default_access_path",
    "default_context_factory",
    "is_loopback",
    "run_stdio",
    "serve_http",
    "tokens_match",
    "validate_http_security",
]
