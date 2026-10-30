from __future__ import annotations

from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eurus.bootstrap.configuration.provider_wizard import run_provider_setup
from eurus.bootstrap.dependencies.catalog import FEATURES, feature_status
from eurus.bootstrap.dependencies.inspector import inspect_all
from eurus.bootstrap.dependencies.installer import install_targets
from eurus.bootstrap.dependencies.resolver import plan_for_features
from eurus.bootstrap.dependencies.verifier import verify_features
from eurus.bootstrap.detection.capabilities import inspect_environment
from eurus.bootstrap.health.checks import load_config_safe, run_checks
from eurus.bootstrap.health.report import STATUS_STYLES, render_report
from eurus.bootstrap.integrations.browser import detect_browser, run_self_test
from eurus.bootstrap.integrations.mcp import ensure_mcp_file, inspect_mcp, test_mcp_servers
from eurus.bootstrap.integrations.telegram import (
    inspect_telegram,
    save_allowlist,
    save_telegram_token,
    test_telegram_token,
)
from eurus.bootstrap.models import READY
from eurus.bootstrap.orchestrator import BootstrapOrchestrator
from eurus.bootstrap.startup.manager import detect_mechanisms, status_all, uninstall_all
from eurus.bootstrap.wizard import BootstrapIO

MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("quick", "Quick Setup"),
    ("system", "System Dependencies"),
    ("packages", "Python Packages"),
    ("provider", "AI Providers & API Keys"),
    ("browser", "Browser Setup"),
    ("mcp", "MCP & Integrations"),
    ("telegram", "Telegram Setup"),
    ("config", "Configuration Manager"),
    ("startup", "Startup & Services"),
    ("diagnostics", "Diagnostics"),
    ("repair", "Repair / Update"),
    ("exit", "Exit"),
)

EXIT_KEYS = {"exit", "q", "quit", "12"}


