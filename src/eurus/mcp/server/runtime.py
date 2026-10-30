"""Runtime assembly for the built-in Eurus MCP server."""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from eurus.mcp.server.access import (
    TOKEN_ENV_VAR,
    ServerAccessPolicy,
    default_access_path,
)
from eurus.mcp.server.tools import EurusToolServer, default_context_factory
from eurus.tools.base import ToolRegistry


@dataclass(slots=True)
class EurusMCPServer:
    registry: ToolRegistry
    policy: ServerAccessPolicy

    @classmethod
    def create(
        cls,
        registry: ToolRegistry,
        *,
        home: Path,
        allowed_tools: list[str] | None = None,
        allow_mutating: bool | None = None,
    ) -> EurusMCPServer:
        policy = ServerAccessPolicy.load(default_access_path(home))
        for name in allowed_tools or []:
            policy.add_tool(name)
        if allow_mutating is not None:
            policy.allow_mutating = allow_mutating
        return cls(registry=registry, policy=policy)

    def tool_server(self, *, timeout: float = 120.0) -> EurusToolServer:
        return EurusToolServer(
            self.registry, self.policy, context_factory=default_context_factory(timeout)
        )

    def resolve_token(self, provided: str | None) -> str:
        """Token precedence: CLI flag, environment, generated ephemeral."""
        if provided:
            return provided
        import os

        env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if env_token:
            return env_token
        generated = secrets.token_urlsafe(32)
        print(
            f"Generated a one-time MCP server token (also settable via "
            f"{TOKEN_ENV_VAR} or --token):\n\n    {generated}\n",
            file=sys.stderr,
        )
        return generated


__all__ = [
    "EurusMCPServer",
    "EurusToolServer",
    "ServerAccessPolicy",
    "default_access_path",
    "default_context_factory",
]
