from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from pathlib import Path

from eurus.home import (
    CONFIG_FILE_NAME,
    HOME_DIR_NAME,
    HomeManager,
    resolve_home,
)

PROJECT_FILE_NAMES: tuple[str, ...] = ("eurus.toml", ".eurus/config.toml")

__all__ = [
    "CONFIG_FILE_NAME",
    "HOME_DIR_NAME",
    "PROJECT_FILE_NAMES",
    "HomeManager",
    "database_path",
    "ensure_private_dir",
    "home_dir",
    "home_manager",
    "project_config_path",
    "resolve_home",
    "user_config_path",
]


def home_dir(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_home(environ)


def home_manager(environ: Mapping[str, str] | None = None) -> HomeManager:
    return HomeManager(environ)


def database_path(environ: Mapping[str, str] | None = None) -> Path:
    return HomeManager(environ).database_path()


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return home_dir(environ) / CONFIG_FILE_NAME


def project_config_path(cwd: Path) -> Path | None:
    for name in PROJECT_FILE_NAMES:
        candidate = cwd / name
        if candidate.is_file():
            return candidate
    return None


def ensure_private_dir(path: Path) -> Path:
    if path.is_dir():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        return path
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path
