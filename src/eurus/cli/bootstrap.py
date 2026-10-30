from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import click

from eurus.bootstrap.health.checks import run_checks
from eurus.bootstrap.health.report import render_report, report_json, report_text
from eurus.bootstrap.models import HealthReport
from eurus.bootstrap.orchestrator import BootstrapOrchestrator
from eurus.bootstrap.wizard import BootstrapIO
from eurus.cli.base import main
from eurus.config import Config, load_config
from eurus.errors import ConfigError


def _bootstrap_environ() -> dict[str, str]:
    return dict(os.environ)


def _state_overrides() -> tuple[dict[str, Any], Path | None]:
    context = click.get_current_context(silent=True)
    root = context.find_root() if context is not None else None
    state = getattr(root, "obj", None) if root is not None else None
    if state is None or not hasattr(state, "overrides"):
        return {}, None
    return dict(state.overrides), state.config_path


def _load_config_safe() -> Config | None:
    return _load_config_with_error()[0]


def _load_config_with_error() -> tuple[Config | None, str]:
    overrides, config_path = _state_overrides()
    try:
        config = load_config(
            config_path=config_path, overrides=overrides, environ=_bootstrap_environ()
        )
        return config, ""
    except ConfigError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _io(yes: bool = False, non_interactive: bool = False) -> BootstrapIO:
    return BootstrapIO(non_interactive=non_interactive, assume_yes=yes)


def _current_io() -> BootstrapIO:
    context = click.get_current_context(silent=True)
    if context is not None and isinstance(context.obj, dict):
        return _io(context.obj.get("yes", False), context.obj.get("non_interactive", False))
    return _io()


def _orchestrator(io: BootstrapIO) -> BootstrapOrchestrator:
    config, config_error = _load_config_with_error()
    return BootstrapOrchestrator(io, _bootstrap_environ(), config=config, config_error=config_error)


def _apply_report_flags(report: HealthReport, as_json: bool, as_text: bool) -> None:
    if as_json:
        click.echo(report_json(report))
    elif as_text:
        click.echo(report_text(report))


def _repair_exit_code(orchestrator: BootstrapOrchestrator, report: HealthReport) -> int:
    if orchestrator.ctx.repair_cancelled:
        return 0
    if any(not ok for _, ok, _ in orchestrator.ctx.repair_outcomes):
        return 1
    if orchestrator.ctx.repair_outcomes and report.summary == "BLOCKED":
        return 1
    return 0


def _repair_json_payload(orchestrator: BootstrapOrchestrator, report: HealthReport) -> str:
    payload: dict[str, Any] = report.to_dict()
    payload["repair"] = {
        "planned": list(orchestrator.ctx.repair_planned),
        "repaired": [key for key, ok, _ in orchestrator.ctx.repair_outcomes if ok],
        "failed": [key for key, ok, _ in orchestrator.ctx.repair_outcomes if not ok],
        "skipped": orchestrator.ctx.repair_planned if orchestrator.ctx.repair_cancelled else [],
        "cancelled": orchestrator.ctx.repair_cancelled,
    }
    return json.dumps(payload, indent=2)


def _repair_text_payload(orchestrator: BootstrapOrchestrator, report: HealthReport) -> str:
    lines: list[str] = []
    if orchestrator.ctx.repair_cancelled:
        lines.append("repair: cancelled; no changes were made")
    for key, ok, message in orchestrator.ctx.repair_outcomes:
        prefix = "repaired" if ok else "FAILED"
        lines.append(f"repair: {prefix} [{key}] {message}")
    lines.append(report_text(report))
    return "\n".join(lines)


