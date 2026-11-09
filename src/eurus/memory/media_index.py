from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from eurus.media.store import MediaFile, MediaStore
from eurus.security.redaction import contains_secret, redact_text
from eurus.sessions import DEFAULT_MEMORY_OWNER, SessionStore
from eurus.tools.base import Tool, ToolContext

log = logging.getLogger(__name__)

MEDIA_MEMORY_CATEGORY = "media"
MEDIA_MEMORY_TYPE = "episode"
MEDIA_SOURCE_PREFIX = "media:"
MAX_MEDIA_MEMORY_CHARS = 2000
MAX_DOCUMENT_TEXT_CHARS = 2000
MEDIA_MEMORY_TAG_BY_KIND = {
    "image": "media,image",
    "audio": "media,audio,transcript",
    "video": "media,video,transcript",
    "document": "media,document",
}


def media_source(asset_id: str) -> str:
    return f"{MEDIA_SOURCE_PREFIX}{asset_id}"


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if result >= 0 else None


def _media_description(media: MediaFile) -> str:
    kind = media.kind
    name = media.filename
    mime = media.mime
    return f"{kind} {name} ({mime})"


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    minutes, rest = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes}:{rest:02d}"


def _video_summary_lines(media: MediaFile) -> list[str]:
    lines: list[str] = []
    duration = _as_seconds(media.metadata.get("duration_seconds"))
    if duration is not None:
        lines.append(f"Duration {_format_timestamp(duration)}.")
    if media.metadata.get("width") and media.metadata.get("height"):
        lines.append(f"Resolution {media.metadata.get('width')}x{media.metadata.get('height')}.")
    frames = media.metadata.get("frames")
    if isinstance(frames, list) and frames:
        stamps = ", ".join(
            _format_timestamp(float(entry.get("timestamp_seconds", 0.0)))
            for entry in frames
            if isinstance(entry, dict)
        )
        if stamps:
            lines.append(f"Keyframes at {stamps}.")
    return lines


