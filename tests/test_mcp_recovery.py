from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eurus.mcp.manager import MCPServerConfig, MCPToolManager, ServerState
from eurus.tools.base import ToolRegistry

pytestmark = pytest.mark.asyncio

MOCK_SERVER = Path(__file__).parent / "mcp_stdio_mock.py"


def _config(name: str = "fake", timeout: float = 5.0) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=(str(MOCK_SERVER), "fail-startup"),
        timeout_seconds=timeout,
    )


def _fake_session(tools: list[str] | None = None, fail: bool = False) -> Any:
    async def list_tools() -> Any:
        if fail:
            raise RuntimeError("session is dead")
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name,
                    description=f"fake {name}",
                    inputSchema={"type": "object", "properties": {}},
                    annotations=None,
                )
                for name in tools or []
            ]
        )

    return SimpleNamespace(list_tools=list_tools)


async def test_connect_retries_failed_servers_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MCPToolManager([_config()])
    spawns: list[str] = []
    sleeps: list[float] = []
    real_spawn = manager._spawn

    def counted_spawn(conn: Any) -> None:
        spawns.append(conn.config.name)
        real_spawn(conn)

    monkeypatch.setattr(manager, "_spawn", counted_spawn)
    monkeypatch.setattr("eurus.mcp.manager._RETRY_DELAYS", (0.01, 0.02), raising=False)
    original_sleep = asyncio.sleep

    async def recorded_sleep(delay: float) -> None:
        sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recorded_sleep)
    try:
        await manager.connect()
    finally:
        await manager.close()
    assert manager.state("fake") is not ServerState.CONNECTED
    assert manager.state("fake") is not ServerState.CONNECTING
    assert spawns.count("fake") >= 2
    assert len(sleeps) >= 1
    assert all(delay <= 0.02 for delay in sleeps)


async def test_connect_retry_recovers_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MCPToolManager([_config()])
    attempts: list[int] = []

    async def flaky_open(conn: Any, stack: Any) -> Any:
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("not ready yet")
        return _fake_session(["probe"])

    monkeypatch.setattr(MCPToolManager, "_open", staticmethod(flaky_open))
    monkeypatch.setattr("eurus.mcp.manager._RETRY_DELAYS", (0.0,), raising=False)
    try:
        await manager.connect()
        assert manager.state("fake") is ServerState.CONNECTED
        assert attempts == [1, 1]
    finally:
        await manager.close()


async def test_register_tools_before_connect_registers_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MCPToolManager([_config()])
    gate: list[int] = []

    async def gated_open(conn: Any, stack: Any) -> Any:
        gate.append(1)
        if len(gate) < 2:
            raise RuntimeError("slow start")
        return _fake_session(["late_tool"])

    monkeypatch.setattr(MCPToolManager, "_open", staticmethod(gated_open))
    monkeypatch.setattr("eurus.mcp.manager._RETRY_DELAYS", (0.0,), raising=False)
    registry = ToolRegistry()
    assert manager.register_tools(registry) == 0
    assert "mcp_fake_late_tool" not in registry
    try:
        await manager.connect()
        assert manager.state("fake") is ServerState.CONNECTED
        assert "mcp_fake_late_tool" in registry
        tool = registry.get("mcp_fake_late_tool")
        assert tool is not None
        assert tool.mutates is True
    finally:
        await manager.close()


async def test_register_tools_unconnected_does_not_raise() -> None:
    manager = MCPToolManager([_config()])
    registry = ToolRegistry()
    count = manager.register_tools(registry)
    assert count == 0
    await manager.close()


async def test_health_check_marks_done_task_disconnected() -> None:
    manager = MCPToolManager([_config()])
    conn = manager._connections["fake"]
    conn.state = ServerState.CONNECTED
    manager._sessions["fake"] = _fake_session(["probe"])
    manager._tools["mcp_fake_probe"] = {"server": "fake", "remote_name": "probe"}
    conn.owned_tools.add("mcp_fake_probe")
    conn.task = asyncio.Future()
    conn.task.set_result(None)
    report = await manager.health_check()
    assert report["fake"] == "disconnected"
    assert manager.state("fake") is ServerState.DISCONNECTED
    assert "fake" not in manager._sessions
    await manager.close()


async def test_health_check_marks_unreachable_session() -> None:
    manager = MCPToolManager([_config()])
    conn = manager._connections["fake"]
    conn.state = ServerState.CONNECTED
    manager._sessions["fake"] = _fake_session(fail=True)
    conn.owned_tools.add("mcp_fake_probe")
    hanging = asyncio.Event()

    async def parked() -> None:
        await hanging.wait()

    conn.task = asyncio.create_task(parked())
    conn.stop = asyncio.Event()
    try:
        report = await manager.health_check()
        assert report["fake"] == "unreachable"
        assert manager.state("fake") is ServerState.DISCONNECTED
        assert "fake" not in manager._sessions
    finally:
        hanging.set()
        with contextlib.suppress(BaseException):
            await conn.task
        await manager.close()
