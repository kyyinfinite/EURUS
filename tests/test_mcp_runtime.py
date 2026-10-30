"""Tests for the centralized MCP runtime assembly."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eurus.mcp.manager import MCPToolManager, ServerState
from eurus.mcp.runtime import MCPRuntime


def test_empty_home_builds_empty_runtime(tmp_path: Path) -> None:
    runtime = MCPRuntime.build(tmp_path)
    assert runtime.manager.configs == {}
    assert runtime.warnings == []
    assert runtime.google is not None
    assert runtime.diagnostics()["google"]["enabled_services"] == []


def test_external_servers_keep_working(tmp_path: Path) -> None:
    (tmp_path / "mcp.json").write_text(
        json.dumps({"servers": [{"name": "ext", "command": "python", "args": ["-c", "pass"]}]}),
        encoding="utf-8",
    )
    runtime = MCPRuntime.build(tmp_path)
    assert list(runtime.manager.configs) == ["ext"]
    assert runtime.manager.configs["ext"].command == "python"


def test_invalid_external_config_still_raises(tmp_path: Path) -> None:
    (tmp_path / "mcp.json").write_text('{"servers": [{"name": "x", "extra": 1}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown MCP server fields"):
        MCPRuntime.build(tmp_path)


def test_google_enabled_without_login_warns_but_starts(tmp_path: Path) -> None:
    (tmp_path / "integrations").mkdir()
    (tmp_path / "integrations" / "google-workspace.json").write_text(
        json.dumps({"services": {"gmail": {"enabled": True}}}), encoding="utf-8"
    )
    runtime = MCPRuntime.build(tmp_path)
    assert "google-gmail" not in runtime.manager.configs
    assert any("login" in warning for warning in runtime.warnings)
    assert runtime.diagnostics()["google"]["login_required"] is True


def test_google_with_account_adds_configs_and_auth(tmp_path: Path) -> None:
    from eurus.mcp.providers.google_workspace import build_auth

    (tmp_path / "integrations").mkdir()
    (tmp_path / "integrations" / "google-workspace.json").write_text(
        json.dumps({"services": {"gmail": {"enabled": True}}}), encoding="utf-8"
    )
    auth = build_auth(tmp_path)
    auth.accounts.add("alice@example.com")
    (tmp_path / "mcp.json").write_text(
        json.dumps({"servers": [{"name": "ext", "command": "python"}]}), encoding="utf-8"
    )
    runtime = MCPRuntime.build(tmp_path)
    assert list(runtime.manager.configs) == ["ext", "google-gmail"]
    assert runtime.manager._auth_provider is not None


def test_google_build_failure_never_blocks_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "integrations").mkdir()
    (tmp_path / "integrations" / "google-workspace.json").write_text(
        json.dumps({"services": {"gmail": {"enabled": True}}}), encoding="utf-8"
    )

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("credential store exploded")

    monkeypatch.setattr("eurus.mcp.runtime.build_auth", boom)
    runtime = MCPRuntime.build(tmp_path)
    assert runtime.google is None
    assert any("credential store exploded" in warning for warning in runtime.warnings)
    assert runtime.manager.configs == {}


@pytest.mark.asyncio
async def test_runtime_connect_register_unregister_with_stdio_mock(tmp_path: Path) -> None:
    mock = Path(__file__).parent / "mcp_stdio_mock.py"
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "mock",
                        "command": sys.executable,
                        "args": [str(mock)],
                        "timeout_seconds": 15.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    from eurus.tools.base import ToolRegistry

    async with MCPRuntime.build(tmp_path) as runtime:
        assert runtime.manager.state("mock") is ServerState.CONNECTED
        registry = ToolRegistry()
        assert runtime.register_tools(registry) == 4
        tool = registry.get("mcp_mock_echo")
        assert tool is not None
        assert tool.mutates is True  # external servers stay fail-closed
        assert tool.requires_approval is True
        assert runtime.unregister_tools(registry) == 4
        assert registry.get("mcp_mock_echo") is None


def test_connect_timeout_hint_covers_all_servers(tmp_path: Path) -> None:
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "a", "command": "x", "timeout_seconds": 30},
                    {"name": "b", "command": "y", "timeout_seconds": 60},
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = MCPRuntime.build(tmp_path)
    assert runtime.connect_timeout_hint() >= 60 * 3


def test_mcp_property_alias(tmp_path: Path) -> None:
    runtime = MCPRuntime.build(tmp_path)
    assert isinstance(runtime.mcp, MCPToolManager)