def _document_text_excerpt(media: MediaFile) -> str:
    from eurus.media.signatures import INLINE_TEXT_MIMES

    if media.mime not in INLINE_TEXT_MIMES:
        return ""
    try:
        text = Path(media.storage_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if len(cleaned) > MAX_DOCUMENT_TEXT_CHARS:
        cleaned = cleaned[:MAX_DOCUMENT_TEXT_CHARS] + "…"
    return cleaned


def derive_media_memory_text(media: MediaFile) -> str:
    parts: list[str] = [_media_description(media)]
    if media.kind == "video":
        parts.extend(_video_summary_lines(media))
        transcript = _as_text(media.metadata.get("transcript"))
        if transcript.strip():
            parts.append(f"Transcript: {transcript.strip()}")
    elif media.kind == "audio":
        duration = _as_seconds(media.metadata.get("duration_seconds"))
        if duration is not None:
            parts.append(f"Duration {_format_timestamp(duration)}.")
        transcript = _as_text(media.metadata.get("transcript"))
        if transcript.strip():
            parts.append(f"Transcript: {transcript.strip()}")
    elif media.kind == "document":
        excerpt = _document_text_excerpt(media)
        if excerpt:
            parts.append(f"Text: {excerpt}")
        title = _as_text(media.metadata.get("title"))
        if title:
            parts.append(f"Title: {title}")
        page_count = media.metadata.get("page_count")
        if isinstance(page_count, int) and not isinstance(page_count, bool):
            parts.append(f"{page_count} pages.")
    elif media.kind == "image":
        width = media.metadata.get("width")
        height = media.metadata.get("height")
        if isinstance(width, int) and isinstance(height, int):
            parts.append(f"Resolution {width}x{height}.")
    text = " ".join(part for part in parts if part.strip()).strip()
    if len(text) > MAX_MEDIA_MEMORY_CHARS:
        text = text[:MAX_MEDIA_MEMORY_CHARS] + "…"
    return text


def media_memory_provenance(
    media: MediaFile, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "asset_id": media.id,
        "kind": media.kind,
        "mime": media.mime,
        "filename": media.filename,
        "session_id": media.session_id,
        "media_created_at": media.created_at,
        "sha256": media.sha256,
        "derivation": "metadata",
    }
    if extra:
        provenance.update(extra)
    return provenance


def _privacy_safe(text: str) -> str:
    redacted = redact_text(text)
    if len(redacted) < len(text):
        log.info("media memory content was redacted before indexing")
    return redacted


_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _privacy_rejected(text: str) -> bool:
    return contains_secret(text) or bool(_EMAIL_PATTERN.search(text))


class MediaMemoryIndexer:
    def __init__(
        self,
        database: Path,
        *,
        owner_id: str = DEFAULT_MEMORY_OWNER,
        media_store: MediaStore | None = None,
        scope: str = "global",
    ) -> None:
        self.store = SessionStore(database)
        self.media_store = media_store
        self.owner_id = owner_id.strip() or DEFAULT_MEMORY_OWNER
        self.scope = scope

    def index_media(self, media: MediaFile) -> int | None:
        return self._upsert(
            media,
            derive_media_memory_text(media),
            provenance=media_memory_provenance(media),
        )

    def index_media_caption(self, media: MediaFile, caption: str) -> int | None:
        cleaned = " ".join(caption.split()).strip()
        if not cleaned:
            return None
        text = f"{_media_description(media)} Caption: {cleaned}"
        if len(text) > MAX_MEDIA_MEMORY_CHARS:
            text = text[:MAX_MEDIA_MEMORY_CHARS] + "…"
        return self._upsert(
            media,
            text,
            provenance=media_memory_provenance(media, extra={"derivation": "vision_analysis"}),
        )

    def forget_media(self, asset_id: str) -> int:
        return self.store.delete_memories_by_source(media_source(asset_id))

    def media_memories(self, asset_id: str) -> list[dict[str, Any]]:
        return self.store.find_memories_by_source(media_source(asset_id))

    def search_media_memories(
        self, query: str, *, limit: int = 5, include_untrusted: bool = False
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        rows = self.store.search_memories_any(
            query.split(),
            limit=max(1, min(limit, 20)) * 3,
            category=MEDIA_MEMORY_CATEGORY,
            scope=None,
            owner_id=self.owner_id,
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            source = str(row.get("source", ""))
            if not source.startswith(MEDIA_SOURCE_PREFIX):
                continue
            if not include_untrusted and str(row.get("trust", "")) == "untrusted":
                continue
            results.append(self._with_asset_view(row))
            if len(results) >= limit:
                break
        return results

    def _with_asset_view(self, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        provenance = row.get("provenance")
        payload: dict[str, Any] = {}
        if isinstance(provenance, str):
            try:
                loaded = json.loads(provenance)
                if isinstance(loaded, dict):
                    payload = loaded
            except ValueError:
                payload = {}
        elif isinstance(provenance, dict):
            payload = provenance
        asset_id = str(payload.get("asset_id", ""))
        enriched["asset_id"] = asset_id
        enriched["asset_available"] = False
        enriched["asset_kind"] = str(payload.get("kind", ""))
        enriched["asset_filename"] = str(payload.get("filename", ""))
        enriched["asset_mime"] = str(payload.get("mime", ""))
        if asset_id and self.media_store is not None:
            existing = self.media_store.get_owned(asset_id, self.owner_id)
            if existing is None:
                existing = self.media_store.get(asset_id)
            enriched["asset_available"] = existing is not None
            if existing is not None:
                enriched["asset_kind"] = existing.kind
                enriched["asset_filename"] = existing.filename
                enriched["asset_mime"] = existing.mime
        return enriched

    def _upsert(
        self,
        media: MediaFile,
        text: str,
        *,
        provenance: dict[str, Any],
    ) -> int | None:
        content = _privacy_safe(text)
        if not content.strip():
            return None
        if _privacy_rejected(content):
            log.warning("media memory rejected by privacy policy asset_id=%s", media.id)
            return None
        self.forget_media(media.id)
        try:
            return self.store.add_memory(
                content,
                MEDIA_MEMORY_TAG_BY_KIND.get(media.kind, "media"),
                category=MEDIA_MEMORY_CATEGORY,
                memory_type=MEDIA_MEMORY_TYPE,
                confidence=0.6,
                source=media_source(media.id),
                scope=self.scope,
                provenance={**provenance, "owner_id": self.owner_id},
                expires_at="",
                trust="derived",
                owner_id=self.owner_id,
            )
        except ValueError as exc:
            log.warning("media memory rejected by policy asset_id=%s reason=%s", media.id, exc)
            return None


def media_marker_for_asset(asset_id: str, kind: str) -> str:
    normalized = kind.strip().lower()
    if normalized == "image":
        return f"[image:{asset_id}]"
    if normalized == "video":
        return f"[video:{asset_id}]"
    if normalized == "audio":
        return f"[voice:{asset_id}]"
    return f"[attach id={asset_id} kind=document name=media mime=application/octet-stream size=0]"


def format_media_memory_row(row: dict[str, Any]) -> str:
    asset_id = str(row.get("asset_id", ""))
    available = bool(row.get("asset_available"))
    kind = str(row.get("asset_kind", ""))
    filename = str(row.get("asset_filename", ""))
    mime = str(row.get("asset_mime", ""))
    content = str(row.get("content", ""))
    marker = media_marker_for_asset(asset_id, kind) if asset_id and available else ""
    header = f"[{filename or 'media'} | {mime}]"
    if marker:
        header = f"{marker} {header}"
    if not available:
        header += " (asset no longer stored)"
    return f"{header} {content}".strip()


def build_media_memory_indexer(
    database: Path,
    *,
    owner_id: str = DEFAULT_MEMORY_OWNER,
    media_store: MediaStore | None = None,
) -> MediaMemoryIndexer:
    return MediaMemoryIndexer(database, owner_id=owner_id, media_store=media_store)


RECALL_MEDIA_TOOL_NAME = "recall_media"

RECALL_MEDIA_DESCRIPTION = (
    "Search previously shared media (images, audio, videos, documents) by describing what you "
    "remember about them, including words from their transcripts or summaries. "
    "Use it when the user asks to find media they sent before, for example "
    "'carikan gambar desain dashboard yang pernah gue kirim' or "
    "'find the voice note about the plan'. "
    "Each result includes a delivery marker ([image:...], [video:...], [voice:...]) plus "
    "the derived description; include a marker verbatim in your answer to send that media "
    "back to the user."
)

RECALL_MEDIA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Words describing the media: filename, topic, or transcript words.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Maximum number of media memories to return.",
        },
    },
    "required": ["query"],
}


def recall_media_tool(
    indexer: MediaMemoryIndexer,
    *,
    database: Path,
    media_store: MediaStore | None = None,
) -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "media search failed: query is required"
        try:
            limit = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        scoped = MediaMemoryIndexer(
            database,
            owner_id=(context.owner_id or "").strip() or indexer.owner_id,
            media_store=media_store,
        )
        rows = scoped.search_media_memories(query, limit=max(1, min(limit, 10)))
        if not rows:
            return "No matching media memories."
        lines = [f"{index + 1}. {format_media_memory_row(row)}" for index, row in enumerate(rows)]
        return "\n".join(lines)

    return Tool(
        name=RECALL_MEDIA_TOOL_NAME,
        description=RECALL_MEDIA_DESCRIPTION,
        parameters=RECALL_MEDIA_SCHEMA,
        handler=handler,
        mutates=False,
    )
