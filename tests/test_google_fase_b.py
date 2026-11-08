from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.cli import main
from eurus.mcp.auth.accounts import GoogleAccountManager
from eurus.mcp.auth.google import GoogleOAuthManager
from eurus.mcp.auth.token_store import CredentialStore, EncryptedFileBackend
from eurus.mcp.manager import MCPServerConfig
from eurus.mcp.providers.google_workspace import build_provider
from eurus.mcp.providers.google_workspace.provider import GoogleWorkspaceProvider
from eurus.mcp.providers.google_workspace.services import (
    PRESET_SERVICES,
    GoogleConfigError,
    GoogleWorkspaceSettings,
)
from eurus.mcp.runtime import FIRST_CONNECT_MAX_WAIT, MCPRuntime
from eurus.tools.base import ToolRegistry


def test_apply_preset_enables_expected_bundles(tmp_path: Path) -> None:
    settings = GoogleWorkspaceSettings.defaults()
    assert settings.apply_preset("personal") == ["gmail", "drive", "calendar", "people"]
    assert settings.preset == "personal"

    fresh = GoogleWorkspaceSettings.defaults()
    student = fresh.apply_preset("student")
    assert "classroom" in student and "chat" not in student

    fresh = GoogleWorkspaceSettings.defaults()
    workspace = fresh.apply_preset("workspace")
    assert "chat" in workspace and "classroom" not in workspace

    custom = GoogleWorkspaceSettings.defaults()
    custom.set_enabled("gmail", True)
    assert custom.apply_preset("custom") == ["gmail"]
    assert custom.preset == "custom"


def test_apply_preset_is_idempotent_and_rejects_unknown(tmp_path: Path) -> None:
    settings = GoogleWorkspaceSettings.defaults()
    first = settings.apply_preset("workspace")
    second = settings.apply_preset("workspace")
    assert first == second
    with pytest.raises(GoogleConfigError, match="unknown preset"):
        settings.apply_preset("galaxy")


