"""Tool and MCP server management commands."""

from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.mcp.manager import MCPServerConfig, MCPToolManager
from eurus.mcp.policy import MCPPolicy, default_policy_path
from eurus.mcp.providers.google_workspace import build_auth, build_provider
from eurus.mcp.providers.google_workspace.scopes import SERVICE_SCOPES
from eurus.mcp.runtime import MCPRuntime
from eurus.mcp.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    TOKEN_ENV_VAR,
    EurusMCPServer,
    HTTPServerError,
    default_access_path,
    is_loopback,
    run_stdio,
    serve_http,
    validate_http_security,
)
from eurus.tools.base import ToolRegistry
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


@mcp_group.group("google")
def mcp_google() -> None:
    pass


@mcp_google.command("list")
def mcp_google_list() -> None:
    provider = build_provider(paths.home_dir())
    table = Table(title="Google Workspace services")
    for column in ("Service", "Label", "Enabled", "Endpoint", "Scopes"):
        table.add_column(column)
    for row in provider.service_status():
        table.add_row(
            str(row["service"]),
            str(row["label"]),
            "yes" if row["enabled"] else "no",
            str(row["endpoint"]),
            str(len(SERVICE_SCOPES.get(str(row["service"]), ()))),
        )
    Console().print(table)
    click.echo("Enable a service with: eurus mcp google enable <service>")


@mcp_google.command("enable")
@click.argument("service")
def mcp_google_enable(service: str) -> None:
    try:
        enabled = build_provider(paths.home_dir()).enable(service)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Enabled {enabled.label} ({enabled.server_name}).")
    scopes = SERVICE_SCOPES.get(service, ())
    for scope in scopes:
        click.echo(f"  scope: {scope}")
    if enabled.requires_workspace_account:
        click.echo(f"  note: {enabled.notes}")
    click.echo("Re-run 'eurus auth google login' to grant the new scopes.")


@mcp_google.command("disable")
@click.argument("service")
def mcp_google_disable(service: str) -> None:
    try:
        disabled = build_provider(paths.home_dir()).disable(service)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Disabled {disabled.label}.")


@mcp_google.command("status")
def mcp_google_status() -> None:
    provider = build_provider(paths.home_dir())
    auth = build_auth(paths.home_dir())
    provider.auth = auth
    active = auth.accounts.active
    click.echo(f"OAuth client configured: {'yes' if auth.client_configured else 'no'}")
    click.echo(f"Token storage: {auth.store.backend_name}")
    click.echo(f"Active account: {active.email if active else 'none'}")
    table = Table(title="Google Workspace status")
    for column in ("Service", "Enabled", "State", "Account", "Tools", "Notes"):
        table.add_column(column)
    for row in provider.service_status():
        table.add_row(
            str(row["service"]),
            "yes" if row["enabled"] else "no",
            str(row["state"]),
            str(row["account"] or "-"),
            str(row["tools"]),
            str(row["notes"] or ""),
        )
    Console().print(table)
    if provider.settings.enabled_ids() and not active:
        click.echo("Services are enabled but no account is logged in: eurus auth google login")


@mcp_google.command("test")
def mcp_google_test() -> None:
    async def run_test() -> None:
        provider = build_provider(paths.home_dir())
        auth = build_auth(paths.home_dir())
        provider.auth = auth
        configs = provider.server_configs()
        if not configs:
            enabled = provider.settings.enabled_ids()
            if not enabled:
                click.echo("No Google services enabled. Try: eurus mcp google enable gmail")
                return
            click.echo(
                "No account available for the enabled services. Run: eurus auth google login"
            )
            return
        manager = MCPToolManager(configs, auth_provider=provider.auth_provider, policy=MCPPolicy())
        await manager.connect()
        try:
            table = Table(title="Google Workspace connection test")
            for column in ("Service", "State", "Tools", "Error"):
                table.add_column(column)
            states = {row["name"]: row for row in manager.status()}
            for row in provider.service_status(states):
                table.add_row(
                    str(row["service"]),
                    str(row["state"]),
                    str(row["tools"]),
                    str(row["error"] or ""),
                )
            Console().print(table)
        finally:
            await manager.close()

    try:
        asyncio.run(run_test())
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@mcp_group.group("server")
def mcp_server_group() -> None:
    pass


