from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from eurus.mcp.manager import MCPServerConfig, MCPToolManager, ServerState
from eurus.mcp.policy import MCPPolicy
from eurus.models import ToolCall
from eurus.tools.base import ToolContext, ToolRegistry


class _FakeSession:
    def __init__(self, *, failures: int = 0, payload: str = "ok") -> None:
        self.failures = failures
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("Stream closed before response completed")
        return _FakeResult(self.payload)

    async def list_tools(self) -> Any:
        raise AssertionError("not used in these tests")


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.content = [type("Block", (), {"text": text, "type": "text"})()]
        self.structuredContent = {}
        self.isError = False


async def _prepare_manager(
    monkeypatch: pytest.MonkeyPatch, readonly_tool: str
) -> tuple[MCPToolManager, ToolRegistry, dict[str, Any]]:
    manager = MCPToolManager(
        [MCPServerConfig(name="google-test", transport="stdio", command="noop")],
        policy=MCPPolicy(),
    )
    item = {
        "server": "google-test",
        "remote_name": readonly_tool,
        "description": "fake",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    exposed = manager._exposed_name("google-test", readonly_tool)
    manager._tools[exposed] = item
    conn = manager._connections["google-test"]
    conn.state = ServerState.CONNECTED
    manager._connected = True

    registry = ToolRegistry()
    manager.register_tools(registry)

    async def instant_reconnect(name: str) -> bool:
        manager._sessions[name] = manager._sessions.get(name) or _FakeSession()
        conn = manager._connections[name]
        conn.state = ServerState.CONNECTED
        return True

    monkeypatch.setattr(manager, "reconnect", instant_reconnect)
    return manager, registry, {"exposed": exposed}


async def test_missing_tool_raises_clear_error_not_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry, info = await _prepare_manager(monkeypatch, "search_threads")
    del manager._tools[info["exposed"]]

    async def fail_reconnect(name: str) -> bool:
        return False

    monkeypatch.setattr(manager, "reconnect", fail_reconnect)
    result = await registry.execute(
        ToolCall("c1", info["exposed"], {}),
        ToolContext(Path.cwd(), timeout=5.0),
    )
    assert result.is_error is True
    assert "KeyError" not in result.output
    assert "no longer available" in result.output
    await manager.close()


async def test_readonly_tool_retries_once_after_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry, info = await _prepare_manager(monkeypatch, "search_threads")
    session = _FakeSession(failures=1, payload="recovered-data")
    manager._sessions["google-test"] = session

    def fake_reconnect_factory(mgr: MCPToolManager):
        async def fake_reconnect(name: str) -> bool:
            conn = mgr._connections[name]
            conn.state = ServerState.CONNECTED
            return True

        return fake_reconnect

    monkeypatch.setattr(manager, "reconnect", fake_reconnect_factory(manager))
    result = await registry.execute(
        ToolCall("c2", info["exposed"], {"q": "hello"}),
        ToolContext(Path.cwd(), timeout=5.0),
    )
    assert result.is_error is False
    assert "recovered-data" in result.output
    assert session.calls == [("search_threads", {"q": "hello"})] * 2, (
        "retry must reuse the original arguments"
    )
    await manager.close()


async def test_mutating_tool_is_never_auto_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry, info = await _prepare_manager(monkeypatch, "create_draft")
    policy = MCPPolicy()
    policy.set_server_rule("google-test", "*", "reversible_write")
    manager._policy = policy
    session = _FakeSession(failures=1, payload="should-not-happen")
    manager._sessions["google-test"] = session
    registry.unregister(info["exposed"])
    manager._registered_handlers.pop(info["exposed"], None)
    manager._registered_names.discard(info["exposed"])
    manager.register_tools(registry)

    monkeypatch.setattr(
        manager,
        "reconnect",
        (lambda mgr: lambda name: _async_true(mgr, name))(manager),
    )
    result = await registry.execute(
        ToolCall("c3", info["exposed"], {"text": "x"}),
        ToolContext(Path.cwd(), timeout=5.0),
    )
    assert result.is_error is True
    assert "NOT retried automatically" in result.output
    assert len(session.calls) == 1, "mutating calls must never be retried"
    await manager.close()


async def _async_true(mgr: MCPToolManager, name: str) -> bool:
    mgr._connections[name].state = ServerState.CONNECTED
    return True


async def test_reauth_only_for_credential_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, registry, info = await _prepare_manager(monkeypatch, "search_threads")

    class _Forbidden(Exception):
        pass

    session = _ForbiddenSession()
    manager._sessions["google-test"] = session
    result = await registry.execute(
        ToolCall("c4", info["exposed"], {}),
        ToolContext(Path.cwd(), timeout=5.0),
    )
    assert result.is_error is True
    assert manager.state("google-test") is ServerState.FAILED
    await manager.close()


class _ForbiddenSession:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("403 PERMISSION_DENIED: service not enabled")


async def test_register_tools_is_idempotent_and_never_partially_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, registry, info = await _prepare_manager(monkeypatch, "search_threads")
    first = manager.register_tools(registry)
    assert first == 0

    new_exposed = manager._exposed_name("google-test", "get_message")
    manager._tools[new_exposed] = {
        "server": "google-test",
        "remote_name": "get_message",
        "description": "fake",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    count = manager.register_tools(registry)
    assert count == 1
    assert registry.get(new_exposed) is not None

    foreign_exposed = manager._exposed_name("google-test", "send_message")
    manager._tools[foreign_exposed] = {
        "server": "google-test",
        "remote_name": "send_message",
        "description": "fake",
        "schema": {"type": "object", "properties": {}},
        "annotations": None,
    }
    from eurus.tools.base import Tool

    async def _foreign(arguments: dict[str, Any], context: ToolContext) -> str:
        return ""

    registry.register(
        Tool(name=foreign_exposed, description="foreign", parameters={}, handler=_foreign)
    )
    count = manager.register_tools(registry)
    assert count == 0
    assert "conflicts" in manager._connections["google-test"].error
    await manager.close()


def test_long_tool_names_never_collide() -> None:
    long_a = "a" * 70
    long_b = "a" * 69 + "b"
    name_a = MCPToolManager._exposed_name("srv", long_a)
    name_b = MCPToolManager._exposed_name("srv", long_b)
    assert name_a != name_b
    assert len(name_a) <= 64 and len(name_b) <= 64
    short = MCPToolManager._exposed_name("srv", "get")
    assert short == "mcp_srv_get"


async def test_close_is_responsive_while_reconnect_is_pending() -> None:
    config = MCPServerConfig(name="hang", transport="stdio", command="noop")
    manager = MCPToolManager([config])
    manager._connected = True
    conn = manager._connections["hang"]
    conn.task = asyncio.create_task(asyncio.sleep(30), name="fake-task")
    conn.stop = asyncio.Event()
    started = asyncio.get_running_loop().time()
    conn.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await conn.task
    conn.task = None
    await asyncio.wait_for(manager.close(), timeout=5.0)
    assert asyncio.get_running_loop().time() - started < 5.0


def test_status_includes_error_code() -> None:
    manager = MCPToolManager([MCPServerConfig(name="s", transport="stdio", command="x")])
    rows = manager.status()
    assert rows[0]["error_code"] == ""
    assert json.dumps(rows)
