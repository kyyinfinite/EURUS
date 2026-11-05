import shlex
import subprocess
from pathlib import Path

import pytest

from eurus.bootstrap.dependencies.system_installer import (
    SystemInstallResult,
    execute_system_install,
)
from eurus.bootstrap.dependencies.system_resolver import (
    plan_system_install,
    preview_system_plan,
)
from eurus.bootstrap.detection.package_manager import PackageManagerInfo


def _manager(name: str = "apt") -> PackageManagerInfo:
    return PackageManagerInfo(name=name, path=f"/usr/bin/{name}", is_root=False)


def test_plan_chromium_for_apt(monkeypatch):
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: _manager("apt"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("apt"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found", lambda item: False
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._package_installed", lambda m, p, e: False
    )
    plan = plan_system_install(("chromium",), environ={})
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.package_name == "chromium"
    assert item.argv == ["apt", "install", "chromium"]
    assert plan.empty is False


def test_plan_skips_already_satisfied(monkeypatch):
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found", lambda item: True
    )
    plan = plan_system_install(("chromium",), environ={})
    assert plan.items == []
    assert plan.skipped_installed == ["chromium"]


def test_plan_reports_unavailable_manager(monkeypatch):
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: PackageManagerInfo(name="none", path="", is_root=False),
    )
    plan = plan_system_install(("chromium",), environ={})
    assert plan.empty is True
    assert plan.unavailable and "no supported" in plan.unavailable[0]


def test_plan_sudo_requires_authorization(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: _manager("apt"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("apt"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found", lambda item: False
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._package_installed", lambda m, p, e: False
    )
    without = plan_system_install(("git",), environ={})
    assert without.items[0].argv[:2] == ["apt", "install"]
    with_sudo = plan_system_install(("git",), environ={}, use_sudo=True)
    assert with_sudo.items[0].argv[0].endswith("sudo")
    assert with_sudo.items[0].argv[1:3] == ["apt", "install"]


def test_plan_pkg_never_uses_sudo(monkeypatch):
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found", lambda item: False
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._package_installed", lambda m, p, e: False
    )
    plan = plan_system_install(("chromium",), environ={}, use_sudo=True)
    assert plan.items[0].argv[0] == "pkg"


def test_preview_lists_commands_and_notes(monkeypatch):
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_resolver.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("pkg"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found", lambda item: False
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._package_installed", lambda m, p, e: False
    )
    plan = plan_system_install(("chromium",), environ={})
    preview = preview_system_plan(plan)
    assert preview
    assert "pkg install chromium" in preview[0]
    assert "x11-repo" in preview[0]


def test_execute_without_confirm_raises(monkeypatch, tmp_path):
    from eurus.bootstrap.dependencies.system_resolver import SystemInstallItem, SystemPlan
    from eurus.bootstrap.models import BootstrapError

    plan = SystemPlan(
        manager=_manager(),
        items=[
            SystemInstallItem(
                key="chromium", label="Chromium", package_name="chromium", manager="apt", argv=[]
            )
        ],
    )
    with pytest.raises(BootstrapError):
        execute_system_install(plan, confirm=None)


def test_execute_requires_confirmation_and_cancels(monkeypatch):
    from eurus.bootstrap.dependencies.system_resolver import SystemInstallItem, SystemPlan

    plan = SystemPlan(
        manager=_manager(),
        items=[
            SystemInstallItem(
                key="git",
                label="Git",
                package_name="git",
                manager="apt",
                argv=["apt", "install", "git"],
            )
        ],
    )
    results = execute_system_install(plan, confirm=lambda _preview: False)
    assert results == []


def test_execute_runs_confirmed_commands(monkeypatch):
    from eurus.bootstrap.dependencies.system_resolver import SystemInstallItem, SystemPlan

    executed = []

    def fake_run(argv, capture_output, text, timeout, check):
        executed.append(argv)

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    plan = SystemPlan(
        manager=_manager(),
        items=[
            SystemInstallItem(
                key="git",
                label="Git",
                package_name="git",
                manager="apt",
                argv=["apt", "install", "git"],
            )
        ],
    )
    results = execute_system_install(plan, confirm=lambda _preview: True)
    assert executed == [["apt", "install", "git"]]
    assert len(results) == 1
    assert results[0].success is True


def test_execute_survives_command_errors(monkeypatch):
    from eurus.bootstrap.dependencies.system_resolver import SystemInstallItem, SystemPlan

    def fake_run(argv, capture_output, text, timeout, check):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "unable to locate package"

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    plan = SystemPlan(
        manager=_manager(),
        items=[
            SystemInstallItem(
                key="chromium",
                label="Chromium",
                package_name="chromium",
                manager="apt",
                argv=["apt", "install", "chromium"],
            )
        ],
    )
    results = execute_system_install(plan, confirm=lambda _preview: True)
    assert results[0].success is False
    assert results[0].returncode == 1
    assert "unable to locate" in results[0].output_tail


def test_preview_commands_are_shell_safe():
    from eurus.bootstrap.dependencies.system_resolver import SystemInstallItem, SystemPlan

    plan = SystemPlan(
        manager=_manager(),
        items=[
            SystemInstallItem(
                key="chromium",
                label="Chromium",
                package_name="chromium",
                manager="apt",
                argv=["apt", "install", "chromium"],
                note="note with spaces; and semicolon",
            )
        ],
    )
    for line in preview_system_plan(plan):
        if "# " in line:
            command, _, _ = line.partition("  # ")
            assert shlex.split(command) == ["apt", "install", "chromium"]


def test_system_install_result_shape():
    result = SystemInstallResult(key="git", command="apt install git", success=True, returncode=0)
    assert result.to_dict() == {
        "key": "git",
        "command": "apt install git",
        "success": True,
        "returncode": 0,
    }


def test_inspector_reports_state_per_package(monkeypatch):
    from eurus.bootstrap.dependencies.system_inspector import inspect_system_packages

    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: _manager("pacman"),
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._provides_found",
        lambda item: item.key == "git",
    )
    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector._package_installed", lambda m, p, e: False
    )
    inspection = inspect_system_packages({}, ("chromium", "git"))
    states = {item.key: item for item in inspection.entries}
    assert states["chromium"].status == "MISSING"
    assert states["chromium"].package_name == "chromium"
    assert states["git"].status == "READY"
    assert states["git"].executable_found is True


def test_inspector_without_package_manager(monkeypatch):
    from eurus.bootstrap.dependencies.system_inspector import inspect_system_packages

    monkeypatch.setattr(
        "eurus.bootstrap.dependencies.system_inspector.detect_package_manager",
        lambda env: PackageManagerInfo(name="none", path="", is_root=False),
    )
    inspection = inspect_system_packages({}, ("chromium",))
    entry = inspection.entries[0]
    assert entry.available is False
    assert "no supported" in entry.note


def test_catalog_covers_supported_managers():
    from eurus.bootstrap.dependencies.system_catalog import SYSTEM_PACKAGES

    for item in SYSTEM_PACKAGES:
        for manager in ("pkg", "apt", "dnf", "pacman", "apk", "zypper"):
            assert item.package_for(manager), f"{item.key} missing for {manager}"
        assert item.package_for("yum")
        assert Path(item.key).name == item.key
