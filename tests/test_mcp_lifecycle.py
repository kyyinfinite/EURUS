"""Lifecycle integration tests for the per-server MCP client manager."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from eurus.mcp.manager import MCPServerConfig, MCPToolManager, ServerState
from eurus.models import ToolCall
from eurus.tools.base import ToolContext, ToolRegistry

MOCK_SERVER = Path(__file__).parent / "mcp_stdio_mock.py"
pytestmark = pytest.mark.asyncio


def stdio_config(name: str, **overrides: object) -> MCPServerConfig:
    defaults: dict[str, object] = {
        "name": name,
        "command": sys.executable,
        "args": (str(MOCK_SERVER),),
        "timeout_seconds": 15.0,
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)  # type: ignore[arg-type]


async def test_connect_discovers_and_calls_tools() -> None:
    manager = MCPToolManager([stdio_config("mock")])
    await manager.connect()
    try:
        assert manager.state("mock") is ServerState.CONNECTED
        tools = [name for name in manager._tools if name.startswith("mcp_mock_")]
        assert len(tools) == 4
        registry = ToolRegistry()
        assert manager.register_tools(registry) == 4
        result = await registry.execute(
            ToolCall("c1", "mcp_mock_echo", {"text": "hi"}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert "echo: hi" in result.output
        assert result.is_error is False
    finally:
        await manager.close()
    assert manager.state("mock") is ServerState.DISCONNECTED
    assert manager._tools == {}
    assert manager._sessions == {}


async def test_partial_failure_keeps_healthy_servers() -> None:
    configs = [
        stdio_config("good"),
        MCPServerConfig(
            name="bad",
            command=sys.executable,
            args=(str(MOCK_SERVER), "fail-startup"),
            timeout_seconds=10.0,
        ),
        stdio_config("good2"),
    ]
    manager = MCPToolManager(configs)
    await manager.connect()
    try:
        assert manager.state("good") is ServerState.CONNECTED
        assert manager.state("good2") is ServerState.CONNECTED
        assert manager.state("bad") is ServerState.FAILED
        assert manager.status()[1]["error"]
        registry = ToolRegistry()
        manager.register_tools(registry)
        result = await registry.execute(
            ToolCall("c2", "mcp_good_echo", {"text": "still alive"}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert "still alive" in result.output
    finally:
        await manager.close()


async def test_disabled_server_is_skipped() -> None:
    manager = MCPToolManager([stdio_config("off", enabled=False)])
    await manager.connect()
    assert manager.state("off") is ServerState.DISABLED
    assert manager.connected is True
    await manager.close()


async def test_reconnect_single_server() -> None:
    manager = MCPToolManager([stdio_config("one"), stdio_config("two")])
    await manager.connect()
    registry = ToolRegistry()
    manager.register_tools(registry)
    assert manager.unregister_tools(registry) == 8

    assert await manager.reconnect("one") is True
    assert manager.state("one") is ServerState.CONNECTED
    assert manager.state("two") is ServerState.CONNECTED
    assert len([n for n in manager._tools if n.startswith("mcp_one_")]) == 4

    count = manager.register_tools(registry)
    assert count == 8
    await manager.close()


async def test_timeout_is_enforced() -> None:
    manager = MCPToolManager([stdio_config("slow", timeout_seconds=2.0)])
    await manager.connect()
    try:
        registry = ToolRegistry()
        manager.register_tools(registry)
        result = await registry.execute(
            ToolCall("c3", "mcp_slow_slow", {"seconds": 10.0}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert result.is_error is True
        assert "timed out" in result.output
    finally:
        await manager.close()


async def test_remote_error_surfaces_as_tool_error() -> None:
    manager = MCPToolManager([stdio_config("mock")])
    await manager.connect()
    try:
        registry = ToolRegistry()
        manager.register_tools(registry)
        result = await registry.execute(
            ToolCall("c4", "mcp_mock_boom", {}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert result.is_error is True
        assert "mock failure" in result.output
    finally:
        await manager.close()


async def test_invalid_arguments_rejected_by_schema() -> None:
    manager = MCPToolManager([stdio_config("mock")])
    await manager.connect()
    try:
        registry = ToolRegistry()
        manager.register_tools(registry)
        result = await registry.execute(
            ToolCall("c5", "mcp_mock_echo", {"wrong": 1}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert result.is_error is True
        assert "invalid arguments" in result.output
    finally:
        await manager.close()


async def test_concurrent_connects_are_isolated() -> None:
    configs = [stdio_config(f"s{index}") for index in range(4)]
    manager = MCPToolManager(configs)
    await asyncio.wait_for(manager.connect(), timeout=60.0)
    try:
        for config in configs:
            assert manager.state(config.name) is ServerState.CONNECTED
        total = sum(
            1 for item in manager._tools.values() for _ in [0] if item["server"].startswith("s")
        )
        assert total == 16
    finally:
        await manager.close()


async def test_reconnect_after_server_death() -> None:
    manager = MCPToolManager([stdio_config("revive")])
    await manager.connect()
    registry = ToolRegistry()
    manager.register_tools(registry)

    # Simulate the server dying: drop the session without a clean shutdown.
    manager._sessions.pop("revive")
    conn = manager._connections["revive"]
    conn.state = ServerState.FAILED
    conn.error = "simulated drop"

    result = await registry.execute(
        ToolCall("c6", "mcp_revive_echo", {"text": "back"}),
        ToolContext(Path.cwd(), timeout=20.0),
    )
    assert "back" in result.output
    assert result.is_error is False
    assert manager.state("revive") is ServerState.CONNECTED
    await manager.close()


async def test_close_is_idempotent_and_graceful() -> None:
    manager = MCPToolManager([stdio_config("grace")])
    await manager.connect()
    await manager.close()
    await manager.close()
    assert manager.connected is False
    assert all(row["state"] == "disconnected" for row in manager.status())


async def test_streamable_http_round_trip(http_mock_server: dict[str, object]) -> None:
    url = str(http_mock_server["url"])
    config = MCPServerConfig(
        name="httpmock",
        transport="http",
        url=url,
        timeout_seconds=15.0,
    )
    manager = MCPToolManager([config])
    await manager.connect()
    try:
        assert manager.state("httpmock") is ServerState.CONNECTED
        names = [name for name in manager._tools if name.startswith("mcp_httpmock_")]
        assert "mcp_httpmock_add" in names
        registry = ToolRegistry()
        manager.register_tools(registry)
        result = await registry.execute(
            ToolCall("c7", "mcp_httpmock_add", {"a": 2, "b": 3}),
            ToolContext(Path.cwd(), timeout=10.0),
        )
        assert "5" in result.output
    finally:
        await manager.close()
    assert manager.state("httpmock") is ServerState.DISCONNECTED
