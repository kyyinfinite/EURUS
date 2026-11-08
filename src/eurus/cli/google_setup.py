from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.mcp.auth.credentials import client_file_path
from eurus.mcp.policy import MCPPolicy, default_policy_path
from eurus.mcp.providers.google_workspace import build_provider
from eurus.mcp.providers.google_workspace.services import (
    PRESETS,
    PUBLISH_STATUSES,
    GoogleWorkspaceSettings,
)
from eurus.mcp.runtime import MCPRuntime
from eurus.tools.base import ToolRegistry

AGENTIC_DEADLINE = "11 Nov 2026"


@main.group("google", help="Set up and diagnose the Google Workspace MCP integration.")
def google_group() -> None:
    pass


@google_group.command("setup")
@click.option(
    "--client-file",
    "client_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Google Cloud console OAuth client JSON to import (installed/web).",
)
@click.option(
    "--services",
    "services",
    default="",
    help="Comma-separated services to enable (e.g. gmail,drive,calendar).",
)
@click.option(
    "--publish-status",
    "publish_status",
    type=click.Choice(PUBLISH_STATUSES),
    default=None,
    help="OAuth consent screen publish status.",
)
@click.option(
    "--preset",
    "preset",
    type=click.Choice(PRESETS),
    default=None,
    help="Enable a service bundle: personal, student, workspace or custom.",
)
@click.option("--login", "do_login", is_flag=True, help="Run the OAuth login at the end.")
@click.option("--paste", is_flag=True, help="Enable the manual paste fallback during login.")
@click.option("--test/--no-test", "run_test", default=True, help="Live-test each service.")
def google_setup(
    client_file: Path | None,
    services: str,
    preset: str | None,
    publish_status: str | None,
    do_login: bool,
    paste: bool,
    run_test: bool,
) -> None:
    _state(click.get_current_context())
    home = paths.home_dir()

    if client_file is not None:
        _import_client_file(client_file, home)
    else:
        existing = client_file_path(home)
        click.echo(
            f"OAuth client file: {existing} ({'present' if existing.exists() else 'absent'})"
        )
        if not existing.exists():
            click.echo(
                "No client.json yet. Create an OAuth client in Google Cloud Console "
                "(see docs/GOOGLE_OAUTH.md), download its JSON and re-run with "
                "--client-file <path>. The client MUST have 'Use this client for an "
                f"AI-powered agent' enabled before {AGENTIC_DEADLINE}; after 12 Nov 2026 "
                "existing clients cannot be changed - create a NEW one."
            )

    settings = GoogleWorkspaceSettings.load(home / "integrations" / "google-workspace.json")
    if publish_status is not None:
        settings.oauth_publish_status = publish_status
    click.echo(
        f"Consent screen publish status: {settings.oauth_publish_status}"
        + (
            "  (Testing mode: refresh tokens expire after 7 days!)"
            if settings.oauth_publish_status == "testing"
            else ""
        )
    )

    if preset is not None:
        enabled = settings.apply_preset(preset)
        click.echo(f"Preset '{preset}' enabled: {', '.join(enabled) or 'none'}")
    requested = [item.strip().lower() for item in services.split(",") if item.strip()]
    if requested:
        for service_id in requested:
            try:
                settings.set_enabled(service_id, True)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(f"Enabled {service_id} (google-{service_id}).")
    else:
        click.echo(
            "Enabled services: "
            + (", ".join(settings.enabled_ids()) or "none")
            + "  (enable more with: eurus mcp google enable <service>)"
        )

    from eurus.mcp.providers.google_workspace.catalog import service_by_id

    enabled_services = [service_by_id(item) for item in settings.enabled_ids()]
    if enabled_services:
        commands: list[str] = []
        for service in enabled_services:
            commands.extend(service.gcloud_commands)
        unique = list(dict.fromkeys(commands))
        click.echo("\nEnable these APIs/MCP services in your Google Cloud project:")
        for command in unique:
            click.echo(f"  {command}")
        click.echo("Console: https://console.cloud.google.com/apis/library")
        for service in enabled_services:
            click.echo(f"  {service.label}: {service.console_url}")
        click.echo(
            f"\nReminder: the OAuth client must be marked 'Use this client for an "
            f"AI-powered agent' (agentic). Deadline: {AGENTIC_DEADLINE}."
        )

    settings.save(home / "integrations" / "google-workspace.json")
    click.echo("Saved integrations/google-workspace.json (config v2).")

    if do_login:
        from eurus.cli.auth import auth_google_login

        click.echo("\nStarting the OAuth login...")
        ctx = click.get_current_context()
        try:
            ctx.invoke(auth_google_login, no_browser=False, paste_fallback=paste)
        except click.ClickException:
            raise
        except Exception as exc:
            click.echo(f"Login failed: {exc}")

    if run_test:
        click.echo("")
        _live_test(home)


