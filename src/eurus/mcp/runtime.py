"""Centralized MCP integration runtime.

One builder assembles the whole MCP stack for every entry point (CLI, TUI,
Telegram gateway, scheduler):

    external MCP servers (mcp.json, unchanged format)
      + native Google Workspace services (enabled + authenticated)
      + central security policy (security/mcp-policies.json)
      -> one MCPToolManager with per-server lifecycles

Missing Google credentials never prevent Eurus from starting; the provider is
skipped and the condition is reported through diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.mcp.manager import MCPServerConfig, MCPToolManager
from eurus.mcp.policy import MCPPolicy, default_policy_path
from eurus.mcp.providers.google_workspace import (
    GoogleWorkspaceProvider,
    build_auth,
    build_provider,
)
from eurus.tools.base import ToolRegistry

MCP_CONFIG_FILE = "mcp.json"


@dataclass(slots=True)
class MCPRuntime:
    manager: MCPToolManager
    policy: MCPPolicy
    google: GoogleWorkspaceProvider | None = None
    warnings: list[str] = field(default_factory=list)
    home: Path | None = None

    @classmethod
    def build(
        cls,
        home: Path,
        *,
        include_google: bool = True,
        environ: dict[str, str] | None = None,
    ) -> MCPRuntime:
        warnings: list[str] = []
        external: list[MCPServerConfig] = []
        mcp_config = home / MCP_CONFIG_FILE
        if mcp_config.exists():
            external.extend(MCPToolManager.load(mcp_config).configs.values())

        policy = MCPPolicy.load(default_policy_path(home))

        google: GoogleWorkspaceProvider | None = None
        if include_google:
            try:
                auth = build_auth(home, environ=environ)
                google = build_provider(home, auth=auth)
                if google.settings.enabled_ids() and not google.diagnostics()["active_account"]:
                    warnings.append(
                        "Google Workspace services are enabled but no account is "
                        "logged in; run: eurus auth google login"
                    )
            except Exception as exc:  # never block startup on auth problems
                warnings.append(
                    f"Google Workspace provider unavailable: {type(exc).__name__}: {exc}"
                )
                google = None

        configs = list(external)
        auth_provider = None
        if google is not None:
            google_configs = google.server_configs()
            existing = {item.name for item in configs}
            configs.extend(item for item in google_configs if item.name not in existing)
            auth_provider = google.auth_provider

        manager = MCPToolManager(
            configs,
            auth_provider=auth_provider,
            policy=policy,
        )
        return cls(manager=manager, policy=policy, google=google, warnings=warnings, home=home)

    @property
    def mcp(self) -> MCPToolManager:
        return self.manager

    async def connect(self) -> None:
        await self.manager.connect()

    def register_tools(self, registry: ToolRegistry) -> int:
        return self.manager.register_tools(registry)

    def unregister_tools(self, registry: ToolRegistry) -> int:
        return self.manager.unregister_tools(registry)

    async def close(self) -> None:
        await self.manager.close()

    def connect_timeout_hint(self) -> float:
        enabled = [item.timeout_seconds for item in self.manager.configs.values() if item.enabled]
        return max(enabled, default=1.0) * 3 + 15.0

    async def __aenter__(self) -> MCPRuntime:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    def diagnostics(self) -> dict[str, Any]:
        states = {row["name"]: row for row in self.manager.status()}
        report: dict[str, Any] = {
            "servers": list(states.values()),
            "warnings": list(self.warnings),
            "policy_file": str(default_policy_path(self.home)) if self.home else "",
            "google": self.google.diagnostics() if self.google is not None else None,
        }
        return report


__all__ = ["MCPRuntime"]
