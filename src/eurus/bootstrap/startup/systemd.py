from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from eurus.bootstrap.models import UNKNOWN


def _default_user_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


SYSTEMD_USER_DIR = _default_user_dir()


def systemd_available() -> bool:
    return Path("/run/systemd/system").exists() and bool(_systemctl_path())


def user_manager_running() -> bool:
    result = _systemctl(["is-system-running"], timeout=6)
    if result is None:
        return False
    return result.returncode == 0 or "degraded" in (result.stdout or "").strip()


def _systemctl(
    arguments: list[str], timeout: float = 10
) -> subprocess.CompletedProcess[str] | None:
    executable = _systemctl_path()
    if not executable:
        return None
    env = dict(os.environ)
    try:
        return subprocess.run(
            [executable, "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _systemctl_path() -> str:
    for candidate in ("/usr/bin/systemctl", "/bin/systemctl", "/usr/local/bin/systemctl"):
        if Path(candidate).exists():
            return candidate
    return ""


def unit_path(service: str) -> Path:
    return SYSTEMD_USER_DIR / f"eurus-{service}.service"


def write_unit(service: str, unit_text: str) -> Path:
    unit_path(service).parent.mkdir(parents=True, exist_ok=True)
    path = unit_path(service)
    path.write_text(unit_text, encoding="utf-8")
    return path


UNIT_SPECIALS = set("\\\"'$%\n\t ")


def quote_unit_argument(argument: str) -> str:
    if argument and not any(char in UNIT_SPECIALS for char in argument):
        return argument
    escaped = argument.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "$$").replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def quote_unit_environment(line: str) -> str:
    name, separator, value = line.partition("=")
    if not separator:
        return quote_unit_argument(line)
    if value and not any(char in UNIT_SPECIALS for char in value):
        return f"{name}={value}"
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'{name}="{escaped}"'


def render_unit(
    *,
    service: str,
    description: str,
    command: list[str],
    workdir: str,
    env_lines: list[str],
) -> str:
    exec_start = " ".join(quote_unit_argument(part) for part in command)
    environment = "\n".join(f"Environment={quote_unit_environment(line)}" for line in env_lines)
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={quote_unit_argument(workdir)}\n"
        f"ExecStart={exec_start}\n"
        + (f"{environment}\n" if env_lines else "")
        + "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def daemon_reload() -> bool:
    result = _systemctl(["daemon-reload"])
    return result is not None and result.returncode == 0


def enable_unit(service: str) -> bool:
    result = _systemctl(["enable", f"eurus-{service}.service"])
    return result is not None and result.returncode == 0


def start_unit(service: str) -> bool:
    result = _systemctl(["start", f"eurus-{service}.service"])
    return result is not None and result.returncode == 0


def stop_unit(service: str) -> bool:
    result = _systemctl(["stop", f"eurus-{service}.service"])
    return result is not None and result.returncode == 0


def restart_unit(service: str) -> bool:
    result = _systemctl(["restart", f"eurus-{service}.service"])
    return result is not None and result.returncode == 0


def disable_unit(service: str) -> bool:
    result = _systemctl(["disable", f"eurus-{service}.service"])
    return result is not None and result.returncode == 0


def is_active(service: str) -> str:
    result = _systemctl(["is-active", f"eurus-{service}.service"])
    if result is None:
        return UNKNOWN
    output = (result.stdout or "").strip()
    return output if output else UNKNOWN


def unit_installed(service: str) -> bool:
    return unit_path(service).is_file()


def uninstall_unit(service: str) -> str:
    path = unit_path(service)
    if not path.exists():
        return ""
    disable_unit(service)
    path.unlink()
    daemon_reload()
    return str(path)


def wait_status(service: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = is_active(service)
        if status not in {"activating", "", UNKNOWN}:
            return status
        time.sleep(0.5)
    return is_active(service)