@main.group(
    "bootstrap",
    invoke_without_command=True,
    help="Install, configure, check and repair the EURUS environment.",
)
@click.option("--quick", is_flag=True, help="Run the guided end-to-end setup.")
@click.option("--check", "as_check", is_flag=True, help="Run the read-only environment inspection.")
@click.option(
    "--repair",
    "as_repair",
    is_flag=True,
    help="Show a repair plan, confirm it, then fix the environment.",
)
@click.option(
    "--update", "as_update", is_flag=True, help="Update installed feature packages with pip."
)
@click.option("--yes", is_flag=True, help="Assume yes for confirmation prompts.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Never prompt; use defaults and skip interactive steps.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the report as JSON (with --check/--quick/--repair).",
)
@click.option(
    "--text",
    "as_text",
    is_flag=True,
    help="Print the report as plain text (with --check/--quick/--repair).",
)
@click.pass_context
def bootstrap_group(
    context: click.Context,
    quick: bool,
    as_check: bool,
    as_repair: bool,
    as_update: bool,
    yes: bool,
    non_interactive: bool,
    as_json: bool,
    as_text: bool,
) -> None:
    context.obj = {"yes": yes, "non_interactive": non_interactive}
    if context.invoked_subcommand is not None:
        return
    requested = [flag for flag in (quick, as_check, as_repair, as_update) if flag]
    if not requested:
        click.echo(context.get_help())
        return
    if len(requested) > 1:
        raise click.UsageError("options --quick, --check, --repair and --update are exclusive.")
    if quick:
        report = _orchestrator(_io(yes, non_interactive)).run_quick_setup(
            render=not (as_json or as_text)
        )
        _apply_report_flags(report, as_json, as_text)
    elif as_check:
        report = _orchestrator(_io(yes, non_interactive)).run_check(render=not (as_json or as_text))
        _apply_report_flags(report, as_json, as_text)
        raise SystemExit(1 if report.summary == "BLOCKED" else 0)
    elif as_repair:
        orchestrator = _orchestrator(_io(yes, non_interactive))
        report = orchestrator.run_repair(render=not (as_json or as_text))
        if as_json:
            click.echo(_repair_json_payload(orchestrator, report))
        elif as_text:
            click.echo(_repair_text_payload(orchestrator, report))
        raise SystemExit(_repair_exit_code(orchestrator, report))
    else:
        _orchestrator(_io(yes, non_interactive)).run_update()


CHECK_FLAGS = [
    click.option("--json", "as_json", is_flag=True, help="Print the report as JSON."),
    click.option("--text", "as_text", is_flag=True, help="Print the report as plain text."),
]


@bootstrap_group.command("check", help="Read-only environment inspection.")
@CHECK_FLAGS[0]
@CHECK_FLAGS[1]
def bootstrap_check(as_json: bool, as_text: bool) -> None:
    report = _orchestrator(_current_io()).run_check(render=not (as_json or as_text))
    _apply_report_flags(report, as_json, as_text)
    raise SystemExit(1 if report.summary == "BLOCKED" else 0)


@bootstrap_group.command("quick", help="Guided end-to-end setup.")
@CHECK_FLAGS[0]
@CHECK_FLAGS[1]
def bootstrap_quick(as_json: bool, as_text: bool) -> None:
    report = _orchestrator(_current_io()).run_quick_setup(render=not (as_json or as_text))
    _apply_report_flags(report, as_json, as_text)


@bootstrap_group.command("repair", help="Show a repair plan, confirm it, then fix the environment.")
@CHECK_FLAGS[0]
@CHECK_FLAGS[1]
def bootstrap_repair(as_json: bool, as_text: bool) -> None:
    orchestrator = _orchestrator(_current_io())
    report = orchestrator.run_repair(render=not (as_json or as_text))
    if as_json:
        click.echo(_repair_json_payload(orchestrator, report))
    elif as_text:
        click.echo(_repair_text_payload(orchestrator, report))
    raise SystemExit(_repair_exit_code(orchestrator, report))


@bootstrap_group.command("update", help="Update installed feature packages with pip.")
def bootstrap_update() -> None:
    _orchestrator(_current_io()).run_update()


@bootstrap_group.command(
    "reset", help="Remove bootstrap state files only; never touches keys, sessions or profiles."
)
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def bootstrap_reset(yes: bool) -> None:
    io = _io(yes or _current_io().assume_yes)
    message = (
        "Remove bootstrap state (state.json, platform.json, dependencies.json, setup_history.json)?"
    )
    if not yes and not io.confirm(message, False):
        io.info("reset cancelled")
        return
    removed = io and BootstrapOrchestrator(io).ctx.store.reset()
    for item in removed or []:
        click.echo(f"removed {item}")
    click.echo(
        "bootstrap state cleared; sessions, .env, browser profile and config were not touched"
    )