def test_google_setup_preset_flag_enables_bundle(tmp_path: Path) -> None:
    runner = _runner()
    result = runner.invoke(
        main,
        ["google", "setup", "--preset", "student", "--no-test"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    config = json.loads(
        (tmp_path / "integrations" / "google-workspace.json").read_text(encoding="utf-8")
    )
    enabled = {key for key, item in config["services"].items() if item["enabled"]}
    assert enabled == set(PRESET_SERVICES["student"])
    assert config["preset"] == "student"
    assert "Preset 'student'" in result.output


def _runtime_with_deny(tmp_path: Path, deny: list[str]) -> MCPRuntime:
    home = tmp_path
    config_dir = home / "integrations"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "google-workspace.json").write_text(
        json.dumps({"version": 2, "tools": {"deny": deny}}),
        encoding="utf-8",
    )
    runtime = MCPRuntime.build(home, include_google=True)
    manager = runtime.manager
    manager.configs["google-gmail"] = MCPServerConfig(
        name="google-gmail",
        transport="http",
        url="https://gmailmcp.googleapis.com/mcp/v1",
        enabled=True,
    )
    manager._tools["mcp_google_gmail_delete_message"] = {
        "server": "google-gmail",
        "remote_name": "delete_message",
        "description": "fake delete",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    manager._tools["mcp_google_gmail_search_threads"] = {
        "server": "google-gmail",
        "remote_name": "search_threads",
        "description": "fake search",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    manager._connected = True
    return runtime


async def test_tools_deny_blocks_registration(tmp_path: Path) -> None:
    runtime = _runtime_with_deny(tmp_path, ["delete_*"])
    registry = ToolRegistry()
    runtime.register_tools(registry)
    assert "mcp_google_gmail_delete_message" not in registry
    assert "mcp_google_gmail_search_threads" in registry
    assert runtime.denied_tools() == ["mcp_google_gmail_delete_message"]
    await runtime.close()


async def test_tools_deny_matches_full_exposed_name_too(tmp_path: Path) -> None:
    runtime = _runtime_with_deny(tmp_path, ["mcp_google_gmail_search_*"])
    registry = ToolRegistry()
    runtime.register_tools(registry)
    assert "mcp_google_gmail_search_threads" not in registry
    assert "mcp_google_gmail_delete_message" in registry
    await runtime.close()


async def test_without_deny_patterns_everything_registers(tmp_path: Path) -> None:
    runtime = _runtime_with_deny(tmp_path, [])
    registry = ToolRegistry()
    runtime.register_tools(registry)
    assert "mcp_google_gmail_delete_message" in registry
    assert runtime.denied_tools() == []
    await runtime.close()


def test_manager_register_tools_deny_predicate() -> None:
    from eurus.mcp.manager import MCPToolManager
    from eurus.mcp.policy import MCPPolicy

    manager = MCPToolManager(
        [MCPServerConfig(name="srv", transport="stdio", command="noop")],
        policy=MCPPolicy(),
    )
    manager._tools["mcp_srv_alpha"] = {
        "server": "srv",
        "remote_name": "alpha",
        "description": "a",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    manager._tools["mcp_srv_beta"] = {
        "server": "srv",
        "remote_name": "beta",
        "description": "b",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    manager._connected = True
    registry = ToolRegistry()
    count = manager.register_tools(registry, deny=lambda name: name.endswith("beta"))
    assert count == 1
    assert "mcp_srv_alpha" in registry
    assert "mcp_srv_beta" not in registry


def _runtime_with_startup(tmp_path: Path, startup: str) -> MCPRuntime:
    config_dir = tmp_path / "integrations"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "google-workspace.json").write_text(
        json.dumps({"version": 2, "startup": startup}),
        encoding="utf-8",
    )
    return MCPRuntime.build(tmp_path, include_google=True)


async def test_startup_lazy_defers_connection_until_first_use(tmp_path: Path) -> None:
    runtime = _runtime_with_startup(tmp_path, "lazy")
    runtime.start_background_connect()
    assert runtime._connect_task is None, "lazy must not connect at startup"
    await runtime.ensure_connected()
    assert runtime._connect_task is not None
    assert runtime._connect_task.done()
    await runtime.close()


async def test_startup_background_keeps_short_first_wait(tmp_path: Path) -> None:
    runtime = _runtime_with_startup(tmp_path, "background")
    runtime.start_background_connect()
    assert runtime._connect_task is not None, "background connects at startup"
    assert runtime._default_max_wait() == FIRST_CONNECT_MAX_WAIT
    await runtime.close()


async def test_startup_eager_waits_for_full_connect_budget(tmp_path: Path) -> None:
    runtime = _runtime_with_startup(tmp_path, "eager")
    assert runtime._default_max_wait() == runtime.connect_timeout_hint()
    await runtime.close()


def _auth(tmp_path: Path) -> GoogleOAuthManager:
    accounts = GoogleAccountManager(tmp_path / "auth" / "google" / "accounts.json")
    store = CredentialStore(EncryptedFileBackend(tmp_path / "auth" / "google" / "tokens", "pp"))
    return GoogleOAuthManager(tmp_path, store=store, accounts=accounts)


def test_account_pin_uses_requested_account(tmp_path: Path) -> None:
    config_dir = tmp_path / "integrations"
    config_dir.mkdir(parents=True)
    (config_dir / "google-workspace.json").write_text(
        json.dumps({"version": 2, "account": "bob@example.com"}),
        encoding="utf-8",
    )
    auth = _auth(tmp_path)
    alice = auth.accounts.add("alice@example.com")
    bob = auth.accounts.add("bob@example.com")
    auth.accounts.set_active(alice.account_id)

    provider = build_provider(tmp_path, auth=auth)
    assert provider._active_account_id() == bob.account_id
    assert provider.diagnostics()["account_setting"] == "bob@example.com"


def test_account_pin_by_account_id_and_auto(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    alice = auth.accounts.add("alice@example.com")
    bob = auth.accounts.add("bob@example.com")
    auth.accounts.set_active(bob.account_id)

    settings = GoogleWorkspaceSettings.defaults()
    settings.account = alice.account_id
    provider = GoogleWorkspaceProvider(tmp_path, auth=auth, settings=settings)
    assert provider._active_account_id() == alice.account_id

    settings.account = "auto"
    assert provider._active_account_id() == bob.account_id


def test_unknown_account_pin_reports_login_required(tmp_path: Path) -> None:
    config_dir = tmp_path / "integrations"
    config_dir.mkdir(parents=True)
    (config_dir / "google-workspace.json").write_text(
        json.dumps(
            {"version": 2, "account": "ghost@example.com", "services": {"gmail": {"enabled": True}}}
        ),
        encoding="utf-8",
    )
    auth = _auth(tmp_path)
    auth.accounts.add("alice@example.com")

    provider = build_provider(tmp_path, auth=auth)
    assert provider._active_account_id() == ""
    diagnostics = provider.diagnostics()
    assert diagnostics["login_required"] is True, "never silently uses another account's token"
    assert provider.server_configs() == []


def _runner() -> object:
    from click.testing import CliRunner

    return CliRunner()


def _env(home: Path) -> dict[str, str]:
    return {
        "EURUS_HOME": str(home),
        "HOME": str(home),
        "TERM": "dumb",
        "COLUMNS": "220",
    }
