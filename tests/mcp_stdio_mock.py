from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("mock-stdio")


@server.tool()
def echo(text: str) -> str:
    return f"echo: {text}"


@server.tool()
def boom() -> str:
    raise RuntimeError("mock failure")


@server.tool()
async def slow(seconds: float = 5.0) -> str:
    import asyncio

    await asyncio.sleep(seconds)
    return f"slept {seconds}"


@server.tool()
def count_tools() -> str:
    return "4 tools"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "fail-startup":
        raise SystemExit("mock server refuses to start")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
