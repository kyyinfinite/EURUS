from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eurus.multiagent.artifacts import (
    STANDALONE_MISSION_ID,
    ArtifactError,
    ArtifactStore,
)

if TYPE_CHECKING:
    from eurus.tools.base import ToolContext

log = logging.getLogger(__name__)

DEFAULT_ARTIFACT_RETENTION_DAYS = 30
MAX_PUBLISH_ATTEMPT_BYTES = 10 * 1024 * 1024

_ARTIFACT_TYPE_BY_SUFFIX = {
    ".txt": ("text", "text/plain"),
    ".md": ("text", "text/markdown"),
    ".csv": ("data", "text/csv"),
    ".json": ("data", "application/json"),
    ".html": ("document", "text/html"),
    ".pdf": ("document", "application/pdf"),
    ".docx": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".xlsx": (
        "data",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".pptx": (
        "design",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
}


def classify_artifact(path: Path) -> tuple[str, str]:
    entry = _ARTIFACT_TYPE_BY_SUFFIX.get(path.suffix.lower())
    if entry is not None:
        return entry
    return "text", "text/plain"


class ArtifactProvider:
    def __init__(self, database_path: Path, storage_root: Path) -> None:
        self.database_path = Path(database_path)
        self.storage_root = Path(storage_root)
        self._store: ArtifactStore | None = None

    def store(self) -> ArtifactStore:
        if self._store is None:
            self._store = ArtifactStore(self.database_path, self.storage_root)
        return self._store

    def __call__(self) -> ArtifactStore:
        return self.store()


_provider_cache: dict[tuple[str, str], ArtifactProvider] = {}


def artifact_provider(database_path: Path, storage_root: Path) -> ArtifactProvider:
    key = (str(database_path), str(storage_root))
    provider = _provider_cache.get(key)
    if provider is None:
        provider = ArtifactProvider(database_path, storage_root)
        _provider_cache[key] = provider
    return provider


def _emit(context: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
    sink = context.event_sink
    if sink is None:
        return
    try:
        sink(event_type, payload)
    except Exception:
        log.debug("artifact event emission failed", exc_info=True)


def artifact_view(record: Any) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "name": record.name,
        "artifact_type": record.artifact_type,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "version": record.version,
        "run_id": record.run_id,
    }


def register_tool_artifact(
    context: ToolContext,
    path: Path,
    *,
    source_tool: str,
    artifact_type: str | None = None,
    media_type: str | None = None,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    provider = context.artifacts
    if provider is None:
        return None
    resolved = Path(path)
    try:
        size = resolved.stat().st_size
    except OSError:
        log.warning("artifact registration skipped; file is not readable: %s", resolved)
        return None
    if size <= 0:
        log.warning("artifact registration skipped; file is empty: %s", resolved)
        return None
    if size > MAX_PUBLISH_ATTEMPT_BYTES:
        log.warning(
            "artifact registration skipped; %s is %d bytes, above the publish limit",
            resolved,
            size,
        )
        return None
    inferred_type, inferred_media = classify_artifact(resolved)
    try:
        data = resolved.read_bytes()
    except OSError:
        log.warning("artifact registration skipped; file could not be read: %s", resolved)
        return None
    owner = (context.owner_id or "local").strip() or "local"
    mission_id = (context.mission_id or "").strip() or STANDALONE_MISSION_ID
    store = provider()
    if size > store.max_bytes:
        log.warning(
            "artifact registration skipped; %s is %d bytes, above the store limit of %d",
            resolved,
            size,
            store.max_bytes,
        )
        return None
    try:
        record = store.publish(
            data,
            owner_id=owner,
            mission_id=mission_id,
            task_id=(context.task_id or "").strip() or None,
            artifact_type=artifact_type or inferred_type,
            media_type=media_type or inferred_media,
            metadata=metadata or {},
            name=(name or resolved.name).strip(),
            source_tool=source_tool,
            run_id=(context.run_id or "").strip(),
        )
    except ArtifactError as exc:
        log.warning("artifact registration failed for %s: %s", resolved, exc)
        return None
    except OSError as exc:
        log.warning("artifact registration failed for %s: %s", resolved, exc)
        return None
    _emit(
        context,
        "artifact.registered",
        {
            "artifact_id": record.artifact_id,
            "name": record.name,
            "artifact_type": record.artifact_type,
            "media_type": record.media_type,
            "size_bytes": record.size_bytes,
            "source_tool": record.source_tool,
            "run_id": record.run_id,
            "mission_id": record.mission_id,
            "path": str(resolved),
        },
    )
    return artifact_view(record)


def default_provider_for_home() -> ArtifactProvider | None:
    from eurus import paths

    try:
        database = paths.database_path()
    except Exception:
        return None
    return artifact_provider(database, paths.home_dir() / "data" / "artifacts")


__all__ = [
    "DEFAULT_ARTIFACT_RETENTION_DAYS",
    "ArtifactError",
    "ArtifactProvider",
    "artifact_provider",
    "artifact_view",
    "classify_artifact",
    "default_provider_for_home",
    "register_tool_artifact",
]
