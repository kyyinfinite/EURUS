"""Tool and MCP server management commands."""

from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.mcp.manager import MCPServerConfig, MCPToolManager
from eurus.tools.builtin import default_registry


@main.command("tools")
@click.pass_context
def tools_command(ctx: click.Context) -> None:
    from eurus.browser.integration import ensure_browser_tools

    config = _state(ctx).config
    registry = default_registry(include_capability_tools=True)
    ensure_browser_tools(registry, config.browser)
    table = Table(title="tools")
    for column in ("Name", "Changes things", "Description"):
        table.add_column(column)
    for item in registry.all():
        table.add_row(item.name, "yes" if item.mutates else "no", item.description)
    Console().print(table)


@main.group("mcp")
def mcp_group() -> None:
    pass


@mcp_group.command("list")
def mcp_list() -> None:

    manager = MCPToolManager.load(paths.home_dir() / "mcp.json")
    table = Table(title="MCP servers")
    for column in ("Name", "Transport", "Enabled", "Configured"):
        table.add_column(column)
    for item in manager.status():
        table.add_row(
            str(item["name"]),
            str(item["transport"]),
            "yes" if item["enabled"] else "no",
            "connected" if item["connected"] else "not connected",
        )
    Console().print(table)


@mcp_group.command("add-stdio")
@click.argument("name")
@click.argument("command")
@click.option(
    "--arg",
    "arguments",
    multiple=True,
    help="Argument passed to the server; repeat as needed.",
)
def mcp_add_stdio(name: str, command: str, arguments: tuple[str, ...]) -> None:

    path = paths.home_dir() / "mcp.json"
    manager = MCPToolManager.load(path)
    if name in manager.configs:
        raise click.ClickException(f"MCP server already exists: {name}")
    configs = [
        *manager.configs.values(),
        MCPServerConfig(name=name, command=command, args=arguments),
    ]
    MCPToolManager.save(path, configs)
    click.echo(f"Configured MCP server '{name}'. Review its command before connecting.")


@mcp_group.command("add-http")
@click.argument("name")
@click.argument("url")
def mcp_add_http(name: str, url: str) -> None:

    path = paths.home_dir() / "mcp.json"
    manager = MCPToolManager.load(path)
    if name in manager.configs:
        raise click.ClickException(f"MCP server already exists: {name}")
    configs = [
        *manager.configs.values(),
        MCPServerConfig(name=name, transport="http", url=url),
    ]
    MCPToolManager.save(path, configs)
    click.echo(
        f"Configured MCP server '{name}'. Authentication headers are not set by this command."
    )


@mcp_group.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def mcp_remove(name: str, yes: bool) -> None:

    path = paths.home_dir() / "mcp.json"
    manager = MCPToolManager.load(path)
    if name not in manager.configs:
        raise click.ClickException(f"Unknown MCP server: {name}")
    if not yes:
        click.confirm(f"Remove MCP server '{name}'?", abort=True)
    MCPToolManager.save(path, [item for key, item in manager.configs.items() if key != name])
    click.echo(f"Removed MCP server '{name}'.")


@mcp_group.command("test")
@click.argument("name", required=False)
def mcp_test(name: str | None) -> None:

    async def run_test() -> None:
        manager = MCPToolManager.load(paths.home_dir() / "mcp.json")
        if name is not None:
            if name not in manager.configs:
                raise click.ClickException(f"Unknown MCP server: {name}")
            manager.configs = {name: manager.configs[name]}
        await manager.connect()
        try:
            table = Table(title="MCP connection test")
            for column in ("Name", "Transport", "Connected", "Tools"):
                table.add_column(column)
            for item in manager.status():
                table.add_row(
                    str(item["name"]),
                    str(item["transport"]),
                    "yes" if item["connected"] else "no",
                    str(item["tools"]),
                )
            Console().print(table)
        finally:
            await manager.close()

    try:
        asyncio.run(run_test())
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
