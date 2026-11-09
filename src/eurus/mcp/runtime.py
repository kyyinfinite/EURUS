from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Callable
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

FIRST_CONNECT_MAX_WAIT = 12.0


@dataclass(slots=True)
class MCPRuntime:
    manager: MCPToolManager
    policy: MCPPolicy
    google: GoogleWorkspaceProvider | None = None
    warnings: list[str] = field(default_factory=list)
    home: Path | None = None
    _connect_task: asyncio.Task[None] | None = None

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
        warnings.extend(f"mcp-policies: {item}" for item in policy.load_warnings)

        google: GoogleWorkspaceProvider | None = None
        if include_google:
            try:
                auth = build_auth(home, environ=environ, allow_prompt=False)
                google = build_provider(home, auth=auth)
                warnings.extend(f"google accounts: {item}" for item in auth.accounts.load_warnings)
                warnings.extend(f"google config: {item}" for item in google.settings.load_warnings)
                if google.settings.enabled_ids() and not google.diagnostics()["active_account"]:
                    warnings.append(
                        "Google Workspace services are enabled but no account is "
                        "logged in; run: eurus auth google login"
                    )
                missing = google.scope_drift()
                if missing:
                    warnings.append(
                        "Google token is missing scopes for newly enabled services; "
                        "re-run: eurus auth google login"
                    )
                policy.google_access = google.settings.access
            except Exception as exc:
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

    def _startup_mode(self) -> str:
        if self.google is None:
            return "background"
        return self.google.settings.startup

    def start_background_connect(self) -> None:
        if self._startup_mode() == "lazy":
            return
        self._begin_connect()

    def _begin_connect(self) -> None:
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(
                self.manager.connect(), name="eurus-mcp-background-connect"
            )

    def _default_max_wait(self) -> float:
        if self._startup_mode() == "eager":
            return self.connect_timeout_hint()
        return FIRST_CONNECT_MAX_WAIT

    async def ensure_connected(self, max_wait: float | None = None) -> None:
        self._begin_connect()
        assert self._connect_task is not None
        wait = self._default_max_wait() if max_wait is None else max_wait
        done, _ = await asyncio.wait({self._connect_task}, timeout=wait)
        if not done:
            return
        task = self._connect_task
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None and not isinstance(error, (OSError, RuntimeError, ValueError)):
                raise error

    async def connect(self) -> None:
        await self.manager.connect()

    def _deny_matcher(self) -> Callable[[str], bool] | None:
        if self.google is None:
            return None
        patterns = [item for item in self.google.settings.tools_deny if item]
        if not patterns:
            return None

        def deny(name: str) -> bool:
            if not name.startswith("mcp_google_"):
                return False
            bare = name.removeprefix("mcp_google_")
            candidates = [name, bare]
            if "_" in bare:
                candidates.append(bare.split("_", 1)[1])
            return any(
                fnmatch.fnmatchcase(candidate, pattern)
                for pattern in patterns
                for candidate in candidates
            )

        return deny

    def register_tools(self, registry: ToolRegistry) -> int:
        return self.manager.register_tools(registry, deny=self._deny_matcher())

    def denied_tools(self) -> list[str]:
        deny = self._deny_matcher()
        if deny is None:
            return []
        return [name for name in self.manager.tool_names() if deny(name)]

    def unregister_tools(self, registry: ToolRegistry) -> int:
        return self.manager.unregister_tools(registry)

    async def close(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            await asyncio.gather(self._connect_task, return_exceptions=True)
            self._connect_task = None
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


__all__ = ["FIRST_CONNECT_MAX_WAIT", "MCPRuntime"]
