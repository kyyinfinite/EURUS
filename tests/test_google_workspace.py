"""Tests for the native Google Workspace provider and service catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from eurus.mcp.auth.accounts import GoogleAccountManager
from eurus.mcp.auth.google import GoogleOAuthManager
from eurus.mcp.auth.token_store import CredentialStore, EncryptedFileBackend
from eurus.mcp.manager import MCPServerConfig
from eurus.mcp.providers.google_workspace import (
    SERVICES,
    build_auth,
    build_provider,
    settings_path,
)
from eurus.mcp.providers.google_workspace.catalog import service_by_id
from eurus.mcp.providers.google_workspace.scopes import scopes_for_services


def test_catalog_contains_nine_official_services() -> None:
    assert [item.service_id for item in SERVICES] == [
        "gmail",
        "drive",
        "docs",
        "sheets",
        "slides",
        "calendar",
        "chat",
        "people",
        "classroom",
    ]
    for service in SERVICES:
        assert service.endpoint.startswith("https://")
        assert service.endpoint.endswith("/mcp/v1")
        assert service.scopes
        assert service.server_name == f"google-{service.service_id}"


def test_official_endpoints_match_google_documentation() -> None:
    expected = {
        "gmail": "https://gmailmcp.googleapis.com/mcp/v1",
        "drive": "https://drivemcp.googleapis.com/mcp/v1",
        "docs": "https://docsmcp.googleapis.com/mcp/v1",
        "sheets": "https://sheetsmcp.googleapis.com/mcp/v1",
        "slides": "https://slidesmcp.googleapis.com/mcp/v1",
        "calendar": "https://calendarmcp.googleapis.com/mcp/v1",
        "chat": "https://chatmcp.googleapis.com/mcp/v1",
        "people": "https://people.googleapis.com/mcp/v1",
        "classroom": "https://classroommcp.googleapis.com/mcp/v1",
    }
    for service in SERVICES:
        assert service.endpoint == expected[service.service_id]


def test_unknown_service_rejected() -> None:
    with pytest.raises(ValueError, match="unknown Google Workspace service"):
        service_by_id("nonexistent")


def test_scopes_union_is_deduplicated_and_includes_identity() -> None:
    scopes = scopes_for_services(["gmail", "drive", "docs"])
    assert scopes[0] == "openid"
    assert len(scopes) == len(set(scopes))
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "https://www.googleapis.com/auth/documents" in scopes
    # gmail scopes must not leak when gmail is not enabled
    without_gmail = scopes_for_services(["drive"])
    assert "https://www.googleapis.com/auth/gmail.readonly" not in without_gmail


def test_all_services_disabled_by_default(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    assert provider.settings.enabled_ids() == []
    assert provider.server_configs() == []
    assert provider.login_scopes()[0] == "openid"


def test_enable_disable_persists(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    provider.enable("gmail")
    provider.enable("calendar")
    reloaded = build_provider(tmp_path)
    assert reloaded.settings.enabled_ids() == ["gmail", "calendar"]
    reloaded.disable("gmail")
    assert build_provider(tmp_path).settings.enabled_ids() == ["calendar"]
    assert settings_path(tmp_path).name == "google-workspace.json"


def test_server_configs_require_active_account(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    provider.enable("gmail")
    assert provider.server_configs() == []

    auth = build_auth(tmp_path)
    auth.accounts.add("alice@example.com")
    provider = build_provider(tmp_path, auth=auth)
    configs = provider.server_configs()
    assert [item.name for item in configs] == ["google-gmail"]
    assert all(isinstance(item, MCPServerConfig) for item in configs)
    assert configs[0].transport == "http"
    assert configs[0].url == "https://gmailmcp.googleapis.com/mcp/v1"


def test_auth_provider_binds_to_active_account(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    provider.enable("gmail")
    none_auth = provider.auth_provider(
        MCPServerConfig(name="google-gmail", transport="http", url="https://x/mcp")
    )
    assert none_auth is None

    auth = build_auth(tmp_path)
    auth.accounts.add("alice@example.com")
    provider = build_provider(tmp_path, auth=auth)
    provider.enable("gmail")
    config = MCPServerConfig(name="google-gmail", transport="http", url="https://x/mcp")
    bearer = provider.auth_provider(config)
    assert bearer is not None
    other = MCPServerConfig(name="not-google", transport="http", url="https://x/mcp")
    assert provider.auth_provider(other) is None


def test_token_failure_isolated_per_account(tmp_path: Path) -> None:
    home = tmp_path
    accounts = GoogleAccountManager(home / "auth" / "google" / "accounts.json")
    store = CredentialStore(EncryptedFileBackend(home / "auth" / "google" / "tokens", "pp"))
    manager = GoogleOAuthManager(home, store=store, accounts=accounts)
    alice = accounts.add("alice@example.com")
    bob = accounts.add("bob@example.com")
    provider = build_provider(home, auth=manager)
    provider.enable("gmail")

    # bob's credentials are gone -> reauth required for bob only
    import time

    from eurus.mcp.auth.token_store import StoredCredentials

    store.store(
        StoredCredentials(
            account_id=alice.account_id,
            email=alice.email,
            client_id="cid",
            refresh_token="rt-alice",
            access_token="at-alice",
            expires_at=time.time() + 3600,
            scope="openid",
        )
    )
    accounts.set_active(bob.account_id)
    import asyncio

    async def check() -> tuple[bool, bool]:
        bob_fails = False
        try:
            await manager.token_manager.get_access_token(bob.account_id)
        except Exception:
            bob_fails = True
        accounts.set_active(alice.account_id)
        alice_token = await manager.token_manager.get_access_token(alice.account_id)
        return bob_fails, alice_token == "at-alice"

    bob_fails, alice_ok = asyncio.run(check())
    assert bob_fails
    assert alice_ok
    # provider auth still works for alice's servers
    assert (
        provider.auth_provider(
            MCPServerConfig(name="google-gmail", transport="http", url="https://x/mcp")
        )
        is not None
    )


def test_service_status_reports_preview_notes(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    provider.enable("chat")
    rows = {row["service"]: row for row in provider.service_status()}
    assert rows["chat"]["requires_workspace_account"] is True
    assert rows["chat"]["notes"]
    assert rows["chat"]["enabled"] is True
    assert rows["chat"]["state"] == "disconnected"
    assert rows["gmail"]["enabled"] is False


def test_diagnostics_report_login_requirement(tmp_path: Path) -> None:
    provider = build_provider(tmp_path)
    assert provider.diagnostics()["login_required"] is False
    provider.enable("gmail")
    diagnostics = provider.diagnostics()
    assert diagnostics["enabled_services"] == ["gmail"]
    assert diagnostics["login_required"] is True
