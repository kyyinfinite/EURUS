"""Integration tests for the built-in Eurus MCP server.

Covers STDIO and streamable-HTTP transports, bearer authentication, tool
allowlisting, permission enforcement, schema validation and execution through
the ToolRegistry boundary.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from eurus.mcp.manager import MCPServerConfig, MCPToolManager
from eurus.mcp.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    EurusMCPServer,
    EurusToolServer,
    HTTPServerError,
    ServerAccessPolicy,
    default_access_path,
    validate_http_security,
)
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext, ToolRegistry


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(arguments: dict, context: ToolContext) -> str:
        return f"echo: {arguments.get('text', '')}"

    async def risky(arguments: dict, context: ToolContext) -> str:
        return f"mutated: {arguments.get('text', '')}"

    registry.register(
        Tool(
            "echo_tool",
            "Echo the text argument",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            echo,
        )
    )
    registry.register(
        Tool(
            "risky_tool",
            "Mutates state",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            risky,
            mutates=True,
            requires_approval=True,
        )
    )
    return registry


@pytest.mark.asyncio
async def test_nothing_exposed_by_default(tmp_path: Path) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path)
    tool_server = server.tool_server()
    tools = await tool_server._list_tools()
    assert tools == []
    assert not default_access_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_allowlist_exposes_only_named_tools(tmp_path: Path) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    tools = await server.tool_server()._list_tools()
    assert [item.name for item in tools] == ["echo_tool"]
    assert tools[0].inputSchema["required"] == ["text"]


@pytest.mark.asyncio
async def test_mutating_tools_denied_without_explicit_flag(tmp_path: Path) -> None:
    server = EurusMCPServer.create(
        build_registry(), home=tmp_path, allowed_tools=["risky_tool", "echo_tool"]
    )
    tools = await server.tool_server()._list_tools()
    assert [item.name for item in tools] == ["echo_tool"]

    server.policy.allow_mutating = True
    tools = await server.tool_server()._list_tools()
    assert sorted(item.name for item in tools) == ["echo_tool", "risky_tool"]


@pytest.mark.asyncio
async def test_call_tool_executes_through_registry(tmp_path: Path) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    tool_server = server.tool_server()
    content = await tool_server._call_tool("echo_tool", {"text": "hello"})
    assert content[0].text == "echo: hello"


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_and_denied(tmp_path: Path) -> None:
    from mcp.shared.exceptions import McpError

    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    tool_server = server.tool_server()
    with pytest.raises(McpError, match="unknown tool"):
        await tool_server._call_tool("nope", {})
    with pytest.raises(McpError, match="not allowlisted"):
        await tool_server._call_tool("risky_tool", {"text": "x"})


@pytest.mark.asyncio
async def test_call_tool_rejects_invalid_arguments(tmp_path: Path) -> None:
    from mcp.shared.exceptions import McpError

    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    tool_server = server.tool_server()
    # schema violations surface as error results from the registry boundary
    content = await tool_server._call_tool("echo_tool", {"wrong": 1})
    assert content[0].text.startswith("error: invalid arguments")
    with pytest.raises(McpError, match="JSON object"):
        await tool_server._call_tool("echo_tool", "not-a-dict")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_stdio_transport_round_trip(tmp_path: Path) -> None:
    script = Path("/tmp/eurus_mcp_server_stdio_test.py")
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, sys
            from pathlib import Path
            sys.path.insert(0, {str(Path.cwd() / "src")!r})
            from eurus.mcp.server import EurusMCPServer, run_stdio
            from eurus.models import ToolCall
            from eurus.tools.base import Tool, ToolContext, ToolRegistry

            registry = ToolRegistry()

            async def echo(arguments, context):
                return "echo: " + str(arguments.get("text", ""))

            registry.register(Tool("echo_tool", "Echo", {{
                "type": "object",
                "properties": {{"text": {{"type": "string"}}}},
                "required": ["text"],
            }}, echo))

            async def main() -> None:
                server = EurusMCPServer.create(
                    registry, home=Path({str(tmp_path)!r}), allowed_tools=["echo_tool"]
                )
                await run_stdio(server.tool_server())

            asyncio.run(main())
            """
        )
    )
    config = MCPServerConfig(
        name="eurus-self",
        command=sys.executable,
        args=(str(script),),
        timeout_seconds=15.0,
    )
    client = MCPToolManager([config])
    await client.connect()
    try:
        assert client.state("eurus-self").value == "connected"
        assert sorted(client._tools) == ["mcp_eurus_self_echo_tool"]
        registry = ToolRegistry()
        client.register_tools(registry)
        result = await registry.execute(
            ToolCall("c", "mcp_eurus_self_echo_tool", {"text": "stdio works"}),
            ToolContext(tmp_path, timeout=10.0),
        )
        assert "echo: stdio works" in result.output
        assert result.is_error is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_transport_requires_token(tmp_path: Path) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    serve = asyncio.create_task(
        serve_http_with_port(server.tool_server(), port=19701, token="secret-1")
    )
    await asyncio.sleep(0.8)
    try:
        async with httpx.AsyncClient() as http:
            response = await http.get("http://127.0.0.1:19701/mcp")
            assert response.status_code == 401
            response = await http.get(
                "http://127.0.0.1:19701/mcp",
                headers={"Authorization": "Bearer wrong"},
            )
            assert response.status_code == 401
    finally:
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve


