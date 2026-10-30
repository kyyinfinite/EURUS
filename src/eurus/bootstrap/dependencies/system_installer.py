from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eurus.bootstrap.dependencies.system_resolver import SystemPlan, preview_system_plan
from eurus.bootstrap.models import BootstrapError
from eurus.logs import redact

INSTALL_TIMEOUT = 1800.0
OUTPUT_TAIL_LINES = 12


@dataclass(slots=True)
class SystemInstallResult:
    key: str
    command: str
    success: bool
    returncode: int
    output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "command": self.command,
            "success": self.success,
            "returncode": self.returncode,
        }


def execute_system_install(
    plan: SystemPlan,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> list[SystemInstallResult]:
    if plan.empty:
        return []
    if confirm is None:
        raise BootstrapError(
            "system package installation requires an explicit confirmation callback"
        )
    preview = preview_system_plan(plan)
    sudo_commands = [item for item in plan.items if _uses_sudo(item.argv, plan)]
    if sudo_commands:
        preview.append(
            "warning: the commands above use sudo; cancel if you do not authorize root escalation"
        )
    if not confirm("\n".join(preview)):
        return []
    results: list[SystemInstallResult] = []
    for item in plan.items:
        results.append(_install_one(item.argv, item.key))
    return results


def _uses_sudo(argv: list[str], plan: SystemPlan) -> bool:
    return bool(plan.manager.name != "pkg" and argv and argv[0].endswith("sudo"))


def _install_one(argv: list[str], key: str) -> SystemInstallResult:
    command = " ".join(argv)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        tail = f"{type(exc).__name__}: {exc}"
        return SystemInstallResult(
            key=key, command=command, success=False, returncode=124, output_tail=tail
        )
    output = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(redact(output).splitlines()[-OUTPUT_TAIL_LINES:])
    return SystemInstallResult(
        key=key,
        command=command,
        success=result.returncode == 0,
        returncode=result.returncode,
        output_tail=tail,
    )