@google_group.command("doctor")
def google_doctor() -> None:
    _state(click.get_current_context())
    _live_test(paths.home_dir(), verbose=True)


def _import_client_file(client_file: Path, home: Path) -> None:
    try:
        raw = json.loads(client_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"cannot read client file: {exc}") from exc
    if not isinstance(raw, dict):
        raise click.ClickException("client file must contain a JSON object")
    section = None
    for candidate in ("installed", "web"):
        if isinstance(raw.get(candidate), dict):
            section = candidate
            break
    if section is None and {"client_id", "client_secret"} & set(raw):
        section = "flat"
    if section is None:
        raise click.ClickException(
            "unrecognized client file: expected a Google console export "
            '({"installed": {...}} or {"web": {...}}) or a flat client_id/client_secret JSON'
        )
    data = raw if section == "flat" else raw[section]
    client_id = str(data.get("client_id", ""))
    if not client_id:
        raise click.ClickException("client file has no client_id")
    uris = data.get("redirect_uris") if isinstance(data.get("redirect_uris"), list) else []
    if section == "web":
        click.echo(
            "Web-application client detected. Make sure one of its redirect URIs is the "
            "loopback URL Eurus prints during login (or use a Desktop-app client, which "
            "needs no fixed redirect URI)."
        )
        if uris:
            click.echo(f"Declared redirect URIs: {', '.join(str(uri) for uri in uris[:5])}")
    target = client_file_path(home)
    if target.exists():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            current_id = str(
                (current.get("installed") or current.get("web") or current).get("client_id", "")
            )
        except (OSError, json.JSONDecodeError):
            current_id = ""
        if current_id == client_id:
            click.echo(f"Client file already imported ({client_id[:24]}...); keeping it.")
            return
        click.echo(f"Replacing existing client.json (was {current_id[:24] or 'unreadable'}).")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        target.chmod(0o600)
    click.echo(f"Imported OAuth client into {target}")
    if shutil.which("gcloud"):
        click.echo("(gcloud detected - remember: client creation, not just configuration.)")


def _live_test(home: Path, *, verbose: bool = False) -> None:
    provider = build_provider(home)
    if not provider.settings.enabled_ids():
        click.echo("No Google services enabled; nothing to test. Try: eurus google setup")
        return

    async def run() -> list[dict[str, object]]:
        runtime = MCPRuntime.build(home)
        await runtime.ensure_connected(max_wait=25.0)
        runtime.register_tools(ToolRegistry())
        try:
            return list(runtime.manager.status())
        finally:
            await runtime.close()

    try:
        rows = asyncio.run(run())
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(f"connection test failed: {exc}") from exc

    google_rows = {row["name"]: row for row in rows if str(row["name"]).startswith("google-")}
    policy = MCPPolicy.load(default_policy_path(home))
    table = Table(title="Google Workspace live test")
    for column in ("Service", "State", "Tools", "Code", "Fix / error"):
        table.add_column(column)
    failures = 0
    for service in provider.enabled_services():
        row = google_rows.get(service.server_name, {})
        state = str(row.get("state", "not configured"))
        error = str(row.get("error", ""))
        code = str(row.get("error_code", ""))
        if state == "connected":
            fix = "-"
        elif state == "reauth_required":
            fix = "run: eurus auth google login"
            failures += 1
        else:
            fix = error[:150] or "see docs/GOOGLE_OAUTH.md troubleshooting"
            failures += 1
        table.add_row(service.service_id, state, str(row.get("tools", 0)), code, fix)
    Console().print(table)
    if failures == 0:
        click.echo("All enabled Google services connected.")
    else:
        click.echo(
            f"{failures} service(s) did not connect; run 'eurus google doctor' for "
            "details or see docs/GOOGLE_OAUTH.md."
        )
    if verbose:
        click.echo(f"Policy: {policy.path} access floor: {policy.google_access}")


__all__ = ["google_group"]
