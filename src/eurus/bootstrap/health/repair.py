from __future__ import annotations

from pathlib import Path

from eurus.bootstrap.configuration.env_manager import (
    ensure_global_env,
    ensure_private_dir,
    global_env_path,
    tighten_permissions,
)
from eurus.bootstrap.dependencies.installer import install_targets
from eurus.bootstrap.models import CheckResult, RepairAction
from eurus.bootstrap.state.backup import backup_file, create_backup_dir

REPAIR_ACTIONS: dict[str, str] = {
    "pip_install": "Install or fix missing Python packages",
    "config_init": "Create a default config.toml",
    "config_repair": "Back up and reset the invalid config.toml",
    "env_perms": "Tighten .env permissions to 0600",
    "home_dirs": "Create ~/.eurus directory layout",
    "env_template": "Create the global .env template",
    "mcp_reset": "Back up and reset the invalid mcp.json",
    "env_perms_home": "Tighten ~/.eurus permissions to 0700",
}

REPAIR_ORDER = (
    "pip_install",
    "home_dirs",
    "env_template",
    "env_perms",
    "env_perms_home",
    "config_init",
    "config_repair",
    "mcp_reset",
)


def actions_for(results: list[CheckResult]) -> list[RepairAction]:
    actions: list[RepairAction] = []
    for result in results:
        if not result.repair_key or result.status in {"READY", "OPTIONAL"}:
            continue
        if result.repair_key == "pip_install":
            targets = [target for target in result.repair_payload.get("targets", []) if target]
            if not targets:
                continue
            actions.append(
                RepairAction(
                    key="pip_install",
                    title=REPAIR_ACTIONS["pip_install"],
                    description="pip install " + " ".join(targets),
                    kind="pip_install",
                    payload={"targets": targets},
                )
            )
        elif result.repair_key == "home_dirs":
            actions.append(
                RepairAction(
                    key="home_dirs",
                    title=REPAIR_ACTIONS["home_dirs"],
                    description="create the ~/.eurus directory layout with 0700 permissions",
                    kind="home_dirs",
                )
            )
        elif result.repair_key == "env_perms_home":
            actions.append(
                RepairAction(
                    key="env_perms_home",
                    title=REPAIR_ACTIONS["env_perms_home"],
                    description="chmod 0700 on the ~/.eurus directory",
                    kind="env_perms_home",
                )
            )
        elif result.repair_key == "config_init":
            actions.append(
                RepairAction(
                    key="config_init",
                    title=REPAIR_ACTIONS["config_init"],
                    description="write the default configuration to the user config path",
                    kind="config_init",
                )
            )
        elif result.repair_key == "config_repair":
            actions.append(
                RepairAction(
                    key="config_repair",
                    title=REPAIR_ACTIONS["config_repair"],
                    description="back up the invalid config.toml and write the default sample",
                    kind="config_repair",
                    backup_paths=(_config_path_str(),),
                )
            )
        elif result.repair_key == "env_perms":
            actions.append(
                RepairAction(
                    key="env_perms",
                    title=REPAIR_ACTIONS["env_perms"],
                    description="chmod 0600 on the global .env",
                    kind="env_perms",
                )
            )
        elif result.repair_key == "mcp_reset":
            actions.append(
                RepairAction(
                    key="mcp_reset",
                    title=REPAIR_ACTIONS["mcp_reset"],
                    description=(
                        "back up the invalid mcp.json and recreate it as an empty server list"
                    ),
                    kind="mcp_reset",
                    backup_paths=(_mcp_path_str(),),
                )
            )
    ordered: list[RepairAction] = []
    for key in REPAIR_ORDER:
        ordered.extend(action for action in actions if action.key == key)
    return ordered


def _config_path_str() -> str:
    from eurus import paths

    return str(paths.user_config_path())


def _mcp_path_str() -> str:
    from eurus import paths

    return str(paths.home_dir() / "mcp.json")


def execute_repair(
    actions: list[RepairAction],
    environ: dict[str, str],
    backup_root: Path | None = None,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for action in actions:
        try:
            outcome = apply_repair_action(action, environ, backup_root)
            results.append((action.key, outcome[0], outcome[1]))
        except Exception as exc:
            results.append((action.key, False, f"{type(exc).__name__}: {exc}"))
    return results


def apply_repair_action(
    action: RepairAction,
    environ: dict[str, str],
    backup_root: Path | None = None,
) -> tuple[bool, str]:
    from eurus import paths
    from eurus.config import sample_config

    if action.kind == "pip_install":
        targets = list(action.payload.get("targets", []))
        outcome = install_targets(targets)
        if outcome.success:
            return True, f"installed: {', '.join(targets)}"
        return False, outcome.error or f"pip exited with code {outcome.returncode}"
    if action.kind == "home_dirs":
        home = paths.home_dir(environ)
        ensure_private_dir(home)
        ensure_private_dir(home / "bootstrap" / "backups")
        return True, f"ensured {home}"
    if action.kind == "env_template":
        path, created = ensure_global_env(environ)
        return True, f"{'created' if created else 'verified'} {path}"
    if action.kind == "env_perms":
        path = global_env_path(environ)
        if tighten_permissions(path):
            return True, f"tightened permissions on {path}"
        return True, f"{path} permissions already safe"
    if action.kind == "env_perms_home":
        from eurus.bootstrap.state.store import is_world_readable

        home = paths.home_dir(environ)
        if is_world_readable(home):
            tighten_permissions(home)
            return True, f"tightened permissions on {home}"
        return True, f"{home} permissions already safe"
    if action.kind == "config_init":
        target = paths.user_config_path(environ)
        if target.exists():
            return True, f"{target} already exists; left untouched"
        ensure_private_dir(target.parent)
        target.write_text(sample_config(environ), encoding="utf-8")
        return True, f"created {target}"
    if action.kind == "config_repair":
        target = paths.user_config_path(environ)
        backup_dir = backup_root or create_backup_dir(
            paths.home_dir(environ) / "bootstrap" / "backups", "config-repair"
        )
        if target.exists():
            backup_file(target, backup_dir)
        ensure_private_dir(target.parent)
        target.write_text(sample_config(environ), encoding="utf-8")
        return True, f"reset {target} (backup in {backup_dir})"
    if action.kind == "mcp_reset":
        target = paths.home_dir(environ) / "mcp.json"
        backup_dir = backup_root or create_backup_dir(
            paths.home_dir(environ) / "bootstrap" / "backups", "mcp-repair"
        )
        if target.exists():
            backup_file(target, backup_dir)
        target.write_text('{"servers": []}\n', encoding="utf-8")
        return True, f"reset {target} (backup in {backup_dir})"
    return False, f"unknown repair kind: {action.kind}"
