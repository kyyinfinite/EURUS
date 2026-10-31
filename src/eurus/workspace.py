from __future__ import annotations

import os
import re
import shutil
import stat
import time
from pathlib import Path

from eurus.errors import ToolError
from eurus.home import HomeManager

SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUNS_SCOPE = ("temp", "runs")
AUTOMATION_SCOPE = ("automation",)


class WorkspaceError(ToolError):
    def __init__(self, message: str, *, code: str = "workspace") -> None:
        super().__init__(message, code=code)


def validated_scope_id(value: str, label: str) -> str:
    if not SCOPE_ID_PATTERN.match(value):
        raise WorkspaceError(f"invalid {label}: {value!r}")
    return value


class WorkspaceManager:
    def __init__(self, root: Path, *, home: HomeManager | None = None) -> None:
        self._root = Path(root).expanduser().resolve()
        self._home = home

    @property
    def root(self) -> Path:
        return self._root

    @property
    def home(self) -> HomeManager:
        if self._home is None:
            self._home = HomeManager()
        return self._home

    def default_workspace(self) -> Path:
        return self._root

    def project_workspace(self, project_id: str) -> Path:
        return self.home.workspace_dir / "projects" / validated_scope_id(project_id, "project_id")

    def run_workspace(self, run_id: str) -> Path:
        return self.home.workspace_dir.joinpath(*RUNS_SCOPE) / validated_scope_id(run_id, "run_id")

    def automation_workspace(self, execution_id: str) -> Path:
        return self.home.workspace_dir.joinpath(*AUTOMATION_SCOPE) / validated_scope_id(
            execution_id, "execution_id"
        )

    def shared_workspace(self) -> Path:
        return self.home.workspace_dir / "shared"

    def artifacts_dir(self) -> Path:
        return self.home.workspace_dir / "artifacts"

    def create(self, workspace: Path) -> Path:
        candidate = Path(workspace)
        if candidate != self._root:
            self._require_under_workspace_dir(candidate)
        return self.home.ensure_dir(candidate)

    def cleanup(self, workspace: Path) -> None:
        expected = Path(workspace)
        base = self.home.workspace_dir
        self._require_under_workspace_dir(expected)
        try:
            resolved = expected.resolve(strict=True)
            parent = expected.parent.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError(f"workspace is not accessible: {expected}") from exc
        if resolved.parent != parent:
            raise WorkspaceError(f"workspace moved or is a symlink; refusing cleanup: {expected}")
        if resolved == self._root or resolved == base.resolve():
            raise WorkspaceError(f"refusing to remove protected workspace: {resolved}")
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise WorkspaceError(f"cannot remove workspace {resolved}: {exc}") from exc

    def sweep(self, max_age_seconds: float, *, now: float | None = None) -> int:
        if max_age_seconds <= 0:
            raise WorkspaceError("max_age_seconds must be positive")
        current = time.time() if now is None else now
        cutoff = current - max_age_seconds
        base = self.home.workspace_dir
        removed = 0
        for scope in (base.joinpath(*RUNS_SCOPE), base.joinpath(*AUTOMATION_SCOPE)):
            if not scope.is_dir():
                continue
            for child in scope.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                try:
                    info = child.stat()
                except OSError as exc:
                    raise WorkspaceError(f"cannot inspect workspace {child}: {exc}") from exc
                if info.st_mtime >= cutoff:
                    continue
                try:
                    shutil.rmtree(child)
                except OSError as exc:
                    raise WorkspaceError(f"cannot sweep expired workspace {child}: {exc}") from exc
                removed += 1
        return removed

    def resolve(self, relative: str) -> Path:
        candidate = self._check_relative(relative)
        if not candidate.parts:
            return self._root
        target = (self._root / candidate).resolve()
        if target != self._root and self._root not in target.parents:
            raise WorkspaceError(f"path escapes the workspace: {relative}")
        return target

    def resolve_strict(self, relative: str) -> Path:
        candidate = self._check_relative(relative)
        cursor = self._root
        for part in candidate.parts:
            if part in {"", "."}:
                continue
            cursor = cursor / part
            self._reject_symlink(cursor, relative)
        return cursor

    def validate(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        target = candidate.resolve()
        if target != self._root and self._root not in target.parents:
            raise WorkspaceError(f"path is outside the workspace: {path}")
        return target

    def validate_strict(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved != self._root and self._root not in resolved.parents:
                raise WorkspaceError(f"path is outside the workspace: {path}")
            try:
                tail = resolved.relative_to(self._root)
            except ValueError as exc:
                raise WorkspaceError(f"path is outside the workspace: {path}") from exc
        else:
            tail = self._check_relative(str(candidate))
        cursor = self._root
        for part in tail.parts:
            if part in {"", "."}:
                continue
            cursor = cursor / part
            self._reject_symlink(cursor, str(path))
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            return cursor
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect path {cursor}: {exc}") from exc
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise WorkspaceError(f"hardlinked file is not permitted: {cursor}")
        return cursor

    def open_read(self, relative: str) -> int:
        return self._open_fd(relative, os.O_RDONLY)

    def open_write(self, relative: str, *, exclusive: bool = True, mode: int = 0o600) -> int:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if exclusive else (os.O_WRONLY | os.O_CREAT)
        descriptor = self._open_fd(relative, flags, creation_mode=mode)
        try:
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise WorkspaceError(f"hardlinked file is not permitted: {relative}")
            if not exclusive:
                os.ftruncate(descriptor, 0)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_fd(self, relative: str, flags: int, creation_mode: int = 0o600) -> int:
        candidate = self._check_relative(relative)
        parts = [part for part in candidate.parts if part not in {"", "."}]
        if not parts:
            raise WorkspaceError(f"path must name a file: {relative!r}")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            current = os.open(self._root, directory_flags)
        except OSError as exc:
            raise WorkspaceError(f"cannot open workspace root {self._root}: {exc}") from exc
        try:
            create_directories = bool(flags & os.O_CREAT)
            for component in parts[:-1]:
                current = self._descend(current, component, directory_flags, create_directories)
            final_flags = flags | getattr(os, "O_NOFOLLOW", 0)
            try:
                return os.open(parts[-1], final_flags, creation_mode, dir_fd=current)
            except OSError as exc:
                raise WorkspaceError(
                    f"cannot open {relative!r} inside the workspace: {exc}"
                ) from exc
        finally:
            os.close(current)

    def _descend(
        self, current: int, component: str, directory_flags: int, create_directories: bool
    ) -> int:
        try:
            nxt = os.open(component, directory_flags, dir_fd=current)
        except FileNotFoundError:
            if not create_directories:
                raise WorkspaceError(
                    f"directory {component!r} does not exist inside the workspace"
                ) from None
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except OSError as exc:
                raise WorkspaceError(
                    f"cannot create directory {component!r} inside the workspace: {exc}"
                ) from exc
            try:
                nxt = os.open(component, directory_flags, dir_fd=current)
            except OSError as exc:
                raise WorkspaceError(
                    f"cannot descend into {component!r} inside the workspace: {exc}"
                ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"cannot descend into {component!r} inside the workspace: {exc}"
            ) from exc
        os.close(current)
        return nxt

    def _check_relative(self, relative: str) -> Path:
        if not relative or "\x00" in relative:
            raise WorkspaceError(f"path must be a non-empty string without NUL: {relative!r}")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise WorkspaceError(f"path must be relative: {relative}")
        if ".." in candidate.parts:
            raise WorkspaceError(f"path cannot traverse parent directories: {relative}")
        return candidate

    def _reject_symlink(self, path: Path, original: str) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect path {path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkspaceError(f"symbolic links are not permitted in workspace paths: {original}")

    def _require_under_workspace_dir(self, candidate: Path) -> None:
        base = self.home.workspace_dir
        resolved = candidate.expanduser().resolve(strict=False)
        base_resolved = base.resolve(strict=False)
        if resolved == base_resolved or base_resolved not in resolved.parents:
            raise WorkspaceError(
                f"workspace is not a managed scope under {base_resolved}: {candidate}"
            )


__all__ = [
    "SCOPE_ID_PATTERN",
    "WorkspaceError",
    "WorkspaceManager",
    "validated_scope_id",
]
