from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eurus.bootstrap.configuration.env_manager import ensure_global_env
from eurus.bootstrap.configuration.provider_wizard import run_provider_setup
from eurus.bootstrap.configuration.secrets import validate_telegram_token
from eurus.bootstrap.dependencies.catalog import FEATURES, feature_status
from eurus.bootstrap.dependencies.inspector import inspect_all, inspect_core
from eurus.bootstrap.dependencies.installer import install_targets, upgrade_targets
from eurus.bootstrap.dependencies.resolver import plan_for_features, plan_update
from eurus.bootstrap.dependencies.verifier import verify_features
from eurus.bootstrap.detection.capabilities import inspect_environment
from eurus.bootstrap.health.checks import load_config_safe, run_checks
from eurus.bootstrap.health.repair import actions_for, execute_repair
from eurus.bootstrap.health.report import render_report
from eurus.bootstrap.integrations.browser import detect_browser
from eurus.bootstrap.integrations.documents import inspect_documents
from eurus.bootstrap.integrations.mcp import ensure_mcp_file, inspect_mcp, test_mcp_servers
from eurus.bootstrap.integrations.telegram import (
    inspect_telegram,
    save_allowlist,
    save_telegram_token,
    test_telegram_token,
)
from eurus.bootstrap.models import (
    CONFIG_UNSET,
    READY,
    BootstrapError,
    ConfigUnset,
    HealthReport,
    SkipStep,
)
from eurus.bootstrap.startup.manager import (
    SERVICE_NAMES,
    detect_mechanisms,
    install_cron,
    install_launchers,
    install_systemd,
    install_termux_boot,
)
from eurus.bootstrap.state.backup import create_backup_dir
from eurus.bootstrap.state.store import (
    BootstrapStore,
    mark_setup_finished,
    mark_setup_started,
    mark_setup_step,
)
from eurus.bootstrap.wizard import BootstrapIO
from eurus.config import Config

QUICK_STEPS: tuple[str, ...] = (
    "platform",
    "python",
    "venv",
    "dependencies",
    "features",
    "install",
    "provider",
    "environment",
    "browser",
    "integrations",
    "startup",
    "health",
    "report",
)


class BootstrapContext:
    io: BootstrapIO
    environ: dict[str, str]
    store: BootstrapStore
    config: Config | None
    config_error: str
    report: HealthReport | None
    home: Path
    repair_planned: list[str]
    repair_outcomes: list[tuple[str, bool, str]]
    repair_cancelled: bool

    def __init__(
        self, io: BootstrapIO, environ: dict[str, str] | None = None, home: Path | None = None
    ) -> None:
        self.io = io
        self.environ = dict(os.environ) if environ is None else dict(environ)
        from eurus import paths

        self.home = home or paths.home_dir(self.environ)
        self.store = BootstrapStore(self.home)
        self.config = load_config_safe(self.environ)
        self.config_error = ""
        self.report = None
        self.repair_planned = []
        self.repair_outcomes = []
        self.repair_cancelled = False


