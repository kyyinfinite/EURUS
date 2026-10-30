"""Google Workspace provider: one provider, independent service adapters.

The provider turns enabled catalog services into MCP client server configs
that point at Google's official remote MCP endpoints, authenticated with the
active Google account's OAuth token. It plugs into MCPToolManager through the
``auth_provider`` hook, so Google services share the same connection
lifecycle, tool discovery and policy enforcement as any other MCP server.

Authentication is shared between all services of the active account; a token
failure marks only that account's servers as needing re-authentication and
never affects other accounts or unrelated MCP servers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from eurus.mcp.auth.accounts import GoogleAccountManager
from eurus.mcp.auth.google import GoogleOAuthManager
from eurus.mcp.manager import MCPServerConfig
from eurus.mcp.providers.google_workspace.catalog import SERVICES, GoogleService, service_by_id
from eurus.mcp.providers.google_workspace.scopes import scopes_for_services
from eurus.mcp.providers.google_workspace.services import (
    GoogleWorkspaceSettings,
    settings_path,
)


class GoogleBearerAuth(httpx.Auth):
    """httpx auth that injects a fresh Google access token on every request."""

    def __init__(self, token_getter: Callable[[], Awaitable[str]]) -> None:
        self._token_getter = token_getter

    async def async_auth_flow(self, request: httpx.Request) -> Any:
        token = await self._token_getter()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class GoogleWorkspaceProvider:
    def __init__(
        self,
        home: Path,
        *,
        auth: GoogleOAuthManager | None = None,
        settings: GoogleWorkspaceSettings | None = None,
    ) -> None:
        self.home = home
        self.auth = auth
        self.settings = settings or GoogleWorkspaceSettings.load(settings_path(home))

    # ------------------------------------------------------------------
    # Service configuration
    # ------------------------------------------------------------------
    def enable(self, service_id: str) -> GoogleService:
        service = service_by_id(service_id)
        self.settings.set_enabled(service_id, True)
        self.settings.save()
        return service

    def disable(self, service_id: str) -> GoogleService:
        service = service_by_id(service_id)
        self.settings.set_enabled(service_id, False)
        self.settings.save()
        return service

    def enabled_services(self) -> list[GoogleService]:
        return [service for service in SERVICES if self.settings.is_enabled(service.service_id)]

    def all_services(self) -> tuple[GoogleService, ...]:
        return SERVICES

    def login_scopes(self) -> list[str]:
        return scopes_for_services(self.settings.enabled_ids())

    # ------------------------------------------------------------------
    # MCP client integration
    # ------------------------------------------------------------------
    def _active_account_id(self) -> str:
        if self.auth is None:
            return ""
        active = self.auth.accounts.active
        return active.account_id if active is not None else ""

    def server_configs(self) -> list[MCPServerConfig]:
        """Server configs for enabled services that have an account."""
        account_id = self._active_account_id()
        if not account_id or self.auth is None:
            return []
        configs: list[MCPServerConfig] = []
        for service in self.enabled_services():
            entry = self.settings.services.get(service.service_id)
            timeout = entry.timeout_seconds if entry else 45.0
            configs.append(
                MCPServerConfig(
                    name=service.server_name,
                    transport="http",
                    url=service.endpoint,
                    enabled=True,
                    timeout_seconds=timeout,
                )
            )
        return configs

    def auth_provider(self, config: MCPServerConfig) -> GoogleBearerAuth | None:
        if self.auth is None or not config.name.startswith("google-"):
            return None
        account_id = self._active_account_id()
        if not account_id:
            return None

        async def token_getter() -> str:
            assert self.auth is not None
            return await self.auth.token_manager.get_access_token(account_id)

        return GoogleBearerAuth(token_getter)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def service_status(
        self,
        connection_states: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        states = connection_states or {}
        account = self._active_account_id()
        rows: list[dict[str, Any]] = []
        for service in SERVICES:
            state = states.get(service.server_name, {})
            default_state = (
                "disabled" if not self.settings.is_enabled(service.service_id) else "disconnected"
            )
            rows.append(
                {
                    "service": service.service_id,
                    "label": service.label,
                    "endpoint": service.endpoint,
                    "enabled": self.settings.is_enabled(service.service_id),
                    "account": account if self.settings.is_enabled(service.service_id) else "",
                    "state": str(state.get("state", default_state)),
                    "tools": int(state.get("tools", 0)),
                    "error": str(state.get("error", "")),
                    "requires_workspace_account": service.requires_workspace_account,
                    "notes": service.notes,
                }
            )
        return rows

    def diagnostics(self) -> dict[str, Any]:
        enabled = self.settings.enabled_ids()
        account = self._active_account_id()
        return {
            "enabled_services": enabled,
            "active_account": account,
            "oauth_ready": self.auth is not None and self.auth.client_configured,
            "login_required": bool(enabled) and not account,
            "scopes": self.login_scopes(),
        }


def build_provider(
    home: Path,
    auth: GoogleOAuthManager | None = None,
) -> GoogleWorkspaceProvider:
    return GoogleWorkspaceProvider(home, auth=auth)


def accounts_path(home: Path) -> Path:
    return home / "auth" / "google" / "accounts.json"


def tokens_dir(home: Path) -> Path:
    return home / "auth" / "google" / "tokens"


def build_auth(home: Path, environ: dict[str, str] | None = None) -> GoogleOAuthManager:
    """Construct the full Google authentication stack for a Eurus home."""
    from eurus.mcp.auth.token_store import CredentialStore

    accounts = GoogleAccountManager(accounts_path(home))
    store = CredentialStore.open(tokens_dir(home))
    return GoogleOAuthManager(home, store=store, accounts=accounts, environ=environ)


__all__ = [
    "GoogleBearerAuth",
    "GoogleWorkspaceProvider",
    "accounts_path",
    "build_auth",
    "build_provider",
    "settings_path",
    "tokens_dir",
]
