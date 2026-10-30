from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.bootstrap.models import UNKNOWN, BootstrapError
from eurus.bootstrap.startup import proot as proot_backend
from eurus.bootstrap.startup import systemd as systemd_backend
from eurus.bootstrap.startup import termux as termux_backend

SERVICE_NAMES: tuple[str, ...] = ("telegram-gateway", "scheduler")

SERVICE_DESCRIPTIONS = {
    "telegram-gateway": "Eurus Telegram Gateway",
    "scheduler": "Eurus Scheduler Daemon",
}


def data_home(environ: dict[str, str] | None = None) -> Path:
    from eurus import paths

    return paths.home_dir(environ)


def service_env_lines(environ: dict[str, str] | None = None) -> list[str]:
    return [f"EURUS_HOME={data_home(environ)}"]


def service_command(service: str) -> list[str]:
    if service == "telegram-gateway":
        return [sys.executable, "-m", "eurus", "gateway", "telegram"]
    if service == "scheduler":
        return [sys.executable, "-m", "eurus", "schedule", "daemon"]
    raise BootstrapError(f"unknown service: {service}")


def validate_service(service: str) -> None:
    if service not in SERVICE_NAMES:
        raise BootstrapError(
            f"unknown service: {service} (expected one of {', '.join(SERVICE_NAMES)})"
        )


@dataclass(slots=True)
class StartupMechanisms:
    systemd: bool = False
    systemd_user_manager: bool = False
    termux_boot: str = UNKNOWN
    cron: bool = False
    recommended: str = "manual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "systemd": self.systemd,
            "systemd_user_manager": self.systemd_user_manager,
            "termux_boot": self.termux_boot,
            "cron": self.cron,
            "recommended": self.recommended,
        }


@dataclass(slots=True)
class ServiceStatus:
    service: str
    mechanism: str
    installed: bool
    state: str
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "mechanism": self.mechanism,
            "installed": self.installed,
            "state": self.state,
            "path": self.path,
        }


def detect_mechanisms(environ: dict[str, str] | None = None) -> StartupMechanisms:
    env = dict(os.environ) if environ is None else dict(environ)
    from eurus.termux import is_termux

    mechanisms = StartupMechanisms()
    mechanisms.systemd = systemd_backend.systemd_available()
    mechanisms.systemd_user_manager = mechanisms.systemd and systemd_backend.user_manager_running()
    mechanisms.termux_boot = termux_backend.termux_boot_available(env)
    mechanisms.cron = proot_backend.crontab_available()
    if is_termux(env):
        if mechanisms.termux_boot == "yes":
            mechanisms.recommended = "termux-boot"
        else:
            mechanisms.recommended = "termux-manual"
    elif mechanisms.systemd_user_manager:
        mechanisms.recommended = "systemd"
    elif mechanisms.cron:
        mechanisms.recommended = "cron"
    else:
        mechanisms.recommended = "manual"
    return mechanisms


def recommend_backend(platform_variant: str, mechanisms: StartupMechanisms) -> str:
    if platform_variant == "termux":
        return "termux-boot" if mechanisms.termux_boot == "yes" else "termux-manual"
    if mechanisms.systemd_user_manager:
        return "systemd"
    if mechanisms.cron:
        return "cron"
    return "manual"


def install_systemd(
    service: str,
    *,
    workdir: str,
    enable: bool,
    start: bool,
    env_lines: list[str] | None = None,
) -> Path:
    validate_service(service)
    command = service_command(service)
    unit_text = systemd_backend.render_unit(
        service=service,
        description=SERVICE_DESCRIPTIONS[service],
        command=command,
        workdir=workdir,
        env_lines=env_lines or [],
    )
    path = systemd_backend.write_unit(service, unit_text)
    systemd_backend.daemon_reload()
    if enable:
        systemd_backend.enable_unit(service)
    if start:
        systemd_backend.start_unit(service)
    return path


def install_termux_boot(
    environ: dict[str, str],
    enabled: list[str],
) -> Path:
    if not enabled:
        raise BootstrapError("no services selected")
    real = Path(environ.get("HOME", str(Path.home())))
    path = termux_backend.write_boot_script(
        environ, sys.executable, real, enabled, eurus_home=data_home(environ)
    )
    return path


def install_launchers(
    environ: dict[str, str], services: list[str], *, eurus_home: Path | None = None
) -> list[Path]:
    base = eurus_home or data_home(environ)
    real = Path(environ.get("HOME", str(Path.home())))
    paths = []
    for service in services:
        paths.append(termux_backend.write_launcher(base, sys.executable, service, home=real))
    return paths


def install_cron(
    services: list[str],
    *,
    eurus_home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[bool, str]:
    if not proot_backend.crontab_available():
        return False, "crontab is not available"
    base = eurus_home or data_home(environ)
    entries = []
    for service in services:
        launcher = base / "services" / f"start-{service}.sh"
        entries.append(proot_backend.cron_entry(str(launcher)))
    return proot_backend.upsert_cron_block(entries)


def uninstall(service: str, environ: dict[str, str] | None = None) -> list[str]:
    validate_service(service)
    env = dict(os.environ) if environ is None else dict(environ)
    removed: list[str] = []
    unit = systemd_backend.uninstall_unit(service)
    if unit:
        removed.append(unit)
    launcher = termux_backend.remove_launcher(data_home(env), service)
    if launcher:
        removed.append(launcher)
    return removed


def uninstall_all(environ: dict[str, str] | None = None) -> list[str]:
    env = dict(os.environ) if environ is None else dict(environ)
    removed: list[str] = []
    for service in SERVICE_NAMES:
        path = systemd_backend.uninstall_unit(service)
        if path:
            removed.append(path)
        path = termux_backend.remove_launcher(data_home(env), service)
        if path:
            removed.append(path)
    boot = termux_backend.remove_boot_script(env)
    if boot:
        removed.append(boot)
    ok, message = proot_backend.upsert_cron_block([])
    if ok:
        removed.append(f"cron: {message}")
    return removed


def status_all(environ: dict[str, str] | None = None) -> list[ServiceStatus]:
    env = dict(os.environ) if environ is None else dict(environ)
    mechanisms = detect_mechanisms(env)
    results: list[ServiceStatus] = []
    for service in SERVICE_NAMES:
        if mechanisms.systemd and systemd_backend.unit_installed(service):
            results.append(
                ServiceStatus(
                    service=service,
                    mechanism="systemd",
                    installed=True,
                    state=systemd_backend.is_active(service),
                    path=str(systemd_backend.unit_path(service)),
                )
            )
            continue
        launcher = data_home(env) / "services" / f"start-{service}.sh"
        if launcher.is_file():
            results.append(
                ServiceStatus(
                    service=service,
                    mechanism="script",
                    installed=True,
                    state="manual",
                    path=str(launcher),
                )
            )
            continue
        results.append(
            ServiceStatus(
                service=service,
                mechanism=mechanisms.recommended,
                installed=False,
                state="not installed",
            )
        )
    boot_script = termux_backend.boot_script_installed(env)
    if boot_script:
        results.append(
            ServiceStatus(
                service="termux-boot",
                mechanism="termux-boot",
                installed=True,
                state="configured",
                path=str(termux_backend.boot_scripts_dir(env) / "eurus-bootstrap.sh"),
            )
        )
    return results
