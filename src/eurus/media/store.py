from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.media.errors import MediaError, MediaErrorCode, MediaValidationError
from eurus.media.limits import MediaLimits
from eurus.media.signatures import (
    ALLOWED_MEDIA,
    EXT_BY_MIME,
    INLINE_TEXT_MAX_CHARS,
    INLINE_TEXT_MIMES,
    safe_session_component,
    sniff_mime,
)
from eurus.storage.sqlite import connect as sqlite_connect
from eurus.storage.sqlite import managed as sqlite_managed

MAX_MEDIA_BYTES = 20 * 1024 * 1024

MEDIA_STATUSES = frozenset({"registered", "processing", "ready", "failed"})

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS media_files (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    error_code TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS media_files_session ON media_files(session_id, created_at);
CREATE INDEX IF NOT EXISTS media_files_sha ON media_files(session_id, sha256);
"""

_COLUMN_MIGRATIONS: dict[str, str] = {
    "owner_id": "ALTER TABLE media_files ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
    "status": "ALTER TABLE media_files ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'",
    "error_code": "ALTER TABLE media_files ADD COLUMN error_code TEXT NOT NULL DEFAULT ''",
    "metadata_json": "ALTER TABLE media_files ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
    "updated_at": "ALTER TABLE media_files ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class MediaFile:
    id: str
    session_id: str
    filename: str
    mime: str
    kind: str
    size: int
    sha256: str
    storage_path: str
    created_at: str
    owner_id: str = ""
    status: str = "ready"
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def view(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "session_id": self.session_id,
            "filename": self.filename,
            "mime": self.mime,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "url": f"/api/media/{self.id}/content",
            "status": self.status,
        }
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        if self.error_code:
            data["error_code"] = self.error_code
        return data


def sanitize_filename(name: str) -> str:
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    base = _FILENAME_SAFE.sub("_", base).strip("._-") or "file"
    return base[:120]


def attachment_marker(file: MediaFile) -> str:
    from urllib.parse import quote

    return (
        f"[attach id={file.id} kind={file.kind}"
        f" name={quote(file.filename, safe='')} mime={quote(file.mime, safe='')}"
        f" size={file.size}]"
    )


def compose_attachment_block(files: list[MediaFile]) -> str:
    if not files:
        return ""
    lines: list[str] = []
    for file in files:
        lines.append("")
        lines.append(attachment_marker(file))
        lines.append(f"Attachment: {file.filename} ({file.mime}, {file.size} bytes)")
        if file.kind == "audio":
            duration = file.metadata.get("duration_seconds")
            if isinstance(duration, int | float) and not isinstance(duration, bool):
                lines.append(f"Duration: {float(duration):.1f} seconds")
            transcript = file.metadata.get("transcript")
            if isinstance(transcript, str) and transcript.strip():
                text = transcript.strip()
                if len(text) > INLINE_TEXT_MAX_CHARS:
                    text = (
                        text[:INLINE_TEXT_MAX_CHARS]
                        + f"\n…[truncated {len(text) - INLINE_TEXT_MAX_CHARS} more characters]"
                    )
                lines.append(f"--- begin transcript of {file.filename} ---")
                lines.append(text)
                lines.append(f"--- end transcript of {file.filename} ---")
        if file.kind == "video":
            _video_context_lines(lines, file)
        if file.mime in INLINE_TEXT_MIMES:
            path = Path(file.storage_path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if text:
                if len(text) > INLINE_TEXT_MAX_CHARS:
                    text = (
                        text[:INLINE_TEXT_MAX_CHARS]
                        + f"\n…[truncated {len(text) - INLINE_TEXT_MAX_CHARS} more characters]"
                    )
                lines.append(f"--- begin {file.filename} ---")
                lines.append(text)
                lines.append(f"--- end {file.filename} ---")
            else:
                lines.append(f"(stored at {path})")
        else:
            lines.append(f"(stored at {file.storage_path})")
    return "\n".join(lines)


def _video_context_lines(lines: list[str], file: MediaFile) -> None:
    duration = file.metadata.get("duration_seconds")
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        lines.append(f"Duration: {float(duration):.1f} seconds")
    if file.metadata.get("width") and file.metadata.get("height"):
        lines.append(f"Resolution: {file.metadata.get('width')}x{file.metadata.get('height')}")
    frames = file.metadata.get("frames")
    if isinstance(frames, list) and frames:
        stamps = ", ".join(
            f"{float(entry.get('timestamp_seconds', 0.0)):.1f}s"
            for entry in frames
            if isinstance(entry, dict)
        )
        if stamps:
            lines.append(f"Keyframes sampled at: {stamps}")
    transcript = file.metadata.get("transcript")
    if isinstance(transcript, str) and transcript.strip():
        text = transcript.strip()
        if len(text) > INLINE_TEXT_MAX_CHARS:
            text = (
                text[:INLINE_TEXT_MAX_CHARS]
                + f"\n…[truncated {len(text) - INLINE_TEXT_MAX_CHARS} more characters]"
            )
        lines.append(f"--- begin transcript of {file.filename} ---")
        lines.append(text)
        lines.append(f"--- end transcript of {file.filename} ---")


def attachment_content_blocks(files: list[MediaFile]) -> tuple[Any, ...]:
    from eurus.content import AudioBlock, DocumentBlock, ImageBlock

    blocks: list[Any] = []
    for file in files:
        if file.kind == "image":
            blocks.append(ImageBlock(asset_id=file.id, mime_type=file.mime, alt_text=file.filename))
        elif file.kind == "audio":
            duration = file.metadata.get("duration_seconds")
            transcript = file.metadata.get("transcript")
            blocks.append(
                AudioBlock(
                    asset_id=file.id,
                    mime_type=file.mime,
                    duration_seconds=(
                        float(duration)
                        if isinstance(duration, int | float) and not isinstance(duration, bool)
                        else None
                    ),
                    transcript=transcript if isinstance(transcript, str) else "",
                )
            )
        elif file.kind == "video":
            blocks.extend(video_content_blocks(file))
        else:
            blocks.append(
                DocumentBlock(asset_id=file.id, mime_type=file.mime, filename=file.filename)
            )
    return tuple(blocks)


def video_content_blocks(file: MediaFile) -> tuple[Any, ...]:
    from eurus.content import VideoBlock, VideoFrameBlock

    duration = file.metadata.get("duration_seconds")
    transcript = file.metadata.get("transcript")
    thumbnail = file.metadata.get("thumbnail_asset_id")
    video_block = VideoBlock(
        asset_id=file.id,
        mime_type=file.mime,
        duration_seconds=(
            float(duration)
            if isinstance(duration, int | float) and not isinstance(duration, bool)
            else None
        ),
        thumbnail_asset_id=thumbnail if isinstance(thumbnail, str) and thumbnail else None,
        transcript=transcript if isinstance(transcript, str) else "",
    )
    blocks: list[Any] = [video_block]
    frames = file.metadata.get("frames")
    if isinstance(frames, list):
        for entry in frames:
            if not isinstance(entry, dict):
                continue
            frame_asset_id = entry.get("asset_id")
            stamp = entry.get("timestamp_seconds")
            if not isinstance(frame_asset_id, str) or not _SAFE_ID.match(frame_asset_id):
                continue
            if not isinstance(stamp, int | float) or isinstance(stamp, bool):
                continue
            blocks.append(
                VideoFrameBlock(
                    asset_id=frame_asset_id,
                    source_asset_id=file.id,
                    mime_type="image/jpeg",
                    timestamp_seconds=float(stamp),
                )
            )
    return tuple(blocks)


class MediaStore:
    def __init__(
        self,
        database: Path | None = None,
        root: Path | None = None,
        *,
        limits: MediaLimits | None = None,
    ) -> None:
        self.database = database if database is not None else paths.database_path()
        self.root = root if root is not None else paths.home_manager().media_dir
        self.limits = limits if limits is not None else MediaLimits.from_environ()
        paths.ensure_private_dir(self.root)
        if not self.database.exists():
            self.database.touch(mode=0o600)
        with suppress(OSError):
            os.chmod(self.database, 0o600)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_connect(self.database, rows=True)

    def _ensure_schema(self) -> None:
        with sqlite_managed(self.database, rows=True) as connection:
            connection.executescript(_SCHEMA_SQL)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(media_files)")}
            for column, statement in _COLUMN_MIGRATIONS.items():
                if column not in columns:
                    connection.execute(statement)

    def save(
        self,
        session_id: str,
        filename: str,
        data: bytes,
        *,
        declared_mime: str | None = None,
        owner_id: str = "",
        deduplicate: bool = True,
    ) -> MediaFile:
        session = safe_session_component(session_id)
        if not data:
            raise MediaValidationError("file is empty", code=MediaErrorCode.EMPTY_FILE.value)
        self.limits.validate_size(len(data))
        safe_name = sanitize_filename(filename)
        mime, kind = sniff_mime(data, declared_mime, safe_name)
        digest = hashlib.sha256(data).hexdigest()
        if deduplicate:
            existing = self.find_by_sha256(session, digest)
            if existing is not None:
                return existing
        media_id = uuid.uuid4().hex
        ext = EXT_BY_MIME.get(mime)
        if not ext:
            raise MediaError(
                f"unsupported media type: {mime}",
                code=MediaErrorCode.UNSUPPORTED_TYPE.value,
            )
        session_dir = self.root / session
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage_path = session_dir / f"{media_id}.{ext}"
        storage_path.write_bytes(data)
        os.chmod(storage_path, 0o600)
        created_at = _now()
        row = MediaFile(
            id=media_id,
            session_id=session,
            filename=safe_name,
            mime=mime,
            kind=kind,
            size=len(data),
            sha256=digest,
            storage_path=str(storage_path),
            created_at=created_at,
            owner_id=owner_id,
            status="registered",
            error_code="",
            metadata={},
            updated_at=created_at,
        )
        with sqlite_managed(self.database, rows=True) as connection:
            connection.execute(
                """INSERT INTO media_files
                   (id, session_id, filename, mime, kind, size, sha256, storage_path,
                    created_at, owner_id, status, error_code, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.id,
                    row.session_id,
                    row.filename,
                    row.mime,
                    row.kind,
                    row.size,
                    row.sha256,
                    row.storage_path,
                    row.created_at,
                    row.owner_id,
                    row.status,
                    row.error_code,
                    json.dumps(row.metadata, ensure_ascii=False),
                    row.updated_at,
                ),
            )
        return row

    def get(self, media_id: str) -> MediaFile | None:
        if not _SAFE_ID.match(media_id or ""):
            return None
        with sqlite_managed(self.database, rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM media_files WHERE id = ?", (media_id,)
            ).fetchone()
        return self._to_file(row) if row is not None else None

    def get_for_session(self, media_id: str, session_id: str) -> MediaFile | None:
        row = self.get(media_id)
        if row is None or row.session_id != safe_session_component(session_id):
            return None
        return row

    def get_owned(self, media_id: str, owner_id: str) -> MediaFile | None:
        row = self.get(media_id)
        if row is None:
            return None
        if row.owner_id and row.owner_id != owner_id:
            return None
        return row

    def find_by_sha256(self, session_id: str, digest: str) -> MediaFile | None:
        if not re.match(r"^[a-f0-9]{64}$", digest or ""):
            return None
        session = safe_session_component(session_id)
        with sqlite_managed(self.database, rows=True) as connection:
            row = connection.execute(
                "SELECT * FROM media_files WHERE session_id = ? AND sha256 = ?"
                " ORDER BY created_at, id LIMIT 1",
                (session, digest),
            ).fetchone()
        return self._to_file(row) if row is not None else None

    def update_status(
        self,
        media_id: str,
        status: str,
        *,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> MediaFile | None:
        if status not in MEDIA_STATUSES:
            raise ValueError(f"status must be one of {sorted(MEDIA_STATUSES)}")
        current = self.get(media_id)
        if current is None:
            return None
        merged = dict(current.metadata)
        if metadata:
            merged.update(metadata)
        updated_at = _now()
        with sqlite_managed(self.database, rows=True) as connection:
            connection.execute(
                """UPDATE media_files
                   SET status = ?, error_code = ?, metadata_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    error_code,
                    json.dumps(merged, ensure_ascii=False),
                    updated_at,
                    media_id,
                ),
            )
        return self.get(media_id)

    def mark_processing(self, media_id: str) -> MediaFile | None:
        return self.update_status(media_id, "processing")

    def mark_ready(self, media_id: str, metadata: dict[str, Any] | None = None) -> MediaFile | None:
        return self.update_status(media_id, "ready", metadata=metadata)

    def mark_failed(self, media_id: str, error_code: str) -> MediaFile | None:
        return self.update_status(media_id, "failed", error_code=error_code)

    def list_for_session(self, session_id: str) -> list[MediaFile]:
        session = safe_session_component(session_id)
        with sqlite_managed(self.database, rows=True) as connection:
            rows = connection.execute(
                "SELECT * FROM media_files WHERE session_id = ? ORDER BY created_at, id",
                (session,),
            ).fetchall()
        return [self._to_file(row) for row in rows]

    def list_recent(self, limit: int = 200, *, session_id: str | None = None) -> list[MediaFile]:
        if limit < 1:
            limit = 1
        with sqlite_managed(self.database, rows=True) as connection:
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM media_files WHERE session_id = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM media_files ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._to_file(row) for row in rows]

    def delete(self, media_id: str) -> bool:
        row = self.get(media_id)
        if row is None:
            return False
        with sqlite_managed(self.database, rows=True) as connection:
            cursor = connection.execute("DELETE FROM media_files WHERE id = ?", (media_id,))
        with suppress(OSError):
            Path(row.storage_path).unlink(missing_ok=True)
        return cursor.rowcount > 0

    def purge_expired(self, retention_days: int | None = None) -> int:
        days = self.limits.retention_days if retention_days is None else retention_days
        if days <= 0:
            return 0
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        with sqlite_managed(self.database, rows=True) as connection:
            rows = connection.execute(
                "SELECT id, storage_path FROM media_files WHERE created_at < ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                return 0
            connection.executemany(
                "DELETE FROM media_files WHERE id = ?",
                [(str(row["id"]),) for row in rows],
            )
        for row in rows:
            with suppress(OSError):
                Path(str(row["storage_path"])).unlink(missing_ok=True)
        return len(rows)

    def storage_path_for(self, media_id: str) -> Path | None:
        row = self.get(media_id)
        if row is None:
            return None
        path = Path(row.storage_path)
        root = self.root.resolve()
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise MediaValidationError(
                "media storage path escapes the media root",
                code=MediaErrorCode.TRAVERSAL_BLOCKED.value,
            )
        return path

    @staticmethod
    def _to_file(row: sqlite3.Row) -> MediaFile:
        columns = set(row.keys())
        metadata: dict[str, Any] = {}
        if "metadata_json" in columns:
            try:
                loaded = json.loads(row["metadata_json"] or "{}")
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                metadata = loaded
        return MediaFile(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            filename=str(row["filename"]),
            mime=str(row["mime"]),
            kind=str(row["kind"]),
            size=int(row["size"]),
            sha256=str(row["sha256"]),
            storage_path=str(row["storage_path"]),
            created_at=str(row["created_at"]),
            owner_id=str(row["owner_id"]) if "owner_id" in columns else "",
            status=str(row["status"]) if "status" in columns else "ready",
            error_code=str(row["error_code"]) if "error_code" in columns else "",
            metadata=metadata,
            updated_at=str(row["updated_at"]) if "updated_at" in columns else "",
        )


def allowed_media() -> tuple[tuple[str, str, str], ...]:
    return ALLOWED_MEDIA
