from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path

BOOT_DIR_NAME = ".termux/boot"
SERVICES_DIR_NAME = "services"
LOG_DIR_PARTS = ("bootstrap", "logs")


def boot_scripts_dir(environ: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if environ is None else dict(environ)
    home = Path(env.get("HOME", str(Path.home())))
    return home / BOOT_DIR_NAME


def real_home(environ: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if environ is None else dict(environ)
    return Path(env.get("HOME", str(Path.home())))


def termux_boot_available(environ: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if environ is None else dict(environ)
    from eurus.termux import is_termux

    if not is_termux(env):
        return "no"
    if boot_scripts_dir(env).is_dir():
        return "yes"
    prefix = env.get("PREFIX", "")
    if prefix and (Path(prefix) / "etc" / "termux-boot").is_dir():
        return "yes"
    return "unknown"


def services_dir(home: Path) -> Path:
    return home / SERVICES_DIR_NAME


def render_boot_script(
    *,
    prefix: str,
    home: Path,
    python_exe: str,
    enabled: list[str],
    eurus_home: Path | None = None,
) -> str:
    data_home = eurus_home or home / ".eurus"
    log_dir = data_home.joinpath(*LOG_DIR_PARTS)
    lines = [
        f"#!{prefix}/bin/sh",
        "termux-wake-lock 2>/dev/null",
        "export PATH=" + shlex.quote(f"{prefix}/bin:$PATH"),
        f"export HOME={shlex.quote(str(home))}",
        f"export EURUS_HOME={shlex.quote(str(data_home))}",
        f"mkdir -p {shlex.quote(str(log_dir))}",
        "",
    ]
    for index, service in enumerate(enabled):
        if index:
            lines.append("sleep 3")
        log = log_dir / f"{service}.log"
        command = f"{shlex.quote(python_exe)} -m eurus {_service_cli(service)}"
        lines.append(f"{command} >> {shlex.quote(str(log))} 2>&1 &")
    lines.append("")
    return "\n".join(lines)


def render_launcher(
    *,
    python_exe: str,
    home: Path,
    service: str,
    eurus_home: Path | None = None,
) -> str:
    data_home = eurus_home or home / ".eurus"
    log_dir = data_home.joinpath(*LOG_DIR_PARTS)
    return "\n".join(
        [
            "#!/bin/sh",
            f"export HOME={shlex.quote(str(home))}",
            f"export EURUS_HOME={shlex.quote(str(data_home))}",
            f"mkdir -p {shlex.quote(str(log_dir))}",
            f"exec {shlex.quote(python_exe)} -m eurus {_service_cli(service)}",
            f"  >> {shlex.quote(str(log_dir / f'{service}.log'))} 2>&1",
            "",
        ]
    )


_SERVICE_COMMANDS = {
    "telegram-gateway": ("gateway", "telegram"),
    "scheduler": ("schedule", "daemon"),
}


def _service_cli(service: str) -> str:
    command = _SERVICE_COMMANDS.get(service)
    if command is None:
        raise ValueError(f"unknown service: {service}")
    return " ".join(command)


def write_boot_script(
    environ: dict[str, str],
    python_exe: str,
    home: Path,
    enabled: list[str],
    eurus_home: Path | None = None,
) -> Path:
    prefix = environ.get("PREFIX", "/data/data/com.termux/files/usr")
    directory = boot_scripts_dir(environ)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "eurus-bootstrap.sh"
    path.write_text(
        render_boot_script(
            prefix=prefix,
            home=home,
            python_exe=python_exe,
            enabled=enabled,
            eurus_home=eurus_home,
        ),
        encoding="utf-8",
    )
    _make_executable(path)
    return path


def write_launcher(base: Path, python_exe: str, service: str, home: Path | None = None) -> Path:
    directory = services_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"start-{service}.sh"
    path.write_text(
        render_launcher(
            python_exe=python_exe, home=home or real_home(), service=service, eurus_home=base
        ),
        encoding="utf-8",
    )
    _make_executable(path)
    return path


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def remove_boot_script(environ: dict[str, str] | None = None) -> str:
    path = boot_scripts_dir(environ) / "eurus-bootstrap.sh"
    if path.exists():
        path.unlink()
        return str(path)
    return ""


def remove_launcher(home: Path, service: str) -> str:
    path = services_dir(home) / f"start-{service}.sh"
    if path.exists():
        path.unlink()
        return str(path)
    return ""


def boot_script_installed(environ: dict[str, str] | None = None) -> bool:
    return (boot_scripts_dir(environ) / "eurus-bootstrap.sh").is_file()


def launcher_installed(home: Path, service: str) -> bool:
    return (services_dir(home) / f"start-{service}.sh").is_file()
