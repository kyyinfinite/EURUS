"""Browser automation management commands."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.config import BrowserConfig
from eurus.errors import ToolError

if TYPE_CHECKING:
    from eurus.browser.manager import BrowserSettings


def _resolved_backend_name(browser_config: BrowserConfig) -> str:
    from eurus.browser.backend import resolve_backend
    from eurus.termux import is_termux

    requested = browser_config.backend.lower()
    if requested != "auto":
        return requested
    try:
        return str(resolve_backend(_settings_of(browser_config), termux=is_termux()))
    except Exception:
        return "auto"


def _settings_of(browser_config: BrowserConfig) -> BrowserSettings:
    from eurus.browser.manager import BrowserSettings

    return BrowserSettings.from_config(browser_config)


def _chromium_path(browser_config: BrowserConfig) -> str:
    from eurus.browser.backend import find_chromium

    try:
        return find_chromium(browser_config.chromium_executable)
    except Exception:
        found = shutil.which("chromium") or shutil.which("chromium-browser")
        return found or "not found"


@main.group("browser")
def browser_group() -> None:
    """Manage the browser automation backend."""


@browser_group.command("status")
@click.pass_context
def browser_status(ctx: click.Context) -> None:
    config = _state(ctx).config
    browser = config.browser
    manager = None
    table = Table(title="browser")
    for column in ("Setting", "Value"):
        table.add_column(column)
    table.add_row("enabled", str(browser.enabled).lower())
    table.add_row("backend", _resolved_backend_name(browser))
    table.add_row("headless", str(browser.headless).lower())
    table.add_row("chromium", _chromium_path(browser))
    table.add_row(
        "cdp endpoint",
        browser.cdp_endpoint or ("(launched locally)"),
    )
    table.add_row("profile", browser.profile_dir or str(paths.home_dir() / "browser" / "profile"))
    table.add_row(
        "navigation timeout",
        f"{browser.navigation_timeout_seconds:g}s "
        f"(interaction {browser.interaction_timeout_seconds:g}s)",
    )
    table.add_row("snapshot budget", f"{browser.max_snapshot_tokens} tokens")
    table.add_row(
        "domains",
        f"allow: {', '.join(browser.allowed_domains) or 'all public'}; "
        f"block: {', '.join(browser.blocked_domains) or 'none'}",
    )
    table.add_row("downloads", browser.download_dir or "(workspace)/downloads")
    table.add_row("max download", f"{browser.max_download_mb} MiB")
    if manager is not None:
        status = manager.status()
        table.add_row("connection", "connected" if status["connected"] else "stopped")
        table.add_row("process", "owned" if status["owns_process"] else "external")
    Console().print(table)
    if not browser.enabled:
        Console().print("browser tools are disabled; set [browser] enabled = true to turn them on")


@browser_group.command("start")
@click.pass_context
def browser_start(ctx: click.Context) -> None:
    config = _state(ctx).config
    if not config.browser.enabled:
        raise click.ClickException("browser is disabled; set [browser] enabled = true first")

    async def _start() -> dict[str, object]:
        from eurus.browser.manager import BrowserManager

        manager = BrowserManager(_settings_of(config.browser), workspace=paths.home_dir())
        try:
            await manager.backend.start()
        except ToolError as exc:
            raise click.ClickException(str(exc)) from exc
        status = manager.status()
        await manager.close_all()
        return status

    try:
        status = asyncio.run(_start())
    except click.ClickException:
        raise
    Console().print(
        f"browser backend '{status.get('backend')}' started and stopped cleanly "
        "(the agent starts its own instance when needed)"
    )


@browser_group.command("stop")
@click.pass_context
def browser_stop(ctx: click.Context) -> None:
    Console().print(
        "the agent-owned browser is stopped when the agent run ends; "
        "use `eurus browser login` sessions by closing their window, or kill the "
        "Chromium process shown in `eurus browser status`"
    )


@browser_group.command("login")
@click.argument("url", default="https://accounts.google.com")
@click.option("--timeout", default=300.0, help="Seconds to keep the login browser open.")
@click.pass_context
def browser_login(ctx: click.Context, url: str, timeout: float) -> None:
    """Open the Eurus browser profile for a manual login handoff."""
    config = _state(ctx).config
    settings = _settings_of(config.browser)
    profile = (
        Path(settings.profile_dir)
        if settings.profile_dir
        else (paths.home_dir() / "browser" / "profile")
    )
    profile.mkdir(parents=True, exist_ok=True)

    async def _login() -> None:
        from eurus.browser.cdp_backend import CdpBackend

        backend = CdpBackend(settings)
        try:
            await backend.start()
            page = await backend.open_page()
            await page.goto(url, settings.navigation_timeout)
        except ToolError as exc:
            raise click.ClickException(str(exc)) from exc
        Console().print(
            f"Login window open for {timeout:g}s. Log in manually; cookies are stored in "
            f"{profile}. Press Ctrl+C to finish early."
        )
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            pass
        finally:
            await backend.stop()

    try:
        asyncio.run(_login())
    except KeyboardInterrupt:
        Console().print("login session closed; the profile keeps the authenticated cookies")


@browser_group.command("config")
@click.pass_context
def browser_config(ctx: click.Context) -> None:
    config = _state(ctx).config
    data = config.browser.__dict__ if hasattr(config.browser, "__dict__") else {}
    if not data:
        from dataclasses import asdict

        data = asdict(config.browser)
    for key in sorted(data):
        value = data[key]
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value) or "(empty)"
        Console().print(f"{key} = {value}")


@browser_group.command("test")
@click.option("--network", is_flag=True, help="Also load https://example.com (requires internet).")
@click.pass_context
def browser_test(ctx: click.Context, network: bool) -> None:
    config = _state(ctx).config
    settings = _settings_of(config.browser)

    async def _test() -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        from eurus.browser.manager import BrowserManager

        manager = BrowserManager(settings, workspace=paths.home_dir())
        try:
            try:
                await manager.backend.start()
                results.append(("backend start", "passed"))
            except Exception as exc:
                results.append(("backend start", f"failed: {exc}"))
                return results
            results.append(
                ("process ownership", "owned" if manager.backend.owns_process() else "external")
            )
            if network:
                try:
                    page = await manager.backend.open_page()
                    outcome = await page.goto("https://example.com", settings.navigation_timeout)
                    title = await page.title()
                    results.append(
                        (
                            "navigation",
                            f"passed: HTTP {outcome.status}, title '{title[:40]}'",
                        )
                    )
                    await page.close()
                except Exception as exc:
                    results.append(("navigation", f"failed: {exc}"))
            try:
                await manager.close_all()
                results.append(("cleanup", "passed"))
            except Exception as exc:
                results.append(("cleanup", f"failed: {exc}"))
        finally:
            await manager.close_all()
        return results

    rows = asyncio.run(_test())
    table = Table(title="browser test")
    for column in ("Check", "Result"):
        table.add_column(column)
    for name, outcome in rows:
        table.add_row(name, outcome)
    Console().print(table)
