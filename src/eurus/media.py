"""Canonical media store for EURUS interfaces (web gateway, TUI, CLI).

Uploaded conversation files live on disk under ``~/.eurus/media/`` with
metadata rows in the shared ``sessions.sqlite3`` database. Message content
references media by id; binary payloads never enter message text.

The store is deliberately interface-agnostic: any gateway can save a file,
hand the id back to its frontend, and later resolve the id to a local path.
MIME types and kinds are derived from a fixed allowlist so every interface
agrees on what "image", "audio", "video" and "document" mean.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.storage.sqlite import connect as sqlite_connect
from eurus.storage.sqlite import managed as sqlite_managed

MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MB per file

# (mime, kind, canonical extension) triples. kind drives frontend rendering
# and the annotation block composed into message text.
ALLOWED_MEDIA: tuple[tuple[str, str, str], ...] = (
    ("image/png", "image", "png"),
    ("image/jpeg", "image", "jpg"),
    ("image/webp", "image", "webp"),
    ("image/gif", "image", "gif"),
    ("audio/mpeg", "audio", "mp3"),
    ("audio/wav", "audio", "wav"),
    ("audio/x-wav", "audio", "wav"),
    ("audio/webm", "audio", "webm"),
    ("audio/ogg", "audio", "ogg"),
    ("audio/mp4", "audio", "m4a"),
    ("video/mp4", "video", "mp4"),
    ("video/webm", "video", "webm"),
    ("application/pdf", "document", "pdf"),
    ("text/plain", "document", "txt"),
    ("text/markdown", "document", "md"),
    ("text/csv", "document", "csv"),
    ("application/json", "document", "json"),
    ("text/xml", "document", "xml"),
    ("application/xml", "document", "xml"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document", "docx"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "document", "xlsx"),
    # Code and data sources: stored and inlined as text so text-only model
    # providers can genuinely read them. Kind stays "document" — the media
    # contract has four kinds (image/audio/video/document).
    ("text/x-python", "document", "py"),
    ("text/javascript", "document", "js"),
    ("application/javascript", "document", "js"),
    ("text/typescript", "document", "ts"),
    ("text/x-typescript", "document", "ts"),
    ("text/jsx", "document", "jsx"),
    ("text/tsx", "document", "tsx"),
    ("text/html", "document", "html"),
    ("text/css", "document", "css"),
    ("application/sql", "document", "sql"),
    ("text/x-shellscript", "document", "sh"),
    ("text/yaml", "document", "yaml"),
    ("text/x-yaml", "document", "yaml"),
    ("text/x-toml", "document", "toml"),
    ("text/x-rst", "document", "rst"),
    ("text/x-c", "document", "c"),
    ("text/x-c++src", "document", "cpp"),
    ("text/x-java", "document", "java"),
    ("text/x-go", "document", "go"),
    ("text/x-rust", "document", "rs"),
)

MIME_BY_EXT: dict[str, tuple[str, str]] = {}
for _mime, _kind, _ext in ALLOWED_MEDIA:
    MIME_BY_EXT.setdefault(_ext, (_mime, _kind))

KIND_BY_MIME: dict[str, str] = {_mime: _kind for _mime, _kind, _ in ALLOWED_MEDIA}
EXT_BY_MIME: dict[str, str] = {_mime: _ext for _mime, _, _ext in ALLOWED_MEDIA}

# Formats whose text content can be inlined into a message for text-only
# model providers (the agent loop is text-only today).
INLINE_TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "text/xml",
        "application/xml",
        "text/x-python",
        "text/javascript",
        "application/javascript",
        "text/typescript",
        "text/x-typescript",
        "text/jsx",
        "text/tsx",
        "text/html",
        "text/css",
        "application/sql",
        "text/x-shellscript",
        "text/yaml",
        "text/x-yaml",
        "text/x-toml",
        "text/x-rst",
        "text/x-c",
        "text/x-c++src",
        "text/x-java",
        "text/x-go",
        "text/x-rust",
    }
)
INLINE_TEXT_MAX_CHARS = 12_000

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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_files_session ON media_files(session_id, created_at);
"""


class MediaError(ValueError):
    """Raised for rejected uploads (size, type, unsafe input)."""


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

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "filename": self.filename,
            "mime": self.mime,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "url": f"/api/media/{self.id}/content",
        }


def sanitize_filename(name: str) -> str:
    """Collapse a client-supplied filename to a safe, readable basename."""
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    base = _FILENAME_SAFE.sub("_", base).strip("._-") or "file"
    return base[:120]