class BootstrapDashboard:
    def __init__(self, io: BootstrapIO, environ: dict[str, str] | None = None) -> None:
        self.io = io
        self.environ = dict(environ) if environ is not None else None
        self.orchestrator = BootstrapOrchestrator(io, self.environ)

    def run(self) -> int:
        while True:
            self.render()
            raw = self._read_choice()
            if raw is None or raw in EXIT_KEYS:
                self.io.info("bye")
                return 0
            key = self._key_for(raw)
            if key is None:
                self.io.warn(f"unknown choice: {raw}")
                continue
            self.run_module(key)

    def _read_choice(self) -> str | None:
        io = self.io
        if not io.interactive():
            io.warn(
                "terminal is not interactive; use direct commands"
                " (eurus bootstrap --quick, --check, --repair)"
            )
            return None
        try:
            return io.ask("Select module (number, or q to exit)", "12")
        except (EOFError, KeyboardInterrupt):
            return None

    def _key_for(self, raw: str) -> str | None:
        text = raw.strip().lower()
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(MENU_ITEMS):
                return MENU_ITEMS[index - 1][0]
            return None
        for key, label in MENU_ITEMS:
            if text == key or text == label.lower():
                return key
        return None

    def render(self) -> None:
        io = self.io
        environment = inspect_environment(self.environ_effective())
        platform = environment.platform
        python = environment.python
        header = Table(box=None, pad_edge=False, show_header=False)
        header.add_column(width=26)
        header.add_column()
        header.add_row("Platform", platform.label)
        header.add_row("Architecture", environment.architecture.normalized)
        header.add_row("Python", f"{python.version}{' (venv)' if python.is_venv else ''}")
        report = run_checks(self.environ)
        health = Table(box=None, pad_edge=False, show_header=False)
        health.add_column(width=9)
        health.add_column()
        for result in report.results[:8]:
            style = STATUS_STYLES.get(result.status, "white")
            health.add_row(
                f"[{style}][{'OK' if result.status == READY else result.status}][/{style}]",
                result.name,
            )
        panel = Panel(
            Text.assemble(
                ("EURUS\n", "bold"),
                ("BOOTSTRAP & ENVIRONMENT MANAGER\n\n", "bright_black"),
            ),
            expand=False,
        )
        io.console.print(panel)
        io.console.print(header)
        io.echo()
        io.console.print("[bold]SYSTEM HEALTH[/bold]")
        io.console.print(health)
        io.echo()
        io.console.print("[bold]SETUP MODULES[/bold]")
        for index, (_key, label) in enumerate(MENU_ITEMS, start=1):
            io.echo(f"  {index:>2}. {label}")
        io.echo()

    def run_module(self, key: str) -> None:
        handlers = {
            "quick": self.module_quick_setup,
            "system": self.module_system_dependencies,
            "packages": self.module_python_packages,
            "provider": self.module_providers,
            "browser": self.module_browser,
            "mcp": self.module_mcp,
            "telegram": self.module_telegram,
            "config": self.module_configuration,
            "startup": self.module_startup,
            "diagnostics": self.module_diagnostics,
            "repair": self.module_repair,
        }
        handler = handlers.get(key)
        if handler is None:
            return
        try:
            handler()
        except KeyboardInterrupt:
            self.io.warn("module interrupted")
        except Exception as exc:
            self.io.error(f"module failed: {type(exc).__name__}: {exc}")
        self.io.echo()

    def module_quick_setup(self) -> None:
        self.orchestrator.run_quick_setup()

    def module_system_dependencies(self) -> None:
        io = self.io
        environment = inspect_environment(self.environ)
        manager = environment.package_manager
        root = "yes" if manager.is_root else "no"
        io.info(f"package manager: {manager.name or 'none found'} (root: {root})")
        self._system_package_report(io)
        report = detect_browser(self.environ)
        if report.chromium_kind == "missing":
            from eurus.bootstrap.detection.package_manager import chromium_install_hint

            io.warn("chromium is not installed")
            io.info(f"suggested command: {chromium_install_hint(manager)}")
            self._offer_system_package(io, ("chromium",))
        else:
            io.ok(f"chromium ({report.chromium_kind}): {report.chromium_path}")
        io.info("this module never upgrades the whole system; it only reports suggested commands")

    def _system_package_report(self, io: BootstrapIO) -> None:
        from eurus.bootstrap.dependencies.system_inspector import inspect_system_packages

        inspection = inspect_system_packages(self.environ)
        for item in inspection.entries:
            io.status_line(
                item.label, "OK" if item.status == READY else item.status, item.description
            )
            if item.note:
                io.info(f"  {item.key}: {item.note}")

    def _offer_system_package(self, io: BootstrapIO, keys: tuple[str, ...]) -> None:
        from eurus.bootstrap.dependencies.system_installer import execute_system_install
        from eurus.bootstrap.dependencies.system_resolver import (
            plan_system_install,
            preview_system_plan,
        )

        plan = plan_system_install(keys, environ=self.environ)
        if plan.empty and not plan.unavailable:
            return
        for line in preview_system_plan(plan):
            io.echo(f"  {line}")
        if plan.empty:
            io.info("automatic installation is unavailable for this platform")
            return
        if not io.confirm("Install the system package(s) listed above?", False):
            io.info("skipped system package installation")
            return
        for item in execute_system_install(plan, confirm=lambda _preview: True):
            if item.success:
                io.ok(f"installed {item.key} (exit 0)")
            else:
                io.error(f"failed {item.key} (exit {item.returncode}): {item.output_tail}")

    def module_python_packages(self) -> None:
        io = self.io
        packages = inspect_all()
        statuses = [feature_status(spec, packages) for spec in FEATURES]
        table = Table(box=None, pad_edge=False)
        table.add_column("#", justify="right", width=3)
        table.add_column("Feature", style="bold")
        table.add_column("Status", width=9)
        table.add_column("Missing")
        for index, item in enumerate(statuses, start=1):
            style = "green" if item.status == READY else "yellow"
            table.add_row(
                str(index),
                item.label,
                f"[{style}]{item.status}[/{style}]",
                ", ".join(item.missing) or "-",
            )
        io.console.print(table)
        broken = [item.name for item in packages.values() if item.status == "BROKEN"]
        if broken:
            io.warn(f"broken packages detected: {', '.join(broken)}")
        io.echo()
        candidates = [item.feature for item in statuses if item.status != READY]
        if not candidates:
            io.ok("all supported features are installed")
            return
        if not io.confirm("Install missing packages now?", False):
            return
        io.info("features: " + ", ".join(candidates))
        plan = plan_for_features(tuple(candidates), packages)
        io.info("pip targets: " + ", ".join(plan.targets))
        if not io.confirm("Proceed with pip install?", True):
            return
        with io.spinner("running pip"):
            outcome = install_targets(list(plan.targets), environ=self.environ)
        if outcome.success:
            io.ok("install finished; verifying")
            for item in verify_features():
                io.status_line(
                    item.label,
                    item.status if item.status != "OPTIONAL" else "SKIP",
                    item.description,
                )
        else:
            io.error(f"pip failed (exit {outcome.returncode}):")
            io.echo(outcome.output_tail)

    def module_providers(self) -> None:
        config = load_config_safe(self.environ)
        run_provider_setup(self.io, config, self.environ_effective())

    def module_browser(self) -> None:
        io = self.io
        environ = self.environ_effective()
        config = load_config_safe(environ)
        report = detect_browser(environ, config=config)
        io.info(f"playwright package: {report.playwright_package}")
        io.info(f"websockets package: {report.websockets_package}")
        if report.cdp_endpoint:
            io.info(f"cdp endpoint: {report.cdp_endpoint} ({report.cdp_reachable})")
        if report.chromium_kind == "missing":
            io.warn("chromium: not found (checked PATH and the Playwright cache)")
        else:
            io.ok(f"chromium ({report.chromium_kind}): {report.chromium_path}")
        for candidate in report.chromium_candidates:
            io.echo(f"  candidate: {candidate['path']} (exists: {candidate['exists']})")
        io.info(f"recommended backend: {report.backend_recommendation}")
        io.info(f"profile directory: {report.profile_dir}")
        if report.cdp_endpoint:
            if report.cdp_reachable == READY:
                io.ok("remote CDP endpoint is reachable; no local launch test is needed")
            else:
                io.error("remote CDP endpoint is not reachable; check the browser and endpoint")
            return
        if report.chromium_kind == "missing":
            return
        if not io.confirm("Run a browser launch self-test?", False):
            return
        network = False
        if io.confirm("Include a network navigation test to https://example.com?", False):
            network = True
        io.info("launching a temporary headless browser...")
        ok, rows = run_self_test(network, environ, config=config)
        for name, detail in rows:
            (io.ok if "passed" in detail or name == "backend name" else io.warn)(
                f"{name}: {detail}"
            )
        (io.ok if ok else io.error)("self-test finished")

    def module_mcp(self) -> None:
        io = self.io
        path = ensure_mcp_file(self.environ_effective())
        report = inspect_mcp(self.environ_effective())
        if not report.valid:
            io.error(f"mcp.json is invalid: {report.error}")
            if io.confirm("Back up and reset mcp.json?", True):
                from eurus.bootstrap.health.repair import apply_repair_action
                from eurus.bootstrap.models import RepairAction

                action = RepairAction(
                    key="mcp_reset", title="reset mcp.json", description="", kind="mcp_reset"
                )
                ok, message = apply_repair_action(action, self.environ_effective())
                (io.ok if ok else io.error)(message)
            return
        io.info(f"configuration: {path}")
        if not report.servers:
            io.info("no MCP servers configured; add one with: eurus mcp add-stdio <name> <command>")
            return
        for item in report.servers:
            style = "green" if item.executable_found == READY else "yellow"
            io.console.print(
                f"  [{style}]{item.name}[/{style}] ({item.transport}) {item.target}:"
                f" {item.executable_found} {item.detail}"
            )
        if io.confirm("Test server connections now? (starts each server command)", False):
            for item in test_mcp_servers(self.environ_effective()):
                style = "green" if item.connection == READY else "red"
                io.console.print(
                    f"  [{style}]{item.name}[/{style}] {item.connection}, "
                    f"{item.tools} tools {item.detail}"
                )

    def module_telegram(self) -> None:

        io = self.io
        report = inspect_telegram(self.environ_effective())
        io.info(f"aiogram: {report.aiogram}")
        if report.token_present:
            io.info(f"token: {report.token_masked}")
            if not io.confirm("Replace the token?", False):
                return
        token = io.hidden("Enter Telegram bot token (hidden)")
        if not token:
            io.info("token entry cancelled")
            return
        from eurus.bootstrap.integrations.telegram import validate_telegram_token

        problem = validate_telegram_token(token)
        if problem:
            io.error(problem)
            return
        path = save_telegram_token(self.environ_effective(), token)
        io.ok(f"saved to {path} (0600)")
        if io.confirm("Test the token against api.telegram.org?", True):
            ok, detail = test_telegram_token(token)
            (io.ok if ok else io.error)(f"telegram: {detail}")
        allowlist = io.ask("Allowed Telegram user IDs (comma separated)", report.allowlist or "")
        if allowlist.strip() and all(
            item.strip().lstrip("-").isdigit() for item in allowlist.split(",")
        ):
            save_allowlist(self.environ_effective(), allowlist)
            io.ok("allowlist saved")
        elif allowlist.strip():
            io.error("allowlist must be numeric user IDs; not saved")

    def module_configuration(self) -> None:
        open_configuration_manager(self.io, self.environ_effective())

    def module_startup(self) -> None:
        io = self.io
        mechanisms = detect_mechanisms(self.environ_effective())
        io.info(
            "mechanisms - systemd: "
            f"{mechanisms.systemd}, user manager: {mechanisms.systemd_user_manager}, "
            f"termux-boot: {mechanisms.termux_boot}, cron: {mechanisms.cron}"
        )
        io.info(f"recommended: {mechanisms.recommended}")
        for item in status_all(self.environ_effective()):
            if item.installed:
                io.ok(f"{item.service}: {item.state} via {item.mechanism} ({item.path})")
            else:
                io.echo(f"  {item.service}: not installed")
        if not io.confirm("Remove all eurus services (autostart scripts and units)?", False):
            return
        removed = uninstall_all(self.environ_effective())
        if removed:
            for item in removed:
                io.ok(f"removed: {item}")
        else:
            io.info("nothing to remove")
        io.info("install services with: eurus setup startup")

    def module_diagnostics(self) -> None:
        io = self.io
        report = run_checks(self.environ)
        render_report(report, io.console, title="eurus diagnostics")
        if io.confirm("Probe the configured model over the network? (sends a tiny request)", False):
            result = self._probe_model()
            io.console.print(result)

    def module_repair(self) -> None:
        io = self.io
        io.echo("1. Repair detected problems")
        io.echo("2. Update installed packages")
        choice = io.choose(
            "Select action", ["Repair detected problems", "Update installed packages"], 0
        )
        if choice == 0:
            self.orchestrator.run_repair()
        else:
            self.orchestrator.run_update()

    def _probe_model(self) -> str:
        import asyncio

        from eurus.diagnose import probe_model
        from eurus.errors import EurusError
        from eurus.models import ModelRef
        from eurus.providers.httpx_transport import HttpxTransport

        config = load_config_safe(self.environ_effective())
        if config is None or not config.agent.model:
            return "no model configured; run eurus setup provider first"
        try:
            ref = ModelRef.parse(config.agent.model)
        except EurusError as exc:
            return f"invalid model: {exc}"

        async def _probe() -> Any:
            transport = HttpxTransport(timeout=45.0)
            try:
                return await probe_model(
                    config, ref, transport=transport, environ=self.environ_effective()
                )
            finally:
                await transport.aclose()

        try:
            result = asyncio.run(_probe())
        except Exception as exc:
            return f"probe failed: {type(exc).__name__}: {exc}"
        return f"{result.model}: {'ok' if result.ok else 'failed'} - {result.detail}"

    def environ_effective(self) -> dict[str, str]:
        import os

        return dict(os.environ) if self.environ is None else dict(self.environ)


