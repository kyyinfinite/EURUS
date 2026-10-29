from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from eurus.coding.paths import atomic_write_text, resolve_workspace_path
from eurus.errors import ToolError

MAX_PATCH_FILE_BYTES = 1_000_000


class PatchError(ValueError):
    """Raised when a multi-file patch cannot be safely applied."""


@dataclass(frozen=True, slots=True)
class FilePatch:
    path: str
    expected_content: str | None
    new_content: str

    @property
    def expected_sha256(self) -> str | None:
        if self.expected_content is None:
            return None
        return hashlib.sha256(self.expected_content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AppliedChange:
    path: str
    before_content: str | None
    after_sha256: str


@dataclass(frozen=True, slots=True)
class PatchResult:
    changes: tuple[AppliedChange, ...]


def _resolve(root: Path, relative: str) -> Path:
    try:
        return resolve_workspace_path(root, relative, label="patch path")
    except ToolError as exc:
        raise PatchError(str(exc)) from exc


def preview_patchset(patches: tuple[FilePatch, ...] | list[FilePatch]) -> str:
    sections: list[str] = []
    seen: set[str] = set()
    for patch in patches:
        if patch.path in seen:
            raise PatchError(f"duplicate path in patchset: {patch.path}")
        seen.add(patch.path)
        before = patch.expected_content or ""
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            patch.new_content.splitlines(keepends=True),
            fromfile=f"a/{patch.path}" if patch.expected_content is not None else "/dev/null",
            tofile=f"b/{patch.path}",
        )
        rendered = "".join(diff)
        if rendered:
            sections.append(rendered)
    return "\n".join(sections) if sections else "No changes proposed."


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def apply_patchset(
    workspace: Path, patches: tuple[FilePatch, ...] | list[FilePatch]
) -> PatchResult:
    """Apply a patchset after validating every target; rollback agent changes on failure.

    Existing files require exact expected_content. New files use expected_content=None.
    Deleting files is intentionally unsupported.
    """
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise PatchError("workspace must be an existing directory")
    if not patches:
        return PatchResult(())

    targets: list[tuple[FilePatch, Path, str | None]] = []
    seen: set[str] = set()
    for patch in patches:
        if patch.path in seen:
            raise PatchError(f"duplicate path in patchset: {patch.path}")
        seen.add(patch.path)
        if len(patch.new_content.encode("utf-8")) > MAX_PATCH_FILE_BYTES:
            raise PatchError(f"replacement exceeds 1000000 bytes: {patch.path}")
        target = _resolve(root, patch.path)
        current: str | None
        if target.exists():
            if not target.is_file():
                raise PatchError(f"target is not a regular file: {patch.path}")
            if target.stat().st_size > MAX_PATCH_FILE_BYTES:
                raise PatchError(f"existing file exceeds 1000000 bytes: {patch.path}")
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PatchError(f"cannot read {patch.path}: {exc}") from exc
        else:
            current = None
        if current != patch.expected_content:
            hint = (
                "; expected_content equals new_content but must be the current content "
                "before the change"
                if patch.expected_content == patch.new_content
                else ""
            )
            raise PatchError(f"file changed since planning: {patch.path}{hint}")
        targets.append((patch, target, current))

    applied: list[AppliedChange] = []
    try:
        for patch, target, current in targets:
            latest = target.read_text(encoding="utf-8") if target.exists() else None
            if latest != current:
                raise PatchError(f"file changed while applying patchset: {patch.path}")
            _atomic_write(target, patch.new_content)
            applied.append(
                AppliedChange(
                    patch.path,
                    current,
                    hashlib.sha256(patch.new_content.encode("utf-8")).hexdigest(),
                )
            )
    except BaseException as exc:
        for change in reversed(applied):
            target = _resolve(root, change.path)
            try:
                current_text = target.read_text(encoding="utf-8")
                current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                if current_hash != change.after_sha256:
                    continue
                if change.before_content is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, change.before_content)
            except (OSError, UnicodeDecodeError, PatchError):
                continue
        raise PatchError(f"patchset failed and rollback was attempted: {exc}") from exc
    return PatchResult(tuple(applied))


def restore_changes(
    workspace: Path, changes: tuple[AppliedChange, ...] | list[AppliedChange]
) -> list[str]:
    """Restore only files that still match the content written by this patchset."""
    root = workspace.expanduser().resolve()
    restored: list[str] = []
    for change in reversed(changes):
        target = _resolve(root, change.path)
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PatchError(f"cannot verify {change.path}: {exc}") from exc
        digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if digest != change.after_sha256:
            raise PatchError(f"refusing to overwrite a later change: {change.path}")
        if change.before_content is None:
            target.unlink()
        else:
            _atomic_write(target, change.before_content)
        restored.append(change.path)
    return restored
