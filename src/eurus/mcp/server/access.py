"""Access control for the built-in Eurus MCP server.

The server exposes nothing by default. Tools are exposed only through an
explicit allowlist (exact names or fnmatch patterns) kept in
``security/server-access.json``. Mutating or approval-gated tools are
additionally denied unless ``allow_mutating`` is explicitly enabled - external
MCP clients cannot approve actions interactively, so the safe default is to
refuse them.

HTTP transports require a bearer token; unauthenticated endpoints are never
started. Loopback bindings may disable authentication explicitly for local
scripting; non-loopback bindings always require a token.
"""

from __future__ import annotations

import fnmatch
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path

from eurus.tools.base import Tool, ToolRegistry

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

TOKEN_ENV_VAR = "EURUS_MCP_SERVER_TOKEN"


def is_loopback(host: str) -> bool:
    return host.lower() in LOOPBACK_HOSTS


def tokens_match(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


@dataclass(slots=True)
class ServerAccessPolicy:
    allowed_tools: set[str] = field(default_factory=set)
    allowed_patterns: tuple[str, ...] = ()
    allow_mutating: bool = False
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> ServerAccessPolicy:
        policy = cls(path=path)
        if not path.exists():
            return policy
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return policy
        if not isinstance(raw, dict):
            return policy
        tools = raw.get("allowed_tools")
        if isinstance(tools, list):
            policy.allowed_tools = {str(item) for item in tools}
        patterns = raw.get("allowed_patterns")
        if isinstance(patterns, list):
            policy.allowed_patterns = tuple(str(item) for item in patterns)
        policy.allow_mutating = bool(raw.get("allow_mutating", False))
        return policy

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("policy path is not configured")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_patterns": list(self.allowed_patterns),
            "allow_mutating": self.allow_mutating,
        }
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def add_tool(self, name: str) -> None:
        self.allowed_tools.add(name)

    def allow(self, name: str) -> bool:
        if name in self.allowed_tools:
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.allowed_patterns)

    def check(self, tool: Tool) -> tuple[bool, str]:
        if not self.allow(tool.name):
            return False, f"tool '{tool.name}' is not allowlisted on this MCP server"
        if (tool.mutates or tool.requires_approval) and not self.allow_mutating:
            return False, (
                f"tool '{tool.name}' mutates state or requires approval; enable "
                "'allow_mutating' in server-access.json to expose it"
            )
        return True, ""

    def exposed_tools(self, registry: ToolRegistry) -> list[Tool]:
        return [tool for tool in registry.all() if self.check(tool)[0]]


def default_access_path(home: Path) -> Path:
    return home / "security" / "server-access.json"