@main.group("setup", help="Configure one EURUS component (provider, browser, MCP, ...).")
def setup_group() -> None:
    pass


@setup_group.command("provider", help="Configure an AI provider, API key and default model.")
def setup_provider() -> None:
    from eurus.bootstrap.configuration.provider_wizard import run_provider_setup

    io = _io()
    result = run_provider_setup(io, _load_config_safe(), _bootstrap_environ())
    if result.model:
        if result.tested:
            status = "tested ok" if result.test_ok else "test failed"
        else:
            status = "not tested"
        click.echo(f"provider: {result.provider}, model: {result.model}, {status}")
    raise SystemExit(1 if result.tested and not result.test_ok else 0)


@setup_group.command("browser", help="Detect browser runtimes and run a self-test.")
@click.option("--network", is_flag=True, help="Include a navigation test to https://example.com.")
def setup_browser(network: bool) -> None:
    from eurus.bootstrap.integrations.browser import detect_browser, run_self_test

    io = _io()
    environ = _bootstrap_environ()
    config = _load_config_safe()
    report = detect_browser(environ, config=config)
    click.echo(f"playwright package: {report.playwright_package}")
    click.echo(f"websockets package: {report.websockets_package}")
    if report.cdp_endpoint:
        click.echo(f"cdp endpoint: {report.cdp_endpoint} ({report.cdp_reachable})")
    if report.chromium_kind == "missing":
        click.echo("chromium: not found (system paths and the playwright cache were checked)")
    else:
        click.echo(f"chromium: {report.chromium_path} ({report.chromium_kind})")
    click.echo(f"recommended backend: {report.backend_recommendation}")
    if report.chromium_kind == "missing":
        _offer_system_chromium(io, environ)
    if report.cdp_endpoint:
        ok = report.cdp_reachable == "READY"
        raise SystemExit(0 if ok else 1)
    if report.chromium_kind != "missing":
        io.info("running a launch self-test (offline)...")
        ok, rows = run_self_test(network, environ, config=config)
        for name, detail in rows:
            click.echo(f"  {name}: {detail}")
        raise SystemExit(0 if ok else 1)


def _offer_system_chromium(io: BootstrapIO, environ: dict[str, str]) -> None:
    from eurus.bootstrap.dependencies.system_installer import execute_system_install
    from eurus.bootstrap.dependencies.system_resolver import (
        plan_system_install,
        preview_system_plan,
    )

    plan = plan_system_install(("chromium",), environ=environ)
    if plan.empty and not plan.unavailable:
        return
    preview = preview_system_plan(plan)
    for line in preview:
        click.echo(f"  {line}")
    if plan.empty:
        io.info("system package installation is unavailable for this platform; see the note above")
        return
    if not io.confirm("Install the system package listed above?", False):
        io.info("skipped; install chromium manually or configure EURUS_CDP_ENDPOINT")
        return
    for item in execute_system_install(plan, confirm=lambda _preview: True):
        if item.success:
            io.ok(f"installed {item.key} (exit 0)")
        else:
            io.error(f"failed {item.key} (exit {item.returncode}): {item.output_tail}")


@setup_group.command(
    "system-packages", help="Inspect and optionally install optional system packages."
)
@click.option(
    "--install",
    "install_keys",
    multiple=True,
    help="Catalog keys to install (e.g. chromium, git). Preview is shown before install.",
)
@click.option("--yes", is_flag=True, help="Assume yes for the confirmation prompt.")
def setup_system_packages(install_keys: tuple[str, ...], yes: bool) -> None:
    from eurus.bootstrap.dependencies.system_inspector import inspect_system_packages
    from eurus.bootstrap.dependencies.system_installer import execute_system_install
    from eurus.bootstrap.dependencies.system_resolver import (
        plan_system_install,
        preview_system_plan,
    )

    io = _io(yes)
    environ = _bootstrap_environ()
    inspection = inspect_system_packages(environ)
    io.info(
        f"package manager: {inspection.manager.name} ({inspection.manager.path or 'not found'})"
    )
    for item in inspection.entries:
        click.echo(f"  {item.key}: {item.status} ({item.package_name or 'not packaged'})")
        if item.note:
            click.echo(f"    note: {item.note}")
    if not install_keys:
        return
    plan = plan_system_install(tuple(install_keys), environ=environ)
    if plan.empty:
        for line in preview_system_plan(plan):
            click.echo(f"  {line}")
        io.info("nothing to install")
        return
    for line in preview_system_plan(plan):
        click.echo(f"  {line}")
    if not io.confirm("Run the system package commands listed above?", False):
        io.info("cancelled; no system packages were installed")
        return
    failures = 0
    for result in execute_system_install(plan, confirm=lambda _preview: True):
        if result.success:
            io.ok(f"installed {result.key} via: {result.command}")
        else:
            failures += 1
            io.error(f"failed {result.key} (exit {result.returncode}): {result.output_tail}")
    raise SystemExit(1 if failures else 0)