class BootstrapOrchestrator:
    def __init__(
        self,
        io: BootstrapIO,
        environ: dict[str, str] | None = None,
        home: Path | None = None,
        config: Config | None | ConfigUnset = CONFIG_UNSET,
        config_error: str = "",
    ) -> None:
        self.ctx = BootstrapContext(io, environ, home)
        if not isinstance(config, ConfigUnset):
            self.ctx.config = config
            self.ctx.config_error = config_error

    @property
    def io(self) -> BootstrapIO:
        return self.ctx.io

    def run_check(self, *, render: bool = True) -> HealthReport:
        report = run_checks(
            self.ctx.environ, config=self.ctx.config, config_error=self.ctx.config_error
        )
        self.ctx.report = report
        if render:
            render_report(report, self.io.console)
        return report

    def _save_check_state(self, report: HealthReport) -> None:
        state = self.ctx.store.load_state() or {"schema_version": 1}
        state["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["doctor"] = {"summary": report.summary, "checked_at": state["last_checked"]}
        environment = inspect_environment(self.ctx.environ)
        state["platform"] = environment.platform.distro_id or environment.platform.system
        state["architecture"] = environment.architecture.normalized
        state["python"] = {
            "version": environment.python.version,
            "venv": environment.python.is_venv,
        }
        self.ctx.store.save_state(state)
        self.ctx.store.save_platform(environment.to_dict())
        packages = inspect_all()
        self.ctx.store.save_dependencies({name: item.to_dict() for name, item in packages.items()})
        self.ctx.store.append_history({"action": "check", "status": report.summary})

    def run_quick_setup(self, *, render: bool = True) -> HealthReport:
        io = self.io
        self._print_banner()
        interrupted = self.ctx.store.interrupted_setup()
        if interrupted is not None:
            io.warn(
                "a previous quick setup did not finish; steps are idempotent and will be re-run"
            )
        state = self.ctx.store.load_state() or {"schema_version": 1}
        mark_setup_started(state, list(QUICK_STEPS))
        self.ctx.store.save_state(state)
        self.ctx.store.append_history({"action": "quick_setup", "status": "started"})
        try:
            self._step_platform(state)
            self._step_python(state)
            self._step_venv(state)
            self._step_dependencies(state)
            selected = self._step_features(state)
            self._step_install(state, selected)
            self._step_provider(state)
            self._step_environment(state)
            self._step_browser(state)
            self._step_integrations(state, selected)
            self._step_startup(state)
            report = self._step_health(state)
            self._mark(state, "report", "done")
            mark_setup_finished(state, "done")
            self.ctx.store.save_state(state)
            self.ctx.store.append_history({"action": "quick_setup", "status": "done"})
            self._final_report(report, render=render)
            return report
        except KeyboardInterrupt:
            mark_setup_finished(state, "interrupted")
            self.ctx.store.save_state(state)
            self.ctx.store.append_history({"action": "quick_setup", "status": "interrupted"})
            io.warn("setup interrupted; progress saved and can be resumed")
            raise
        except SkipStep as exc:
            mark_setup_finished(state, "skipped")
            self.ctx.store.save_state(state)
            io.warn(f"setup step skipped: {exc}")
            report = run_checks(
                self.ctx.environ, config=self.ctx.config, config_error=self.ctx.config_error
            )
            if render:
                render_report(report, io.console)
            return report

    def run_repair(self, *, render: bool = True) -> HealthReport:
        io = self.io
        if render:
            io.console.print("[bold]REPAIR[/bold]")
        self.ctx.repair_planned = []
        self.ctx.repair_outcomes = []
        self.ctx.repair_cancelled = False
        report = run_checks(
            self.ctx.environ, config=self.ctx.config, config_error=self.ctx.config_error
        )
        self.ctx.report = report
        actions = actions_for(report.results)
        self.ctx.repair_planned = [action.key for action in actions]
        if not actions:
            if render:
                io.ok("nothing to repair; all mandatory checks pass")
                render_report(report, io.console)
            return report
        if render:
            io.console.print("\n[bold]Planned changes:[/bold]")
            for index, action in enumerate(actions, start=1):
                io.console.print(f"  {index}. [{action.key}] {action.title}")
                io.console.print(f"     {action.description}")
        if not io.confirm("\nApply these repairs?", False):
            if render:
                io.info("repair cancelled; no changes were made")
            self.ctx.repair_cancelled = True
            return report
        backup_dir = create_backup_dir(self.ctx.home / "bootstrap" / "backups", "repair")
        results = execute_repair(actions, self.ctx.environ, backup_dir)
        self.ctx.repair_outcomes = list(results)
        if render:
            io.echo()
            for key, ok, message in results:
                if ok:
                    io.ok(f"repaired [{key}]: {message}")
                else:
                    io.error(f"failed [{key}]: {message}")
            io.echo()
        verify_report = run_checks(
            self.ctx.environ, config=self.ctx.config, config_error=self.ctx.config_error
        )
        if render:
            render_report(verify_report, io.console, title="eurus doctor (after repair)")
        self.ctx.store.append_history(
            {
                "action": "repair",
                "status": "done" if all(ok for _, ok, _ in results) else "partial",
                "detail": str(len(results)),
            }
        )
        self._save_check_state(verify_report)
        return verify_report

    def run_update(self) -> None:
        io = self.io
        io.console.print("[bold]UPDATE PACKAGES[/bold]")
        packages = inspect_all()
        plan = plan_update(packages)
        if plan.empty:
            io.warn("no installed feature packages to update")
            return
        io.info("packages: " + ", ".join(plan.targets))
        if not io.confirm("Update the packages listed above with pip?", False):
            io.info("update cancelled")
            return
        with io.spinner("updating packages"):
            outcome = upgrade_targets(list(plan.targets), environ=self.ctx.environ)
        if outcome.success:
            io.ok("update finished")
            statuses = verify_features()
            for item in statuses:
                io.status_line(
                    item.label,
                    item.status if item.status != "OPTIONAL" else "SKIP",
                    item.description,
                )
        else:
            io.error(f"update failed (exit {outcome.returncode}):")
            io.echo(outcome.output_tail)
        self.ctx.store.append_history(
            {"action": "update", "status": "done" if outcome.success else "failed"}
        )

    def _print_banner(self) -> None:
        self.io.console.print(
            Panel(
                Text.assemble(
                    ("EURUS\n", "bold"), ("BOOTSTRAP & ENVIRONMENT MANAGER", "bright_black")
                ),
                expand=False,
            )
        )

    def _mark(self, state: dict[str, Any], step: str, status: str) -> None:
        mark_setup_step(state, step, status)
        self.ctx.store.save_state(state)

    def _step_platform(self, state: dict[str, Any]) -> None:
        environment = inspect_environment(self.ctx.environ)
        platform = environment.platform
        self.io.ok(f"platform: {platform.label} ({environment.architecture.normalized})")
        if platform.confidence != "high":
            self.io.warn("platform detection confidence is medium; automatic mutations are limited")
        self.ctx.store.save_platform(environment.to_dict())
        self._mark(state, "platform", "done")

    def _step_python(self, state: dict[str, Any]) -> None:
        from eurus.bootstrap.detection.python import inspect_python

        info = inspect_python(self.ctx.environ)
        if not info.supported:
            raise BootstrapError(f"python {info.version} is not supported; install python 3.11+")
        if not info.pip_available:
            raise BootstrapError("pip is unavailable for the active interpreter; install pip first")
        self.io.ok(f"python: {info.version} (pip {info.pip_version or 'unknown'})")
        self._mark(state, "python", "done")

    def _step_venv(self, state: dict[str, Any]) -> None:
        from eurus.bootstrap.detection.python import detect_venv

        is_venv, path = detect_venv(self.ctx.environ)
        if is_venv:
            self.io.ok(f"virtual environment: active ({path})")
        else:
            self.io.warn(
                "virtual environment: not active; pip installs will target the current interpreter"
            )
        self._mark(state, "venv", "done")

    def _step_dependencies(self, state: dict[str, Any]) -> None:
        packages = inspect_core()
        problems = [item for item in packages.values() if item.status != READY]
        if problems:
            detail = ", ".join(item.name for item in problems)
            self.io.warn(f"core dependencies need attention: {detail}")
            self._mark(state, "dependencies", "repair-needed")
        else:
            self.io.ok(f"core dependencies: {len(packages)} packages ok")
            self._mark(state, "dependencies", "done")

    def _step_features(self, state: dict[str, Any]) -> list[str]:
        io = self.io
        packages = inspect_all()
        statuses = [feature_status(spec, packages) for spec in FEATURES]
        table = Table(box=None, pad_edge=False)
        table.add_column("#", justify="right", width=3)
        table.add_column("Feature", style="bold")
        table.add_column("Status", width=9)
        table.add_column("Packages")
        for index, item in enumerate(statuses, start=1):
            status_style = "green" if item.status == READY else "yellow"
            packages_text = ", ".join(_short_specs(item)) or "-"
            table.add_row(
                str(index),
                item.label,
                f"[{status_style}]{item.status}[/{status_style}]",
                packages_text,
            )
        io.console.print(table)
        selected: list[str] = []
        if io.interactive():
            io.echo()
            io.info(
                "select optional features to install"
                " (comma separated numbers, empty to skip, 'a' for all)"
            )
            raw = io.ask("Features to install", "")
            raw = raw.strip().lower()
            if raw == "a":
                selected = [item.feature for item in statuses if item.status != READY]
            elif raw:
                for token in raw.replace(" ", "").split(","):
                    if token.isdigit() and 1 <= int(token) <= len(statuses):
                        feature = statuses[int(token) - 1]
                        if feature.status != READY and feature.feature not in selected:
                            selected.append(feature.feature)
        else:
            io.info("non-interactive mode: skipping optional feature selection")
        self._mark(state, "features", "done")
        return selected

    def _step_install(self, state: dict[str, Any], selected: list[str]) -> None:
        io = self.io
        packages = inspect_all()
        plan = plan_for_features(tuple(selected), packages)
        core_problems = [item for item in inspect_core().values() if item.status != READY]
        core_targets = [item.required_spec for item in core_problems]
        if not plan.targets and not core_targets:
            io.ok("python packages: nothing to install")
            self._mark(state, "install", "done")
            return
        if core_targets:
            io.warn("repairing core packages: " + ", ".join(core_targets))
        if plan.targets:
            io.info("will install: " + ", ".join(plan.targets))
        if not io.confirm("Install the packages listed above?", True):
            io.info("installation skipped")
            self._mark(state, "install", "skipped")
            return
        targets = list(core_targets) + list(plan.targets)
        with io.spinner("running pip (this can take several minutes)"):
            outcome = install_targets(targets, environ=self.ctx.environ)
        io.echo()
        if outcome.success:
            io.ok("pip install finished")
            for line in outcome.output_tail.splitlines()[-3:]:
                io.info(line)
            self._mark(state, "install", "done")
        else:
            io.error(f"pip install failed (exit {outcome.returncode})")
            for line in outcome.output_tail.splitlines()[-6:]:
                io.echo(f"  {line}")
            self._mark(state, "install", "failed")
            io.warn("you can re-run bootstrap later; installation is idempotent")

    def _step_provider(self, state: dict[str, Any]) -> None:
        io = self.io
        config = load_config_safe(self.ctx.environ)
        model = config.agent.model if config else ""
        if model:
            io.info(f"current default model: {model}")
            if not io.confirm("Configure an AI provider now?", False):
                self._mark(state, "provider", "skipped")
                return
        else:
            io.info("no default model configured")
            if not io.confirm("Configure an AI provider now?", True):
                io.info("you can run eurus setup provider at any time")
                self._mark(state, "provider", "skipped")
                return
        result = run_provider_setup(io, config, self.ctx.environ)
        if result.model:
            self._mark(state, "provider", "done")
        else:
            self._mark(state, "provider", "skipped")

    def _step_environment(self, state: dict[str, Any]) -> None:
        io = self.io
        path, created = ensure_global_env(self.ctx.environ)
        if created:
            io.ok(f"created global environment file: {path}")
        else:
            io.ok(f"global environment file: {path}")
        from eurus.bootstrap.configuration.env_manager import tighten_permissions

        if tighten_permissions(path):
            io.ok("permissions set to 0600")
        if io.confirm("Open the configuration manager now?", False):
            from eurus.bootstrap.dashboard import open_configuration_manager

            open_configuration_manager(io, self.ctx.environ)
        self._mark(state, "environment", "done")

    def _step_browser(self, state: dict[str, Any]) -> None:
        io = self.io
        report = detect_browser(self.ctx.environ)
        if report.chromium_kind == "missing":
            io.warn("browser: no chromium executable found")
            io.info(
                "install chromium with your system package manager, or install playwright"
                " and run: python -m playwright install chromium"
            )
            if report.playwright_package == READY and io.confirm(
                "Download Playwright chromium now (~150 MB)?", False
            ):
                from eurus.bootstrap.integrations.browser import playwright_install_chromium

                with io.spinner("down playwright chromium"):
                    code, tail = playwright_install_chromium()
                if code == 0:
                    io.ok("playwright chromium installed")
                    report = detect_browser(self.ctx.environ)
                else:
                    io.error(f"playwright install failed: {tail}")
        else:
            io.ok(f"browser: {report.chromium_kind} chromium at {report.chromium_path}")
            io.info(f"recommended backend: {report.backend_recommendation}")
        enabled_config = False
        if self.ctx.config is not None:
            enabled_config = self.ctx.config.browser.enabled
        confirm_browser = report.chromium_kind != "missing" and not enabled_config
        if confirm_browser and io.confirm("Enable browser tools in config.toml?", False):
            self._enable_browser(report)
        self._mark(state, "browser", "done")

    def _enable_browser(self, report: Any) -> None:
        from eurus import paths
        from eurus.bootstrap.configuration.toml_manager import TomlManager

        path = paths.user_config_path(self.ctx.environ)
        manager = TomlManager.load(path)
        updates: dict[str, Any] = {"browser": {"enabled": True}}
        if report.chromium_path and report.chromium_kind == "system":
            updates["browser"]["chromium_executable"] = report.chromium_path
        manager.merge(updates)
        try:
            manager.save(environ=self.ctx.environ)
            self.io.ok(f"browser enabled in {path}")
        except Exception as exc:
            self.io.error(f"cannot update {path}: {exc}")

    def _step_integrations(self, state: dict[str, Any], selected: list[str]) -> None:
        io = self.io
        handled = False
        review_mcp = "mcp" in selected or inspect_mcp(self.ctx.environ).servers
        if review_mcp and io.confirm("Review MCP configuration?", False):
            self._mcp_module()
            handled = True
        if "telegram" in selected and io.confirm("Configure Telegram now?", True):
            self._telegram_module()
            handled = True
        if "documents" in selected:
            report = inspect_documents()
            io.status_line(
                "documents",
                report.creation_test if report.creation_test != "UNKNOWN" else "OK",
                report.detail,
            )
            handled = True
        if not handled:
            io.info("integrations: nothing selected; run the dashboard modules any time")
        self._mark(state, "integrations", "done")

    def _step_startup(self, state: dict[str, Any]) -> None:
        io = self.io
        mechanisms = detect_mechanisms(self.ctx.environ)
        io.info(f"startup mechanism available: {mechanisms.recommended}")
        if not io.confirm("Configure a startup service now?", False):
            io.info("you can run eurus setup startup at any time")
            self._mark(state, "startup", "skipped")
            return
        self._startup_module()
        self._mark(state, "startup", "done")

    def _step_health(self, state: dict[str, Any]) -> HealthReport:
        io = self.io
        io.echo()
        io.console.print("[bold]HEALTH CHECK[/bold]")
        report = run_checks(
            self.ctx.environ, config=self.ctx.config, config_error=self.ctx.config_error
        )
        self.ctx.report = report
        self._mark(state, "health", "done")
        return report

    def _final_report(self, report: HealthReport, *, render: bool = True) -> None:
        io = self.io
        if not render:
            return
        io.echo()
        render_report(report, io.console, title="eurus bootstrap report")
        io.echo()
        if report.summary == "READY":
            io.ok("EURUS is ready. Start with: eurus chat")
        elif report.summary == "BLOCKED":
            io.error("mandatory problems remain; run: eurus bootstrap --repair")
        else:
            io.info("optional items remain; run: eurus bootstrap for the dashboard")

    def _mcp_module(self) -> None:
        io = self.io
        path = ensure_mcp_file(self.ctx.environ)
        report = inspect_mcp(self.ctx.environ)
        if not report.valid:
            io.error(f"mcp.json is invalid: {report.error}")
            if io.confirm("Back up and reset mcp.json?", True):
                from eurus.bootstrap.health.repair import apply_repair_action
                from eurus.bootstrap.models import RepairAction

                action = RepairAction(
                    key="mcp_reset", title="reset mcp.json", description="", kind="mcp_reset"
                )
                ok, message = apply_repair_action(action, self.ctx.environ)
                (io.ok if ok else io.error)(message)
            return
        if not report.servers:
            io.info(f"{path} has no servers; add one with: eurus mcp add-stdio <name> <command>")
            return
        io.info("testing configured servers (each server command will be started)...")
        tested = test_mcp_servers(self.ctx.environ)
        for item in tested:
            style = "green" if item.connection == READY else "red"
            io.console.print(
                f"  [{style}]{item.name}[/{style}] ({item.transport}, {item.connection},"
                f" {item.tools} tools) {item.detail}"
            )

    def _telegram_module(self) -> None:
        from eurus.bootstrap.integrations.telegram import TELEGRAM_TOKEN_ENV

        io = self.io
        report = inspect_telegram(self.ctx.environ)
        if report.token_present:
            io.info(f"token already configured ({report.token_masked})")
            if not io.confirm("Replace the token?", False):
                token = self.ctx.environ.get(TELEGRAM_TOKEN_ENV, "").strip()
            else:
                token = self._ask_token(io)
        else:
            token = self._ask_token(io)
        if token:
            path = save_telegram_token(self.ctx.environ, token)
            io.ok(f"token saved to {path} (0600)")
            if io.confirm("Test the token against api.telegram.org?", True):
                ok, detail = test_telegram_token(token)
                (io.ok if ok else io.error)(f"telegram test: {detail}")
        allowlist = io.ask(
            "Telegram user IDs allowed to use the bot (comma separated)", report.allowlist or ""
        )
        if allowlist.strip():
            if _validate_allowlist(allowlist):
                save_allowlist(self.ctx.environ, allowlist)
                io.ok(f"allowlist saved: {allowlist}")
            else:
                io.error("allowlist must be numeric Telegram user IDs; not saved")

    def _ask_token(self, io: BootstrapIO) -> str:
        while True:
            token = io.hidden("Enter Telegram bot token (hidden)")
            if not token:
                return ""
            problem = validate_telegram_token(token)
            if not problem:
                return token
            io.error(problem)
            if not io.confirm("Try again?", True):
                return ""

    def _startup_module(self) -> None:
        io = self.io
        mechanisms = detect_mechanisms(self.ctx.environ)
        io.echo("Available services:")
        for index, name in enumerate(SERVICE_NAMES, 1):
            io.echo(f"  {index}. {name} ({SERVICE_DESCRIPTIONS_SHORT[name]})")
        picked = io.ask("Services to enable (comma separated numbers, empty to skip)", "").strip()
        if not picked:
            io.info("no services selected")
            return
        services = []
        for token in picked.replace(" ", "").split(","):
            if token.isdigit() and 1 <= int(token) <= len(SERVICE_NAMES):
                services.append(SERVICE_NAMES[int(token) - 1])
        if not services:
            io.warn("no valid service selected")
            return
        backend = mechanisms.recommended
        if backend == "systemd":
            workdir = str(self.ctx.home)
            env_lines = [f"EURUS_HOME={self.ctx.home}"]
            for service in services:
                path = install_systemd(
                    service, workdir=workdir, enable=True, start=False, env_lines=env_lines
                )
                io.ok(
                    f"installed {path} "
                    f"(enable + start with: systemctl --user enable --now eurus-{service})"
                )
        elif backend == "termux-boot":
            path = install_termux_boot(self.ctx.environ, services)
            io.ok(f"installed termux boot script: {path}")
        elif backend == "cron":
            install_launchers(self.ctx.environ, services, eurus_home=self.ctx.home)
            ok, message = install_cron(services, eurus_home=self.ctx.home)
            (io.ok if ok else io.warn)(message)
        else:
            paths = install_launchers(self.ctx.environ, services, eurus_home=self.ctx.home)
            for path in paths:
                io.ok(f"created launcher: {path}")
            io.info(
                "no autostart mechanism detected;"
                " run the launcher manually or add it to your shell profile"
            )


SERVICE_DESCRIPTIONS_SHORT = {
    "telegram-gateway": "telegram bot gateway",
    "scheduler": "schedule daemon",
}


def _short_specs(status: Any) -> list[str]:
    items = list(status.missing) + list(status.installed)
    return [item.split(">")[0].split("<")[0].split("=")[0] for item in items]


def _validate_allowlist(value: str) -> bool:
    tokens = [item.strip() for item in value.split(",") if item.strip()]
    return bool(tokens) and all(token.lstrip("-").isdigit() for token in tokens)


def quick_setup(io: BootstrapIO, environ: dict[str, str] | None = None) -> HealthReport:
    return BootstrapOrchestrator(io, environ).run_quick_setup()


def check(io: BootstrapIO, environ: dict[str, str] | None = None) -> HealthReport:
    return BootstrapOrchestrator(io, environ).run_check()


def repair(io: BootstrapIO, environ: dict[str, str] | None = None) -> HealthReport:
    return BootstrapOrchestrator(io, environ).run_repair()


def update(io: BootstrapIO, environ: dict[str, str] | None = None) -> None:
    BootstrapOrchestrator(io, environ).run_update()
