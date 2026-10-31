from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.coding.paths import atomic_write_text, resolve_workspace_path
from eurus.errors import ToolError
from eurus.models import ToolCall
from eurus.tools.base import Tool

MAX_SNAPSHOT_BYTES = 1_000_000
MAX_FILES_PER_RUN = 200


@dataclass(frozen=True, slots=True)
class CheckpointFile:
    path: str
    before_content: str | None
    after_sha256: str | None = None
    unsupported: bool = False


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: str
    workspace: str
    created_at: str
    files: tuple[CheckpointFile, ...]


class CheckpointStore:
    """Per-run pre-image snapshots for /undo and `eurus undo <run_id>`.

    Before an approved mutating file tool runs, the store captures the current
    content of every target path. When the run finishes, the manifest is
    finalized with the content the run left behind (SHA-256 of the raw bytes).
    Undo restores a file only when its current bytes still match what the run
    wrote; anything changed afterwards is refused. No git is required, so this
    degrades cleanly on workspaces that are not repositories.
    """

    def __init__(self, home: Path | None = None) -> None:
        self.directory = home / "undo" if home is not None else paths.home_manager().undo_dir

    def _manifest_path(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")[:120]
        if not safe:
            raise ValueError("invalid run id")
        return self.directory / f"{safe}.json"

    def record(self, run_id: str, workspace: Path, targets: Iterable[Path]) -> list[str]:
        """Capture before-content for target paths the first time they are touched."""
        manifest = self._load(run_id)
        known = {item.path for item in manifest.files} if manifest else set()
        recorded: list[str] = []
        entries = list(manifest.files) if manifest else []
        root = workspace.expanduser().resolve()
        for target in targets:
            try:
                resolved = target.resolve()
                relative = resolved.relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            if relative in known:
                continue
            if len(entries) >= MAX_FILES_PER_RUN:
                return recorded
            try:
                if resolved.exists() and resolved.is_file():
                    if resolved.stat().st_size > MAX_SNAPSHOT_BYTES:
                        entries.append(CheckpointFile(relative, None, unsupported=True))
                    else:
                        entries.append(
                            CheckpointFile(relative, resolved.read_text(encoding="utf-8"))
                        )
                else:
                    entries.append(CheckpointFile(relative, None))
            except (OSError, UnicodeDecodeError):
                entries.append(CheckpointFile(relative, None, unsupported=True))
            known.add(relative)
            recorded.append(relative)
        if manifest is None and not entries:
            return []
        self._write(
            Checkpoint(
                run_id=run_id,
                workspace=str(root),
                created_at=manifest.created_at if manifest else _now(),
                files=tuple(entries),
            )
        )
        return recorded

    def finalize(self, run_id: str, workspace: Path) -> Path | None:
        """Pin the post-run content hash for every recorded file; write the manifest."""
        manifest = self._load(run_id)
        if manifest is None:
            return None
        root = workspace.expanduser().resolve()
        finalized: list[CheckpointFile] = []
        for item in manifest.files:
            if item.unsupported or item.after_sha256 is not None:
                finalized.append(item)
                continue
            target = root / item.path
            try:
                digest = _hash_file(target) if target.is_file() else None
            except OSError:
                digest = None
            finalized.append(replace(item, after_sha256=digest))
        path = self._write(
            Checkpoint(
                run_id=run_id,
                workspace=str(root),
                created_at=manifest.created_at,
                files=tuple(finalized),
            )
        )
        return path

    def load(self, run_id: str) -> Checkpoint | None:
        return self._load(run_id)

    def latest(self, workspace: Path) -> Checkpoint | None:
        found: list[Checkpoint] = []
        if not self.directory.is_dir():
            return None
        for path in self.directory.glob("*.json"):
            manifest = self._read(path)
            if manifest is None:
                continue
            if manifest.workspace != str(workspace.expanduser().resolve()):
                continue
            if all(item.after_sha256 is not None or item.unsupported for item in manifest.files):
                found.append(manifest)
        if not found:
            return None
        found.sort(key=lambda item: item.created_at, reverse=True)
        return found[0]

    def undo(self, run_id: str, workspace: Path) -> tuple[list[str], list[str]]:
        """Restore the files changed by a run; refuse files modified afterwards."""
        manifest = self._load(run_id)
        if manifest is None:
            raise ValueError(f"no checkpoint found for run {run_id}")
        root = workspace.expanduser().resolve()
        if manifest.workspace != str(root):
            raise ValueError("checkpoint belongs to a different workspace")
        restored: list[str] = []
        refused: list[str] = []
        for item in reversed(manifest.files):
            if item.unsupported:
                refused.append(f"{item.path} (snapshot unsupported)")
                continue
            target = root / item.path
            try:
                digest = _hash_file(target) if target.is_file() else None
            except OSError:
                digest = None
            if digest is None and item.after_sha256 is None:
                pass
            elif digest != item.after_sha256:
                refused.append(f"{item.path} (changed after the run)")
                continue
            try:
                if item.before_content is None:
                    if target.exists():
                        target.unlink()
                else:
                    atomic_write_text(target, item.before_content)
            except OSError:
                refused.append(f"{item.path} (cannot write)")
                continue
            restored.append(item.path)
        if restored:
            with contextlib.suppress(OSError):
                self._manifest_path(run_id).unlink()
        return restored, refused

    def _write(self, checkpoint: Checkpoint) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "run_id": checkpoint.run_id,
            "workspace": checkpoint.workspace,
            "created_at": checkpoint.created_at,
            "files": [
                {
                    "path": item.path,
                    "before_content": item.before_content,
                    "after_sha256": item.after_sha256,
                    "unsupported": item.unsupported,
                }
                for item in checkpoint.files
            ],
        }
        target = self._manifest_path(checkpoint.run_id)
        atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=1))
        return target

    def _load(self, run_id: str) -> Checkpoint | None:
        try:
            path = self._manifest_path(run_id)
        except ValueError:
            return None
        return self._read(path)

    def _read(self, path: Path) -> Checkpoint | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        files = tuple(
            CheckpointFile(
                path=str(item.get("path", "")),
                before_content=(
                    None if item.get("before_content") is None else str(item["before_content"])
                ),
                after_sha256=(
                    None if item.get("after_sha256") is None else str(item["after_sha256"])
                ),
                unsupported=bool(item.get("unsupported", False)),
            )
            for item in data.get("files", [])
            if isinstance(item, dict)
        )
        return Checkpoint(
            run_id=str(data.get("run_id", "")),
            workspace=str(data.get("workspace", "")),
            created_at=str(data.get("created_at", "")),
            files=files,
        )


def mutation_targets(call: ToolCall, tool: Tool, workspace: Path) -> list[Path]:
    """Workspace paths a mutating tool call will touch (path keys + patchsets)."""
    if not tool.mutates:
        return []
    raw: list[str] = []
    for key in tool.path_keys:
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            raw.append(value)
    if tool.name == "apply_patchset":
        patches = call.arguments.get("patches")
        if isinstance(patches, list):
            raw.extend(
                str(item.get("path"))
                for item in patches
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
    root = workspace.expanduser().resolve()
    targets: list[Path] = []
    for value in raw:
        try:
            targets.append(resolve_workspace_path(root, value, label="path"))
        except ToolError:
            continue
    return targets


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat()
