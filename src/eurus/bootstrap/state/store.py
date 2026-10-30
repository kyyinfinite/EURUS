from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from eurus.bootstrap.configuration.env_manager import (
    DIR_PERMISSION,
    atomic_write,
    ensure_private_dir,
)

SCHEMA_VERSION = 1

STATE_FILE = "state.json"
PLATFORM_FILE = "platform.json"
DEPENDENCIES_FILE = "dependencies.json"
HISTORY_FILE = "setup_history.json"
BACKUPS_DIR = "backups"

HISTORY_LIMIT = 200


class BootstrapStore:
    def __init__(self, home: Path) -> None:
        self.dir = home / "bootstrap"
        self.backups_dir = self.dir / BACKUPS_DIR

    def _ensure_dir(self) -> None:
        ensure_private_dir(self.dir)

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def platform_path(self) -> Path:
        return self.dir / PLATFORM_FILE

    @property
    def dependencies_path(self) -> Path:
        return self.dir / DEPENDENCIES_FILE

    @property
    def history_path(self) -> Path:
        return self.dir / HISTORY_FILE

    def load_state(self) -> dict[str, Any]:
        data = _load_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        state = migrate_state(state)
        self._ensure_dir()
        atomic_write(
            self.state_path,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            mode=DIR_PERMISSION,
        )

    def save_platform(self, payload: dict[str, Any]) -> None:
        self._ensure_dir()
        atomic_write(
            self.platform_path,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            mode=DIR_PERMISSION,
        )

    def save_dependencies(self, payload: dict[str, Any]) -> None:
        self._ensure_dir()
        atomic_write(
            self.dependencies_path,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            mode=DIR_PERMISSION,
        )

    def load_history(self) -> list[dict[str, Any]]:
        data = _load_json(self.history_path, [])
        return data if isinstance(data, list) else []

    def append_history(self, entry: dict[str, Any]) -> None:
        history = self.load_history()
        history.append({"ts": _utcnow(), **entry})
        if len(history) > HISTORY_LIMIT:
            history = history[-HISTORY_LIMIT:]
        self._ensure_dir()
        atomic_write(
            self.history_path,
            json.dumps(history, indent=2, ensure_ascii=False) + "\n",
            mode=DIR_PERMISSION,
        )

    def interrupted_setup(self) -> dict[str, Any] | None:
        state = self.load_state()
        last = state.get("last_quick_setup")
        if not isinstance(last, dict) or last.get("interrupted"):
            return None
        if last.get("started_at") and not last.get("finished_at"):
            return last
        return None

    def clear_interrupted(self) -> None:
        state = self.load_state()
        last = state.get("last_quick_setup")
        if isinstance(last, dict):
            state["last_quick_setup"] = {**last, "interrupted": True, "finished_at": None}
            self.save_state(state)

    def reset(self) -> list[str]:
        removed: list[str] = []
        for path in (
            self.state_path,
            self.platform_path,
            self.dependencies_path,
            self.history_path,
        ):
            if path.exists():
                path.unlink()
                removed.append(str(path))
        return removed


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    version = int(state.get("schema_version", 0) or 0)
    if version < 1:
        state.setdefault("schema_version", SCHEMA_VERSION)
        state.setdefault("created_at", _utcnow())
        state.setdefault("features", {})
    state["schema_version"] = SCHEMA_VERSION
    return state


def new_state(environ: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utcnow(),
        "last_checked": "",
        "platform": "",
        "architecture": "",
        "python": {},
        "features": {},
        "provider": {},
        "last_quick_setup": {},
        "doctor": {},
    }


def file_mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return 0


def is_world_readable(path: Path) -> bool:
    return bool(file_mode(path) & 0o077)


def mark_setup_started(state: dict[str, Any], steps: list[str]) -> dict[str, Any]:
    state["last_quick_setup"] = {
        "started_at": _utcnow(),
        "finished_at": "",
        "steps": {name: "pending" for name in steps},
    }
    return state


def mark_setup_step(state: dict[str, Any], step: str, status: str) -> dict[str, Any]:
    last = state.get("last_quick_setup")
    if isinstance(last, dict):
        steps = last.setdefault("steps", {})
        steps[step] = status
    return state


def mark_setup_finished(state: dict[str, Any], status: str = "done") -> dict[str, Any]:
    last = state.get("last_quick_setup")
    if isinstance(last, dict):
        last["finished_at"] = _utcnow()
        last["status"] = status
    return state


def prune_backups(backups_dir: Path, keep: int = 20) -> list[str]:
    if not backups_dir.is_dir():
        return []
    entries = sorted(
        (item for item in backups_dir.iterdir() if item.is_dir()), key=lambda item: item.name
    )
    removed = []
    for entry in entries[: max(len(entries) - keep, 0)]:
        try:
            for child in sorted(entry.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    os.rmdir(child)
            entry.rmdir()
            removed.append(entry.name)
        except OSError:
            continue
    return removed
