from __future__ import annotations

import os
import shutil
from pathlib import Path

from eurus.bootstrap.models import PackageManagerInfo

MANAGERS: tuple[tuple[str, str], ...] = (
    ("pkg", "pkg"),
    ("apt", "apt"),
    ("dnf", "dnf"),
    ("yum", "yum"),
    ("pacman", "pacman"),
    ("apk", "apk"),
    ("zypper", "zypper"),
)

CHROMIUM_PACKAGE_HINTS = {
    "pkg": "pkg install x11-repo && pkg install chromium",
    "apt": "apt install chromium (Debian) or chromium-browser (Ubuntu)",
    "dnf": "dnf install chromium",
    "yum": "yum install chromium",
    "pacman": "pacman -S chromium",
    "apk": "apk add chromium",
    "zypper": "zypper install chromium",
    "none": "install Chromium with your system package manager",
}


def detect_package_manager(environ: dict[str, str] | None = None) -> PackageManagerInfo:
    env = dict(os.environ) if environ is None else dict(environ)
    for name, command in MANAGERS:
        found = shutil.which(command)
        if found:
            return PackageManagerInfo(name=name, path=found, is_root=_is_root(env))
    return PackageManagerInfo(name="none", path="", is_root=_is_root(env))


def _is_root(environ: dict[str, str]) -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return environ.get("USER", "") == "root"


def chromium_install_hint(manager: PackageManagerInfo) -> str:
    return CHROMIUM_PACKAGE_HINTS.get(manager.name, CHROMIUM_PACKAGE_HINTS["none"])


def needs_root(manager: PackageManagerInfo, environ: dict[str, str] | None = None) -> bool:
    if manager.name == "pkg":
        return False
    return not manager.is_root


def manager_prefix(manager: PackageManagerInfo, sudo: bool = False) -> list[str]:
    if sudo and manager.name != "pkg":
        sudo_bin = shutil.which("sudo")
        if sudo_bin:
            return [sudo_bin, manager.name]
        return [manager.name]
    return [manager.name]


def executable_exists(path: str) -> bool:
    return bool(path) and Path(path).exists()