@setup_group.command("mcp", help="Validate mcp.json and test configured servers.")
def setup_mcp() -> None:
    from eurus.bootstrap.integrations.mcp import ensure_mcp_file, inspect_mcp, test_mcp_servers

    io = _io()
    environ = _bootstrap_environ()
    path = ensure_mcp_file(environ)
    report = inspect_mcp(environ)
    if not report.valid:
        io.error(f"{path} is invalid: {report.error}")
        if io.confirm("Back up and reset mcp.json?", True):
            from eurus.bootstrap.health.repair import apply_repair_action
            from eurus.bootstrap.models import RepairAction

            action = RepairAction(key="mcp_reset", title="reset", description="", kind="mcp_reset")
            ok, message = apply_repair_action(action, environ)
            click.echo(message)
            raise SystemExit(0 if ok else 1)
        raise SystemExit(1)
    if not report.servers:
        click.echo(f"{path} has no servers; add one with: eurus mcp add-stdio <name> <command>")
        return
    for item in report.servers:
        click.echo(
            f"{item.name} ({item.transport}): executable={item.executable_found} {item.detail}"
        )
    if io.confirm("Test connections now? (starts each server command)", False):
        failures = 0
        for item in test_mcp_servers(environ):
            click.echo(f"  {item.name}: {item.connection}, {item.tools} tools {item.detail}")
            if item.connection != "READY":
                failures += 1
        raise SystemExit(1 if failures else 0)


@setup_group.command(
    "telegram", help="Store the Telegram bot token and allowlist in the global .env."
)
def setup_telegram() -> None:
    from eurus.bootstrap.configuration.secrets import validate_telegram_token
    from eurus.bootstrap.integrations.telegram import (
        inspect_telegram,
        save_allowlist,
        save_telegram_token,
        test_telegram_token,
    )

    io = _io()
    environ = _bootstrap_environ()
    report = inspect_telegram(environ)
    if report.token_present:
        io.info(f"a token is already configured ({report.token_masked})")
        if not io.confirm("Replace it?", False):
            return
    token = io.hidden("Enter Telegram bot token (hidden)")
    if not token:
        io.info("cancelled; token unchanged")
        return
    problem = validate_telegram_token(token)
    if problem:
        io.error(problem)
        raise SystemExit(1)
    path = save_telegram_token(environ, token)
    io.ok(f"token saved to {path} (0600)")
    if io.confirm("Test the token against api.telegram.org?", True):
        ok, detail = test_telegram_token(token)
        io.console.print(detail)
        if not ok:
            raise SystemExit(1)
    allowlist = io.ask("Allowed Telegram user IDs (comma separated)", report.allowlist or "")
    if allowlist.strip():
        tokens = [item.strip().lstrip("-") for item in allowlist.split(",") if item.strip()]
        if tokens and all(item.isdigit() for item in tokens):
            save_allowlist(environ, allowlist)
            io.ok("allowlist saved")
        else:
            io.error("allowlist must be numeric user IDs; not saved")
            raise SystemExit(1)


