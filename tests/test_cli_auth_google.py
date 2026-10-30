"""CLI tests for the new auth google / mcp google / mcp server commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from eurus.cli import main


def _run(args: list[str], home: Path, env: dict[str, str] | None = None) -> object:
    runner = CliRunner()
    environment = {
        "EURUS_HOME": str(home),
        "HOME": str(home),
        "TERM": "dumb",
        "COLUMNS": "220",
    }
    environment.update(env or {})
    return runner.invoke(main, args, env=environment)


def test_auth_google_login_without_client_fails_with_instructions(tmp_path: Path) -> None:
    result = _run(["auth", "google", "login"], tmp_path)
    assert result.exit_code != 0
    assert "EURUS_GOOGLE_CLIENT_ID" in result.output


def test_auth_google_status_empty(tmp_path: Path) -> None:
    result = _run(["auth", "google", "status"], tmp_path)
    assert result.exit_code == 0
    assert "No Google accounts configured" in result.output


def test_mcp_google_list_shows_nine_services(tmp_path: Path) -> None:
    result = _run(["mcp", "google", "list"], tmp_path)
    assert result.exit_code == 0
    for service in (
        "gmail",
        "drive",
        "docs",
        "sheets",
        "slides",
        "calendar",
        "chat",
        "people",
        "classroom",
    ):
        assert service in result.output


def test_mcp_google_enable_disable_round_trip(tmp_path: Path) -> None:
    assert _run(["mcp", "google", "enable", "gmail"], tmp_path).exit_code == 0
    config = json.loads(
        (tmp_path / "integrations" / "google-workspace.json").read_text(encoding="utf-8")
    )
    assert config["services"]["gmail"]["enabled"] is True
    result = _run(["mcp", "google", "status"], tmp_path)
    assert "gmail" in result.output
    assert _run(["mcp", "google", "disable", "gmail"], tmp_path).exit_code == 0
    config = json.loads(
        (tmp_path / "integrations" / "google-workspace.json").read_text(encoding="utf-8")
    )
    assert config["services"]["gmail"]["enabled"] is False


def test_mcp_google_enable_rejects_unknown_service(tmp_path: Path) -> None:
    result = _run(["mcp", "google", "enable", "not-a-service"], tmp_path)
    assert result.exit_code != 0
    assert "unknown Google Workspace service" in result.output


def test_mcp_google_test_reports_missing_login(tmp_path: Path) -> None:
    _run(["mcp", "google", "enable", "gmail"], tmp_path)
    result = _run(["mcp", "google", "test"], tmp_path)
    assert result.exit_code == 0
    assert "No account available" in result.output


def test_mcp_google_test_without_enabled_services(tmp_path: Path) -> None:
    result = _run(["mcp", "google", "test"], tmp_path)
    assert result.exit_code == 0
    assert "No Google services enabled" in result.output


def test_mcp_doctor_reports_missing_login(tmp_path: Path) -> None:
    _run(["mcp", "google", "enable", "gmail"], tmp_path)
    result = _run(["mcp", "doctor"], tmp_path)
    assert result.exit_code != 0
    assert "no account is logged in" in result.output


def test_mcp_doctor_healthy_without_google(tmp_path: Path) -> None:
    result = _run(["mcp", "doctor"], tmp_path)
    assert result.exit_code == 0
    assert "MCP SDK" in result.output
    assert "healthy" in result.output


def test_mcp_permissions_set_and_show(tmp_path: Path) -> None:
    result = _run(
        [
            "mcp",
            "permissions",
            "--set",
            "google-gmail",
            "send_*",
            "sensitive_mutation",
        ],
        tmp_path,
    )
    assert result.exit_code == 0
    path = tmp_path / "security" / "mcp-policies.json"
    assert path.exists()
    policy = json.loads(path.read_text(encoding="utf-8"))
    assert policy["server_patterns"]["google-gmail"] == {"send_*": "sensitive_mutation"}
    show = _run(["mcp", "permissions"], tmp_path)
    assert "google-gmail" in show.output


def test_mcp_permissions_read_only_toggle(tmp_path: Path) -> None:
    result = _run(["mcp", "permissions", "--read-only-approval", "on"], tmp_path)
    assert result.exit_code == 0
    assert "read-only tools require approval: yes" in result.output
    result = _run(["mcp", "permissions", "--read-only-approval", "off"], tmp_path)
    assert "read-only tools require approval: no" in result.output


def test_mcp_server_tools_lists_registry(tmp_path: Path) -> None:
    result = _run(["mcp", "server", "tools"], tmp_path)
    assert result.exit_code == 0
    assert "apply_patch" in result.output


def test_mcp_server_start_streamable_http_refuses_remote_without_token(
    tmp_path: Path,
) -> None:
    result = _run(
        ["mcp", "server", "start", "--transport", "streamable-http", "--host", "0.0.0.0"],
        tmp_path,
    )
    assert result.exit_code != 0
    assert "refusing" in result.output


def test_auth_google_accounts_use_logout_manage_accounts(tmp_path: Path) -> None:
    from eurus.mcp.providers.google_workspace import build_auth

    auth = build_auth(tmp_path)
    alice = auth.accounts.add("alice@example.com")
    auth.accounts.add("bob@example.com")
    auth.accounts.set_active(alice.account_id)

    result = _run(["auth", "google", "accounts"], tmp_path)
    assert result.exit_code == 0
    assert "alice@example.com" in result.output
    assert "bob@example.com" in result.output

    result = _run(["auth", "google", "status"], tmp_path)
    assert "alice@example.com" in result.output

    result = _run(["auth", "google", "use", "bob@example.com"], tmp_path)
    assert result.exit_code == 0
    assert "Active Google account is now bob@example.com" in result.output

    result = _run(["auth", "google", "use", "ghost@example.com"], tmp_path)
    assert result.exit_code != 0

    result = _run(["auth", "google", "logout", "alice@example.com"], tmp_path)
    assert result.exit_code == 0
    reloaded = build_auth(tmp_path)
    remaining = [item.email for item in reloaded.accounts.list()]
    assert "bob@example.com" in remaining
    assert "alice@example.com" not in remaining


def test_auth_google_logout_all(tmp_path: Path) -> None:
    from eurus.mcp.providers.google_workspace import build_auth

    auth = build_auth(tmp_path)
    auth.accounts.add("alice@example.com")
    auth.accounts.add("bob@example.com")
    result = _run(["auth", "google", "logout", "--all"], tmp_path)
    assert result.exit_code == 0
    assert build_auth(tmp_path).accounts.list() == []


def test_mcp_legacy_commands_still_work(tmp_path: Path) -> None:
    result = _run(["mcp", "add-stdio", "legacy", "python"], tmp_path)
    assert result.exit_code == 0
    config = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert config["servers"][0]["name"] == "legacy"
    result = _run(["mcp", "list"], tmp_path)
    assert "legacy" in result.output
    result = _run(["mcp", "remove", "legacy", "--yes"], tmp_path)
    assert result.exit_code == 0
    config = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert config["servers"] == []