def sniff_mime(data: bytes, declared: str | None, filename: str) -> tuple[str, str]:
    """Return (mime, kind) using magic bytes first, falling back to extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image"
    if data[:4] == b"%PDF":
        return "application/pdf", "document"
    if data[:4] == b"PK\x03\x04":
        # OOXML or mp4 inside a zip container: prefer the declared type when
        # it is an allowed office format, else the declared type if allowed.
        if declared in KIND_BY_MIME:
            return declared, KIND_BY_MIME[declared]
        return "application/octet-stream", "document"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ("audio/mpeg", "audio")
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "audio"
    if data[:4] == b"OggS":
        return "audio/ogg", "audio"
    # Fall back to the extension mapping for text-like and container files.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in MIME_BY_EXT:
        mime, kind = MIME_BY_EXT[ext]
        # Trust the declared mime when both agree on the kind.
        if declared == mime:
            return mime, kind
        return mime, kind
    if declared in KIND_BY_MIME:
        return declared, KIND_BY_MIME[declared]
    raise MediaError(f"unsupported file type: {filename or 'unnamed'}")


class MediaStore:
    """Disk + SQLite storage for conversation media, scoped to sessions."""

    def __init__(self, database: Path | None = None, root: Path | None = None) -> None:
        self.database = database if database is not None else paths.database_path()
        self.root = root if root is not None else paths.home_manager().media_dir
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

    def save(
        self,
        session_id: str,
        filename: str,
        data: bytes,
        *,
        declared_mime: str | None = None,
    ) -> MediaFile:
        if not session_id or not session_id.strip():
            raise MediaError("session id is required")
        if not data:
            raise MediaError("file is empty")
        if len(data) > MAX_MEDIA_BYTES:
            raise MediaError(
                f"file exceeds the {MAX_MEDIA_BYTES // (1024 * 1024)} MB limit"
            )
        safe_name = sanitize_filename(filename)
        mime, kind = sniff_mime(data, declared_mime, safe_name)
        media_id = uuid.uuid4().hex
        ext = EXT_BY_MIME[mime]
        digest = hashlib.sha256(data).hexdigest()
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        storage_path = session_dir / f"{media_id}.{ext}"
        storage_path.write_bytes(data)
        import datetime as _dt

        created_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        row = MediaFile(
            id=media_id,
            session_id=session_id,
            filename=safe_name,
            mime=mime,
            kind=kind,
            size=len(data),
            sha256=digest,
            storage_path=str(storage_path),
            created_at=created_at,
        )
        with sqlite_managed(self.database, rows=True) as connection:
            connection.execute(
                """INSERT INTO media_files
                   (id, session_id, filename, mime, kind, size, sha256, storage_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def list_for_session(self, session_id: str) -> list[MediaFile]:
        with sqlite_managed(self.database, rows=True) as connection:
            rows = connection.execute(
                "SELECT * FROM media_files WHERE session_id = ? ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
        return [self._to_file(row) for row in rows]

    def list_recent(self, limit: int = 200, *, session_id: str | None = None) -> list[MediaFile]:
        """Most recent media rows, newest first (optionally per session)."""
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
            cursor = connection.execute(
                "DELETE FROM media_files WHERE id = ?", (media_id,)
            )
        from contextlib import suppress

        with suppress(OSError):
            Path(row.storage_path).unlink(missing_ok=True)
        return cursor.rowcount > 0

    @staticmethod
    def _to_file(row: sqlite3.Row) -> MediaFile:
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
        )


def attachment_marker(file: MediaFile) -> str:
    """Canonical single-line reference embedded into message content.

    Format (stable contract; the web renderer and future TUI renderer parse
    this): ``[attach id=<hex> kind=<kind> name=<pct> mime=<pct> size=<n>]``
    """
    from urllib.parse import quote

    return (
        f"[attach id={file.id} kind={file.kind}"
        f" name={quote(file.filename, safe='')} mime={quote(file.mime, safe='')}"
        f" size={file.size}]"
    )


def compose_attachment_block(files: list[MediaFile]) -> str:
    """Build the text appended to a user message for its attachments.

    Text-like documents are inlined (bounded) so text-only model providers
    can genuinely work with the content. Binary media is referenced by
    metadata and storage path — the agent may inspect it with its file tools
    when workspace permissions allow.
    """
    if not files:
        return ""
    lines: list[str] = []
    for file in files:
        lines.append("")
        lines.append(attachment_marker(file))
        lines.append(f"Attachment: {file.filename} ({file.mime}, {file.size} bytes)")
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
