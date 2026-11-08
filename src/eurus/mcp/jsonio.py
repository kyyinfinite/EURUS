from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any


def read_json_dict(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path.name}: {exc}"
    except json.JSONDecodeError as exc:
        backup = _backup_corrupt(path)
        warning = f"{path.name} is corrupt ({exc}); backed up to {backup.name}, using defaults"
        return None, warning
    if not isinstance(raw, dict):
        backup = _backup_corrupt(path)
        warning = f"{path.name} does not contain a JSON object; backed up to {backup.name}"
        return None, warning
    return raw, ""


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    with suppress(OSError):
        os.chmod(temporary, 0o600)
    temporary.replace(path)


def _backup_corrupt(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(backup)
    except OSError:
        backup = path
    return backup


__all__ = ["atomic_write_text", "read_json_dict"]