def open_configuration_manager(io: BootstrapIO, environ: dict[str, str]) -> None:
    from eurus import paths
    from eurus.bootstrap.configuration.toml_manager import TomlManager

    path = paths.user_config_path(environ)
    manager = TomlManager.load(path)
    io.info(f"configuration file: {path}")
    config = load_config_safe(environ)
    if config is None:
        io.error("current configuration is invalid; fix it before making changes here")
        return
    while True:
        io.echo()
        io.echo(f"  model: {config.agent.model or '(not set)'}")
        io.echo(f"  permission mode: {config.permissions.mode}")
        io.echo(
            f"  network timeout: {config.network.timeout_seconds:g}s,"
            f" local only: {config.network.local_only}"
        )
        io.echo(f"  browser: enabled={config.browser.enabled}, backend={config.browser.backend}")
        io.echo("  1) Set default model     2) Permission mode     3) Network timeout")
        io.echo("  4) Toggle browser        5) Validate            0) Back")
        choice = io.ask("Choice", "0")
        if choice in {"0", "", "q"}:
            return
        try:
            if choice == "1":
                model = io.ask("Model (provider/model)", config.agent.model)
                if model.strip() and "/" in model:
                    manager.update_section("agent", {"model": model.strip()})
            elif choice == "2":
                mode = io.ask("Permission mode (ask/auto-edit/yolo)", config.permissions.mode)
                if mode.strip() in {"ask", "auto-edit", "yolo"}:
                    manager.update_section("permissions", {"mode": mode.strip()})
            elif choice == "3":
                raw = io.ask("Network timeout seconds", str(config.network.timeout_seconds))
                try:
                    value = float(raw)
                    if 1 <= value <= 600:
                        manager.update_section("network", {"timeout_seconds": value})
                except ValueError:
                    io.error("invalid number")
            elif choice == "4":
                manager.update_section("browser", {"enabled": not config.browser.enabled})
            elif choice == "5":
                try:
                    manager.validate(environ)
                    io.ok("configuration is valid")
                except Exception as exc:
                    io.error(f"validation failed: {exc}")
                continue
            else:
                continue
            manager.save(environ=environ)
            io.ok(f"saved {path}")
            config = load_config_safe(environ) or config
        except KeyboardInterrupt:
            return
        except Exception as exc:
            io.error(f"cannot save: {exc}")
