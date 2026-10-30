from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from eurus.bootstrap.dependencies.system_catalog import INSTALL_ARGUMENTS, PACKAGE_BY_KEY
from eurus.bootstrap.dependencies.system_inspector import inspect_system_packages
from eurus.bootstrap.detection.package_manager import (
    PackageManagerInfo,
    detect_package_manager,
    manager_prefix,
    needs_root,
)


@dataclass(slots=True)
class SystemInstallItem:
    key: str
    label: str
    package_name: str
    manager: str
    argv: list[str]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "package_name": self.package_name,
            "manager": self.manager,
            "command": shlex.join(self.argv),
            "note": self.note,
        }


@dataclass(slots=True)
class SystemPlan:
    manager: PackageManagerInfo
    items: list[SystemInstallItem] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    skipped_installed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items

    def to_dict(self) -> dict[str, Any]:
        return {
            "manager": self.manager.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "unavailable": list(self.unavailable),
            "skipped_installed": list(self.skipped_installed),
        }


def plan_system_install(
    keys: tuple[str, ...],
    *,
    environ: dict[str, str] | None = None,
    use_sudo: bool = False,
) -> SystemPlan:
    manager = detect_package_manager(environ)
    plan = SystemPlan(manager=manager)
    if manager.name == "none":
        plan.unavailable = [f"{key}: no supported system package manager found" for key in keys]
        return plan
    inspection = inspect_system_packages(environ, keys)
    states = {item.key: item for item in inspection.entries}
    for key in keys:
        spec = PACKAGE_BY_KEY.get(key)
        if spec is None:
            plan.unavailable.append(f"{key}: unknown system package key")
            continue
        state = states.get(key)
        if state is not None and (state.executable_found or state.installed):
            plan.skipped_installed.append(key)
            continue
        package_name = spec.package_for(manager.name)
        if package_name is None:
            plan.unavailable.append(f"{key}: {spec.manual_hint}")
            continue
        install_arguments = INSTALL_ARGUMENTS.get(manager.name, ("install",))
        argv = [
            *manager_prefix(manager, sudo=use_sudo and needs_root(manager, environ)),
            *install_arguments,
            package_name,
        ]
        plan.items.append(
            SystemInstallItem(
                key=key,
                label=spec.label,
                package_name=package_name,
                manager=manager.name,
                argv=argv,
                note=spec.note_for(manager.name),
            )
        )
    return plan


def preview_system_plan(plan: SystemPlan) -> list[str]:
    lines = []
    for item in plan.items:
        line = shlex.join(item.argv)
        if item.note:
            line = f"{line}  # {item.note}"
        lines.append(line)
    for entry in plan.unavailable:
        lines.append(f"unavailable: {entry}")
    for key in plan.skipped_installed:
        lines.append(f"already satisfied: {key}")
    return lines
