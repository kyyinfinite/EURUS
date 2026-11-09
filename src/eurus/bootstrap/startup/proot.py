from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

CRON_MARKER_BEGIN = "# eurus-bootstrap-begin"
CRON_MARKER_END = "# eurus-bootstrap-end"


def crontab_available() -> bool:
    return shutil.which("crontab") is not None


def read_crontab() -> str:
    executable = shutil.which("crontab")
    if not executable:
        return ""
    result = subprocess.run(
        [executable, "-l"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return (result.stdout or "") if result.returncode == 0 else ""


def write_crontab(content: str) -> bool:
    executable = shutil.which("crontab")
    if not executable:
        return False
    result = subprocess.run(
        [executable, "-"],
        input=content,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def cron_entry(launcher_path: str) -> str:
    return f"@reboot {shlex.quote(launcher_path)}"


def build_cron_block(entries: list[str]) -> str:
    lines = [CRON_MARKER_BEGIN, *entries, CRON_MARKER_END]
    return "\n".join(lines)


def upsert_cron_block(entries: list[str]) -> tuple[bool, str]:
    current = read_crontab()
    if not crontab_available():
        return False, "crontab is not available"
    cleaned = _strip_block(current)
    if not entries:
        if cleaned == current:
            return False, "no eurus cron entries found"
        if write_crontab(cleaned):
            return True, "removed eurus cron entries"
        return False, "cannot write crontab"
    block = build_cron_block(entries)
    updated = (cleaned.rstrip("\n") + "\n\n" + block + "\n") if cleaned.strip() else block + "\n"
    if write_crontab(updated):
        return True, "crontab updated"
    return False, "cannot write crontab"


def _strip_block(content: str) -> str:
    lines = content.splitlines()
    result: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == CRON_MARKER_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_MARKER_END:
            inside = False
            continue
        if not inside:
            result.append(line)
    return "\n".join(result).rstrip("\n")


def cron_block_present() -> bool:
    return CRON_MARKER_BEGIN in read_crontab()


def shell_profile_path(environ: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if environ is None else dict(environ)
    shell = env.get("SHELL", "")
    home = Path(env.get("HOME", str(Path.home())))
    if shell.endswith("zsh"):
        return home / ".zshrc"
    return home / ".profile"


def profile_hint(launcher_path: str) -> str:
    quoted = shlex.quote(launcher_path)
    return f"[ -x {quoted} ] && {quoted} &"
