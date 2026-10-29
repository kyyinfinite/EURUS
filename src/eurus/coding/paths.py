from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from eurus.errors import ToolError

MAX_FILE_BYTES = 1_000_000


def resolve_workspace_path(root: Path, relative: str, *, label: str = "path") -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute():
        raise ToolError(f"{label} must be a non-empty relative path: {relative!r}")
    if ".." in candidate.parts:
        raise ToolError(f"{label} cannot traverse parent directories: {relative}")
    resolved_root = root.resolve()
    cursor = resolved_root
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise ToolError(f"symbolic links are not allowed in {label}s: {relative}")
    target = cursor.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ToolError(f"{label} is outside the configured workspace: {relative}")
    return target


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".eurus-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
