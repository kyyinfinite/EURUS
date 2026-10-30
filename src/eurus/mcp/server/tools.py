"""Bridge between the Eurus ToolRegistry and the MCP server.

External MCP clients reach Eurus tools through the same ToolRegistry lookup
and schema validation used everywhere else; nothing bypasses the execution
boundary. Google OAuth credentials are never registered as tools and cannot
be exposed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, INVALID_REQUEST, ErrorData, TextContent
from mcp.types import Tool as MCPTool

from eurus.mcp.server.access import ServerAccessPolicy
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext, ToolRegistry

SERVER_NAME = "eurus"

ContextFactory = Callable[[str], ToolContext]


def default_context_factory(timeout: float = 120.0) -> ContextFactory:
    def factory(call_id: str) -> ToolContext:
        return ToolContext(workspace=Path.cwd(), timeout=timeout, run_id=call_id)

    return factory


class EurusToolServer:
    """Low-level MCP server exposing allowlisted Eurus tools."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: ServerAccessPolicy,
        *,
        context_factory: ContextFactory | None = None,
        name: str = SERVER_NAME,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.context_factory = context_factory or default_context_factory()
        self.server = Server(name)
        self.server.list_tools()(self._list_tools)  # type: ignore[no-untyped-call]
        self.server.call_tool()(self._call_tool)

    def _exposed(self) -> list[Tool]:
        return self.policy.exposed_tools(self.registry)

    async def _list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.parameters,
            )
            for tool in self._exposed()
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if not isinstance(arguments, dict):
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="tool arguments must be a JSON object")
            )
        tool = self.registry.get(name)
        if tool is None:
            raise McpError(ErrorData(code=INVALID_REQUEST, message=f"unknown tool: {name}"))
        allowed, reason = self.policy.check(tool)
        if not allowed:
            raise McpError(ErrorData(code=INVALID_REQUEST, message=f"access denied: {reason}"))
        call_id = uuid.uuid4().hex
        context = self.context_factory(call_id)
        result = await self.registry.execute(ToolCall(call_id, name, arguments), context)
        return [TextContent(type="text", text=result.output)]
