from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from eurus.bootstrap.configuration.env_manager import ensure_private_dir


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def backup_file(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    ensure_private_dir(backup_dir)
    target = backup_dir / path.name
    counter = 1
    while target.exists():
        target = backup_dir / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    shutil.copy2(path, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target


def backup_files(paths: list[Path], backup_dir: Path) -> list[Path]:
    saved: list[Path] = []
    for path in paths:
        result = backup_file(path, backup_dir)
        if result is not None:
            saved.append(result)
    return saved


def create_backup_dir(root: Path, label: str) -> Path:
    directory = root / f"{_stamp()}-{label}"
    ensure_private_dir(directory)
    return directory


def list_backups(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)


def restore_file(backup_path: Path, target: Path) -> bool:
    if not backup_path.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target)
    return True
