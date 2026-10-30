"""Built-in MCP integration providers."""

from eurus.mcp.providers.google_workspace import (
    GoogleWorkspaceProvider,
    build_auth,
    build_provider,
)

__all__ = ["GoogleWorkspaceProvider", "build_auth", "build_provider"]
