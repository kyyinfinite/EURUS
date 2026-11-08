from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.mcp.auth import GoogleOAuthManager
from eurus.mcp.providers.google_workspace import build_auth, build_provider


def _manager() -> GoogleOAuthManager:
    return build_auth(paths.home_dir())


@main.group("auth")
def auth_group() -> None:
    pass


@auth_group.group("google")
def auth_google() -> None:
    pass


@auth_google.command("login")
@click.option(
    "--no-browser",
    is_flag=True,
    help="Print the authorization URL instead of opening a browser.",
)
@click.option(
    "--paste",
    "paste_fallback",
    is_flag=True,
    help="Offer a manual paste of the redirect URL when the callback fails "
    "(headless/SSH/Termux without a browser).",
)
def auth_google_login(no_browser: bool, paste_fallback: bool) -> None:
    _state(click.get_current_context())
    provider = build_provider(paths.home_dir())
    manager = _manager()
    if not manager.client_configured:
        raise click.ClickException(
            "Google OAuth client is not configured.\n"
            "Set EURUS_GOOGLE_CLIENT_ID and EURUS_GOOGLE_CLIENT_SECRET (or create\n"
            "~/.eurus/auth/google/client.json). See docs/GOOGLE_OAUTH.md for the\n"
            "Google Cloud console steps, including the 'AI-powered agent' client option."
        )
    scopes = provider.login_scopes()
    if not provider.settings.enabled_ids():
        scopes = []
        click.echo("No Google Workspace services enabled yet; requesting base identity scopes.")
        click.echo("Enable services with: eurus mcp google enable <service>")
    try:
        account = asyncio.run(
            manager.login(
                scopes,
                open_browser=not no_browser,
                paste_input=input if paste_fallback else None,
            )
        )
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    click.echo(f"Logged in as {account.email} (account id: {account.account_id}).")
    enabled_ids = provider.settings.enabled_ids()
    if enabled_ids:
        click.echo(f"Services enabled for this account: {', '.join(enabled_ids)}")
    drift = provider.scope_drift()
    if drift:
        click.echo(
            f"Warning: {len(drift)} scope(s) missing from the stored token; "
            "re-run the login to grant them."
        )


@auth_google.command("status")
def auth_google_status() -> None:
    _state(click.get_current_context())
    provider = build_provider(paths.home_dir())
    manager = _manager()
    manager.publish_status = provider.settings.oauth_publish_status
    rows = manager.status()
    table = Table(title="Google accounts")
    for column in ("Account", "Email", "Active", "Credentials", "Token valid", "Age (h)", "Status"):
        table.add_column(column)
    if not rows:
        click.echo("No Google accounts configured. Run: eurus auth google login")
        return
    for row in rows:
        table.add_row(
            str(row["account_id"]),
            str(row["email"]),
            "yes" if row["active"] else "no",
            "yes" if row["has_credentials"] else "no",
            "yes" if row["token_valid"] else "no",
            str(row["token_age_hours"]) if row["token_age_hours"] is not None else "-",
            str(row["status"]),
        )
    Console().print(table)
    for row in rows:
        warning = str(row.get("refresh_warning") or "")
        if warning:
            click.echo(f"warning: {warning}")
    click.echo(f"Client configured: {'yes' if manager.client_configured else 'no'}")
    click.echo(f"Token storage: {manager.store.backend_name}")
    click.echo(f"Consent screen publish status: {provider.settings.oauth_publish_status}")
    drift = provider.scope_drift()
    if drift:
        click.echo(f"Scope drift: {len(drift)} scope(s) missing; re-run 'eurus auth google login'")
    for warning in provider.settings.load_warnings + manager.accounts.load_warnings:
        click.echo(f"warning: {warning}")


@auth_google.command("accounts")
def auth_google_accounts() -> None:
    _state(click.get_current_context())
    manager = _manager()
    for account in manager.accounts.list():
        marker = "*" if account.account_id == manager.accounts.active_id else " "
        click.echo(f"{marker} {account.account_id}  {account.email}")


@auth_google.command("use")
@click.argument("account")
def auth_google_use(account: str) -> None:
    _state(click.get_current_context())
    manager = _manager()
    resolved = manager.accounts.get(account)
    if resolved is None:
        resolved = manager.accounts.by_email(account)
    if resolved is None:
        known = ", ".join(item.account_id for item in manager.accounts.list()) or "none"
        raise click.ClickException(f"Unknown Google account: {account}. Known: {known}")
    manager.accounts.set_active(resolved.account_id)
    click.echo(f"Active Google account is now {resolved.email} ({resolved.account_id}).")


@auth_google.command("logout")
@click.argument("account", required=False)
@click.option("--all", "logout_all", is_flag=True, help="Log out every account.")
def auth_google_logout(account: str | None, logout_all: bool) -> None:
    _state(click.get_current_context())
    manager = _manager()
    if logout_all:
        targets = [item.account_id for item in manager.accounts.list()]
    else:
        target = account
        if target is None:
            active = manager.accounts.active
            if active is None:
                raise click.ClickException("No active Google account to log out.")
            target = active.account_id
        resolved = manager.accounts.get(target)
        if resolved is None:
            resolved = manager.accounts.by_email(target)
        if resolved is None:
            raise click.ClickException(f"Unknown Google account: {target}")
        targets = [resolved.account_id]
    if not targets:
        click.echo("No Google accounts to log out.")
        return
    removed = asyncio.run(manager.logout() if logout_all else manager.logout(targets[0]))
    for item in removed:
        click.echo(f"Logged out {item.email} (tokens revoked and removed).")