@setup_group.command(
    "startup", help="Install or remove autostart services (systemd, Termux:Boot, cron)."
)
def setup_startup() -> None:
    from eurus.bootstrap.startup.manager import (
        SERVICE_NAMES,
        detect_mechanisms,
        install_cron,
        install_launchers,
        install_systemd,
        install_termux_boot,
        recommend_backend,
        status_all,
        uninstall_all,
    )

    io = _io()
    environ = _bootstrap_environ()
    from eurus import paths

    mechanisms = detect_mechanisms(environ)
    termux_prefix = "/data/data/com.termux"
    is_termux = environ.get("TERMUX_VERSION") or environ.get("PREFIX", "").startswith(termux_prefix)
    variant = "termux" if is_termux else "linux"
    backend = recommend_backend(variant, mechanisms)
    io.info(
        f"mechanisms: systemd={mechanisms.systemd}, "
        f"termux-boot={mechanisms.termux_boot}, cron={mechanisms.cron}"
    )
    io.info(f"recommended backend: {backend}")
    action = io.choose("Action", ["Install services", "Remove all services", "Show status"], 2)
    if action == 2:
        for item in status_all(environ):
            if item.installed:
                state = f"{item.state} via {item.mechanism} ({item.path})"
            else:
                state = "not installed"
            click.echo(f"{item.service}: {state}")
        return
    if action == 1:
        if not io.confirm("Remove every eurus service, boot script and cron entry?", False):
            return
        removed = uninstall_all(environ)
        for entry in removed:
            io.ok(f"removed: {entry}")
        if not removed:
            io.info("nothing to remove")
        return
    labels = list(SERVICE_NAMES)
    picked = io.ask("Services to enable (comma separated numbers, empty to skip)", "")
    services = []
    for token in picked.replace(" ", ",").split(","):
        if token.isdigit() and 1 <= int(token) <= len(labels):
            services.append(labels[int(token) - 1])
    if not services:
        io.info("no services selected")
        return
    if backend == "systemd":
        for service in services:
            workdir = str(paths.home_dir(environ))
            env_lines = [f"EURUS_HOME={workdir}"]
            path = install_systemd(
                service, workdir=workdir, enable=True, start=False, env_lines=env_lines
            )
            io.ok(f"installed {path}; enable with: systemctl --user enable --now eurus-{service}")
    elif backend == "termux-boot":
        path = install_termux_boot(environ, services)
        io.ok(f"installed termux boot script: {path}")
    elif backend == "cron":
        install_launchers(environ, services)
        ok, message = install_cron(services, environ=environ)
        (io.ok if ok else io.warn)(message)
    else:
        created = install_launchers(environ, services)
        for launcher in created:
            io.ok(f"created launcher: {launcher}")
        io.info("no autostart mechanism detected; add the launcher to your shell profile")


@setup_group.command("config", help="Open the interactive configuration manager for config.toml.")
def setup_config() -> None:
    from eurus.bootstrap.dashboard import open_configuration_manager

    open_configuration_manager(_io(), _bootstrap_environ())


@main.command(help="Check the EURUS environment and report its health.")
@click.option(
    "--full",
    is_flag=True,
    help="Include network probes: model API, browser launch and MCP connections.",
)
@click.option(
    "--repair", "do_repair", is_flag=True, help="Offer to repair the problems that were found."
)
@click.option("--yes", is_flag=True, help="Assume yes for the repair confirmation.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option("--text", "as_text", is_flag=True, help="Print the report as plain text.")
def doctor(full: bool, do_repair: bool, yes: bool, as_json: bool, as_text: bool) -> None:
    io = _io(yes)
    environ = _bootstrap_environ()
    if do_repair:
        orchestrator = _orchestrator(io)
        report = orchestrator.run_repair(render=not (as_json or as_text))
        if as_json:
            click.echo(_repair_json_payload(orchestrator, report))
        elif as_text:
            click.echo(_repair_text_payload(orchestrator, report))
        raise SystemExit(_repair_exit_code(orchestrator, report))
    config, config_error = _load_config_with_error()
    report = run_checks(environ, full=full, config=config, config_error=config_error)
    from eurus import __version__, paths
    from eurus.bootstrap.state.store import BootstrapStore

    store = BootstrapStore(paths.home_dir(environ))
    state = store.load_state() or {"schema_version": 1}
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["doctor"] = {"summary": report.summary, "checked_at": checked_at}
    state["last_checked"] = checked_at
    state["eurus_version"] = __version__
    store.save_state(state)
    if as_json:
        click.echo(report_json(report))
    elif as_text:
        click.echo(report_text(report))
    else:
        render_report(report, io.console)
    raise SystemExit(1 if report.summary == "BLOCKED" else 0)
