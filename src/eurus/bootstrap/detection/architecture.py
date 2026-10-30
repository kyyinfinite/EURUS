from __future__ import annotations

import os
import platform
from pathlib import Path

from eurus.bootstrap.models import ArchitectureInfo
from eurus.termux import is_termux

_ARCH_MAP = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "armv7l": "armv7l",
    "armv8l": "armv7l",
    "arm": "arm",
    "i686": "x86",
    "i386": "x86",
    "riscv64": "riscv64",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
}


def normalize_machine(machine: str) -> str:
    return _ARCH_MAP.get(machine.lower().strip(), machine.lower().strip() or "unknown")


def memory_total_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                fields = line.split()
                if len(fields) >= 2:
                    return int(int(fields[1]) / 1024)
    except (OSError, ValueError):
        pass
    return 0


def disk_free_gb(path: Path | None = None) -> float:
    target = path or Path.home()
    try:
        usage = os.statvfs(target)
    except OSError:
        return 0.0
    return round(usage.f_bavail * usage.f_frsize / (1024**3), 1)


def inspect_architecture(environ: dict[str, str] | None = None) -> ArchitectureInfo:
    machine = platform.machine() or "unknown"
    bits = 64 if platform.architecture()[0] == "64bit" else 32
    return ArchitectureInfo(
        machine=machine,
        normalized=normalize_machine(machine),
        bits=bits,
        ram_total_mb=memory_total_mb(),
        disk_free_gb=disk_free_gb(),
    )


def is_wheel_platform(arch: ArchitectureInfo) -> str:
    if arch.normalized in {"x86_64", "aarch64"}:
        return "yes"
    if arch.normalized in {"armv7l", "arm", "riscv64"}:
        return "limited"
    return "unknown"


def is_termux_env(environ: dict[str, str] | None = None) -> bool:
    return is_termux(environ or dict(os.environ))
