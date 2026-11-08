from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import tempfile
from pathlib import Path

from eurus.errors import ToolError
from eurus.execution.host import scrubbed_env

MAX_FILE_BYTES = 1_000_000

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

GIT_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
)


def git_child_env() -> dict[str, str]:
    env = scrubbed_env()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    for name in GIT_OVERRIDE_VARS:
        env.pop(name, None)
    return env


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


def _relative_parts(relative: str, *, label: str) -> list[str]:
    candidate = Path(relative)
    if not relative or candidate.is_absolute():
        raise ToolError(f"{label} must be a non-empty relative path: {relative!r}")
    if ".." in candidate.parts:
        raise ToolError(f"{label} cannot traverse parent directories: {relative}")
    return [part for part in candidate.parts if part not in {"", "."}]


def _reject_race(exc: OSError, relative: str) -> ToolError:
    if exc.errno == errno.ELOOP:
        return ToolError(f"symbolic links are not allowed in paths: {relative}")
    if exc.errno == errno.ENOTDIR:
        return ToolError(f"file does not exist or is not a regular file: {relative}")
    return ToolError(f"cannot access {relative!r} inside the workspace: {exc}")


def _descend_dir(current: int, component: str, relative: str, *, create: bool) -> int:
    try:
        nxt = os.open(component, _DIR_FLAGS, dir_fd=current)
    except FileNotFoundError:
        if not create:
            raise ToolError(
                f"directory {component!r} does not exist inside the workspace"
            ) from None
        try:
            os.mkdir(component, 0o700, dir_fd=current)
        except OSError as exc:
            raise ToolError(
                f"cannot create directory {component!r} inside the workspace: {exc}"
            ) from exc
        try:
            nxt = os.open(component, _DIR_FLAGS, dir_fd=current)
        except OSError as exc:
            raise _reject_race(exc, relative) from exc
    except OSError as exc:
        swapped_symlink = False
        with contextlib.suppress(OSError):
            swapped_symlink = stat.S_ISLNK(
                os.stat(component, dir_fd=current, follow_symlinks=False).st_mode
            )
        if swapped_symlink:
            raise ToolError(f"symbolic links are not allowed in paths: {relative}") from exc
        raise _reject_race(exc, relative) from exc
    os.close(current)
    return nxt


def _open_dir_fd(root: Path, parts: list[str], relative: str, *, create: bool) -> int:
    try:
        current = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise ToolError(f"cannot open workspace root {root}: {exc}") from exc
    try:
        for component in parts:
            current = _descend_dir(current, component, relative, create=create)
    except BaseException:
        os.close(current)
        raise
    return current


def read_text_at(root: Path, relative: str, *, label: str = "path") -> str:
    parts = _relative_parts(relative, label=label)
    if not parts:
        raise ToolError(f"{label} must name a file: {relative!r}")
    parent_fd = _open_dir_fd(root, parts[:-1], relative, create=False)
    try:
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise ToolError("file does not exist or is not a regular file") from exc
        except OSError as exc:
            raise _reject_race(exc, relative) from exc
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ToolError("file does not exist or is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise ToolError("file exceeds 1000000 bytes")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("file is not valid UTF-8 text") from exc


def atomic_write_at(
    root: Path,
    relative: str,
    content: str,
    *,
    label: str = "path",
    require_absent: bool = False,
) -> None:
    parts = _relative_parts(relative, label=label)
    if not parts:
        raise ToolError(f"{label} must name a file: {relative!r}")
    parent_fd = _open_dir_fd(root, parts[:-1], relative, create=True)
    temporary = f".eurus-write-{secrets.token_hex(8)}"
    descriptor: int | None = None
    owned_by_handle = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        mode: int | None = None
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode):
                raise ToolError(f"symbolic links are not allowed in {label}s: {relative}")
            if require_absent:
                raise ToolError(f"{label} already exists: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise ToolError(f"{label} exists and is not a regular file: {relative}")
            mode = stat.S_IMODE(info.st_mode)
        if mode is not None:
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            owned_by_handle = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        if descriptor is not None and not owned_by_handle:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent_fd)
        raise
    finally:
        os.close(parent_fd)


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