@pytest.mark.asyncio
async def test_http_transport_round_trip(tmp_path: Path) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path, allowed_tools=["echo_tool"])
    serve = asyncio.create_task(
        serve_http_with_port(server.tool_server(), port=19702, token="secret-2")
    )
    await asyncio.sleep(0.8)
    client = MCPToolManager(
        [
            MCPServerConfig(
                name="eurus-http",
                transport="http",
                url="http://127.0.0.1:19702/mcp",
                headers={"Authorization": "Bearer secret-2"},
                timeout_seconds=15.0,
            )
        ]
    )
    try:
        await client.connect()
        assert client.state("eurus-http").value == "connected"
        assert sorted(client._tools) == ["mcp_eurus_http_echo_tool"]
        registry = ToolRegistry()
        client.register_tools(registry)
        result = await registry.execute(
            ToolCall("c", "mcp_eurus_http_echo_tool", {"text": "http works"}),
            ToolContext(tmp_path, timeout=10.0),
        )
        assert "echo: http works" in result.output
    finally:
        await client.close()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve


async def serve_http_with_port(tool_server: EurusToolServer, port: int, token: str) -> None:
    from eurus.mcp.server.transport import serve_http as _serve

    await _serve(tool_server, host="127.0.0.1", port=port, token=token)


def test_access_policy_persistence(tmp_path: Path) -> None:
    path = tmp_path / "server-access.json"
    policy = ServerAccessPolicy(allowed_tools={"a"}, allowed_patterns=("memory_*",))
    policy.save(path)
    loaded = ServerAccessPolicy.load(path)
    assert loaded.allowed_tools == {"a"}
    assert loaded.allowed_patterns == ("memory_*",)
    assert loaded.allow("a") and loaded.allow("memory_search")
    assert not loaded.allow("shell")


def test_security_validation_refuses_unauthenticated_exposure() -> None:
    with pytest.raises(HTTPServerError, match="unauthenticated"):
        validate_http_security("0.0.0.0", "token", auth_disabled=True)
    with pytest.raises(HTTPServerError, match="without a token"):
        validate_http_security("192.168.1.5", "", auth_disabled=False)
    validate_http_security("127.0.0.1", "token", auth_disabled=False)
    validate_http_security("localhost", "token", auth_disabled=True)


def test_defaults_are_loopback() -> None:
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8765


def test_token_generation_is_ephemeral(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = EurusMCPServer.create(build_registry(), home=tmp_path)
    first = server.resolve_token(None)
    second = server.resolve_token(None)
    assert first != second
    assert len(first) >= 32
    assert "Generated a one-time MCP server token" in capsys.readouterr().err
