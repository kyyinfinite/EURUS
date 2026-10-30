from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from eurus.bootstrap.dependencies.system_catalog import SYSTEM_PACKAGES, SystemPackage
from eurus.bootstrap.detection.package_manager import PackageManagerInfo, detect_package_manager
from eurus.bootstrap.models import UNKNOWN

QUERY_TIMEOUT = 30.0

MISSING = "MISSING"
READY = "READY"


@dataclass(slots=True)
class SystemPackageState:
    key: str
    label: str
    description: str
    manager: str
    package_name: str | None
    available: bool
    installed: bool
    executable_found: bool
    note: str = ""
    status: str = UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "manager": self.manager,
            "package_name": self.package_name,
            "available": self.available,
            "installed": self.installed,
            "executable_found": self.executable_found,
            "note": self.note,
            "status": self.status,
        }


@dataclass(slots=True)
class SystemInspection:
    manager: PackageManagerInfo
    entries: list[SystemPackageState] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager": self.manager.to_dict(),
            "entries": [item.to_dict() for item in self.entries],
        }


def inspect_system_packages(
    environ: dict[str, str] | None = None,
    keys: tuple[str, ...] | None = None,
) -> SystemInspection:
    manager = detect_package_manager(environ)
    wanted = _select_packages(keys)
    entries = [
        _inspect_package(item, manager, environ) for item in wanted if manager.name != "none"
    ] or [
        SystemPackageState(
            key=item.key,
            label=item.label,
            description=item.description,
            manager="none",
            package_name=None,
            available=False,
            installed=False,
            executable_found=_provides_found(item),
            note="no supported system package manager found",
            status=UNKNOWN,
        )
        for item in wanted
    ]
    return SystemInspection(manager=manager, entries=entries)


def _select_packages(keys: tuple[str, ...] | None) -> list[SystemPackage]:
    if not keys:
        return list(SYSTEM_PACKAGES)
    return [item for item in SYSTEM_PACKAGES if item.key in keys]


def _inspect_package(
    item: SystemPackage, manager: PackageManagerInfo, environ: dict[str, str] | None
) -> SystemPackageState:
    package_name = item.package_for(manager.name)
    executable_found = _provides_found(item)
    installed = bool(package_name) and _package_installed(manager, package_name, environ)
    available = package_name is not None
    if executable_found or installed:
        status = READY
    elif available:
        status = MISSING
    else:
        status = UNKNOWN
    return SystemPackageState(
        key=item.key,
        label=item.label,
        description=item.description,
        manager=manager.name,
        package_name=package_name,
        available=available,
        installed=installed,
        executable_found=executable_found,
        note=item.note_for(manager.name) if available else item.manual_hint,
        status=status,
    )


def _provides_found(item: SystemPackage) -> bool:
    return any(shutil.which(name) for name in item.provides)


def _package_installed(
    manager: PackageManagerInfo, package_name: str, environ: dict[str, str] | None
) -> bool:
    try:
        if manager.name in {"pkg", "apt"}:
            result = _run(["dpkg-query", "-W", "-f=${Status}", package_name], environ)
            return result is not None and result.returncode == 0 and "installed" in result.stdout
        if manager.name in {"dnf", "yum", "zypper"}:
            result = _run(["rpm", "-q", package_name], environ)
            return result is not None and result.returncode == 0
        if manager.name == "pacman":
            result = _run(["pacman", "-Qi", package_name], environ)
            return result is not None and result.returncode == 0
        if manager.name == "apk":
            result = _run(["apk", "info", "-e", package_name], environ)
            return result is not None and result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def _run(
    arguments: list[str], environ: dict[str, str] | None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
            check=False,
            env=dict(os.environ) if environ is None else dict(environ),
        )
    except OSError:
        return None