@mcp_server_group.command("start")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "streamable-http"]),
    default="stdio",
    show_default=True,
)
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="HTTP bind address.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="HTTP bind port.")
@click.option("--token", default=None, help="Bearer token for HTTP clients.")
@click.option("--no-auth", is_flag=True, help="Disable HTTP auth (loopback only).")
@click.option(
    "--tool",
    "tools",
    multiple=True,
    help="Expose this tool; repeat as needed. Nothing is exposed by default.",
)
@click.option(
    "--allow-mutating",
    is_flag=True,
    help="Allow exposing mutating/approval-gated tools.",
)
def mcp_server_start(
    transport: str,
    host: str,
    port: int,
    token: str | None,
    no_auth: bool,
    tools: tuple[str, ...],
    allow_mutating: bool,
) -> None:
    """Run Eurus itself as an MCP server for external clients."""
    registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
    server = EurusMCPServer.create(
        registry,
        home=paths.home_dir(),
        allowed_tools=list(tools),
        allow_mutating=allow_mutating or None,
    )
    exposed = server.policy.exposed_tools(registry)
    if not exposed:
        click.echo(
            "No tools exposed. Pass --tool <name> (see 'eurus mcp server tools') or edit\n"
            f"{default_access_path(paths.home_dir())}",
            err=True,
        )
    if transport == "stdio":
        asyncio.run(run_stdio(server.tool_server()))
        return
    import os as _os

    explicit = token or _os.environ.get(TOKEN_ENV_VAR, "").strip()
    effective = explicit
    if no_auth:
        try:
            validate_http_security(host, effective, auth_disabled=True)
        except HTTPServerError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        if not effective:
            if not is_loopback(host):
                raise click.ClickException(
                    f"refusing to expose the MCP endpoint on {host} without a token; "
                    f"pass --token or set {TOKEN_ENV_VAR}"
                )
            effective = server.resolve_token(None)
        try:
            validate_http_security(host, effective, auth_disabled=False)
        except HTTPServerError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Serving Eurus MCP on http://{host}:{port}/mcp", err=True)
    try:
        asyncio.run(serve_http(server.tool_server(), host=host, port=port, token=effective))
    except KeyboardInterrupt:
        click.echo("MCP server stopped.", err=True)


@mcp_server_group.command("tools")
def mcp_server_tools() -> None:
    registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
    table = Table(title="Eurus tools available for MCP exposure")
    for column in ("Name", "Mutating", "Approval", "Description"):
        table.add_column(column)
    for item in registry.all():
        table.add_row(
            item.name,
            "yes" if item.mutates else "no",
            "yes" if item.requires_approval else "no",
            item.description[:80],
        )
    Console().print(table)
    click.echo("Expose with: eurus mcp server start --tool <name> ...")


