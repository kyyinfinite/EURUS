"""Standalone mock MCP server used by the test suite.

Runs a FastMCP server over stdio with a couple of tools, including one that
fails and one that echoes. Launched as a subprocess by the lifecycle tests
exactly like a real third-party stdio MCP server.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("mock-stdio")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@server.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("mock failure")


@server.tool()
async def slow(seconds: float = 5.0) -> str:
    """Sleeps before answering; used for timeout tests."""
    import asyncio

    await asyncio.sleep(seconds)
    return f"slept {seconds}"


@server.tool()
def count_tools() -> str:
    """Reports how many tools this server exposes."""
    return "4 tools"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "fail-startup":
        raise SystemExit("mock server refuses to start")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