@mcp_group.command("doctor")
def mcp_doctor() -> None:
    """Diagnose the MCP stack: SDK, servers, policy, Google auth."""
    problems: list[str] = []

    try:
        import importlib.metadata

        version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        version = "missing"
        problems.append("the 'mcp' package is not installed")
    click.echo(f"MCP SDK: {version}")

    runtime = MCPRuntime.build(paths.home_dir())
    for warning in runtime.warnings:
        click.echo(f"warning: {warning}")
    for row in runtime.manager.status():
        state = str(row["state"])
        click.echo(
            f"server {row['name']}: transport={row['transport']} state={state} tools={row['tools']}"
        )
        if row["error"]:
            click.echo(f"  error: {row['error']}")
        if state == "failed":
            problems.append(f"MCP server '{row['name']}' failed")

    policy_path = default_policy_path(paths.home_dir())
    click.echo(f"policy file: {policy_path} ({'present' if policy_path.exists() else 'default'})")

    google = runtime.google
    if google is not None:
        diagnostics = google.diagnostics()
        click.echo(f"google oauth client configured: {diagnostics['oauth_ready']}")
        storage = google.auth.store.backend_name if google.auth else "-"
        click.echo(f"google token storage: {storage}")
        click.echo(f"google active account: {diagnostics['active_account'] or 'none'}")
        enabled = ", ".join(diagnostics["enabled_services"]) or "none"
        click.echo(f"google enabled services: {enabled}")
        if diagnostics["login_required"]:
            problems.append("Google services enabled but no account logged in")
    else:
        click.echo("google workspace: unavailable")

    if problems:
        for problem in problems:
            click.echo(f"problem: {problem}")
        raise click.ClickException("MCP doctor found problems")
    click.echo("MCP stack looks healthy.")


@mcp_group.command("tools")
def mcp_runtime_tools() -> None:
    """List the tools MCP servers would register right now."""

    async def run() -> list[tuple[str, str, str, str]]:
        runtime = MCPRuntime.build(paths.home_dir())
        await runtime.connect()
        try:
            registry = ToolRegistry()
            runtime.register_tools(registry)
            rows = []
            for name, item in runtime.manager._tools.items():
                tool = registry.get(name)
                if tool is None:
                    continue
                decision = runtime.policy.classify(str(item["server"]), str(item["remote_name"]))
                rows.append(
                    (name, str(item["server"]), decision.level, "yes" if tool.mutates else "no")
                )
            return rows
        finally:
            await runtime.close()

    try:
        rows = asyncio.run(run())
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not rows:
        click.echo("No MCP tools registered (no servers connected or none configured).")
        return
    table = Table(title="MCP tools")
    for column in ("Exposed tool", "Server", "Risk", "Mutates"):
        table.add_column(column)
    for name, server, level, mutates in rows:
        table.add_row(name, server, level, mutates)
    Console().print(table)


@mcp_group.command("permissions")
@click.option("--read-only-approval", type=click.Choice(["on", "off"]), default=None)
@click.option("--set", "set_rule", nargs=3, type=str, metavar="SERVER_GLOB PATTERN LEVEL")
@click.option("--remove", "remove_glob", nargs=1, default=None)
def mcp_permissions(
    read_only_approval: str | None,
    set_rule: tuple[str, str, str] | None,
    remove_glob: str | None,
) -> None:
    """Show or edit MCP risk policies (security/mcp-policies.json)."""
    path = default_policy_path(paths.home_dir())
    policy = MCPPolicy.load(path)
    changed = False
    if read_only_approval is not None:
        policy.read_only_requires_approval = read_only_approval == "on"
        changed = True
    if set_rule is not None:
        server_glob, pattern, level = set_rule
        try:
            policy.set_server_rule(server_glob, pattern, level)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        changed = True
    if remove_glob is not None:
        if not policy.remove_server(remove_glob):
            raise click.ClickException(f"no rules for server pattern: {remove_glob}")
        changed = True
    if changed:
        policy.save(path)
        click.echo(f"Saved {path}")
        policy = MCPPolicy.load(path)
    approval = "yes" if policy.read_only_requires_approval else "no"
    click.echo(f"read-only tools require approval: {approval}")
    rules = policy.describe()
    if not rules:
        click.echo("No explicit rules; Google tools use built-in risk classification,")
        click.echo("external servers require approval for every tool.")
        return
    table = Table(title="MCP permission rules")
    for column in ("Server", "Tool pattern", "Risk", "Approval"):
        table.add_column(column)
    for row in rules:
        table.add_row(
            str(row["server"]),
            str(row["tool_pattern"]),
            str(row["risk_level"]),
            str(row["approval"]),
        )
    Console().print(table)
