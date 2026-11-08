from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from eurus.content import (
    ArtifactBlock,
    DocumentBlock,
    ImageBlock,
    TextBlock,
    VideoBlock,
    VideoFrameBlock,
)
from eurus.models import Message

log = logging.getLogger(__name__)

MAX_MEDIA_PARTS_PER_REQUEST = 16
DEFAULT_MAX_INLINE_BYTES = 20 * 1024 * 1024

OPENAI_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ANTHROPIC_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ANTHROPIC_DOCUMENT_MIMES = frozenset({"application/pdf"})
GEMINI_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)
GEMINI_DOCUMENT_MIMES = frozenset({"application/pdf"})
GEMINI_VIDEO_MIMES = frozenset(
    {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska", "video/3gpp"}
)

OPENAI_INLINE_BYTES: Mapping[str, int] = {"image/": 20 * 1024 * 1024}
ANTHROPIC_INLINE_BYTES: Mapping[str, int] = {
    "image/": 5 * 1024 * 1024,
    "application/": 20 * 1024 * 1024,
}
GEMINI_INLINE_BYTES: Mapping[str, int] = {
    "image/": 15 * 1024 * 1024,
    "application/": 15 * 1024 * 1024,
    "video/": 10 * 1024 * 1024,
}

WIRE_MODALITIES_BY_PROTOCOL: Mapping[str, frozenset[str]] = {
    "openai": frozenset({"text", "image"}),
    "anthropic": frozenset({"text", "image", "document"}),
    "gemini": frozenset({"text", "image", "document", "video"}),
}
TEXT_ONLY_WIRE_MODALITIES = frozenset({"text"})


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    asset_id: str
    mime: str
    data: bytes
    filename: str


class AssetResolver(Protocol):
    def resolve(self, asset_id: str) -> ResolvedAsset | None: ...


class MediaStoreAssetResolver:
    def __init__(self, database: Path | None = None, root: Path | None = None) -> None:
        self._database = database
        self._root = root
        self._store: Any | None = None

    def _store_handle(self) -> Any:
        if self._store is None:
            from eurus.media.store import MediaStore

            self._store = MediaStore(database=self._database, root=self._root)
        return self._store

    def resolve(self, asset_id: str) -> ResolvedAsset | None:
        try:
            store = self._store_handle()
            row = store.get(asset_id)
            if row is None:
                return None
            path = store.storage_path_for(asset_id)
            if path is None:
                return None
            data = path.read_bytes()
        except Exception:
            log.warning("media asset resolution failed asset_id=%s", asset_id, exc_info=True)
            return None
        if not data:
            return None
        return ResolvedAsset(asset_id=row.id, mime=row.mime, data=data, filename=row.filename)


def _inline_cap(mime: str, caps: Mapping[str, int]) -> int:
    prefix = mime.split("/", 1)[0] + "/"
    if prefix in caps:
        return caps[prefix]
    return caps.get(mime, DEFAULT_MAX_INLINE_BYTES)


def _block_asset(block: Any) -> tuple[str, str] | None:
    if isinstance(block, TextBlock | ArtifactBlock):
        return None
    if isinstance(block, ImageBlock | DocumentBlock | VideoBlock | VideoFrameBlock):
        return block.asset_id, block.mime_type
    return None


def collect_block_assets(messages: Sequence[Message]) -> dict[str, str]:
    requested: dict[str, str] = {}
    for message in reversed(messages):
        if message.role != "user" or not message.blocks:
            continue
        for block in reversed(message.blocks):
            found = _block_asset(block)
            if found is None:
                continue
            asset_id, mime = found
            requested.setdefault(asset_id, mime)
    return requested


async def resolve_request_assets(
    messages: Sequence[Message],
    resolver: AssetResolver,
    *,
    allowed_mimes: frozenset[str],
    inline_bytes: Mapping[str, int],
) -> dict[str, ResolvedAsset]:
    requested = collect_block_assets(messages)
    if not requested:
        return {}
    resolved: dict[str, ResolvedAsset] = {}
    for asset_id, expected_mime in list(requested.items())[:MAX_MEDIA_PARTS_PER_REQUEST]:
        if expected_mime not in allowed_mimes:
            continue
        try:
            asset = await asyncio.to_thread(resolver.resolve, asset_id)
        except Exception:
            log.warning("media asset resolve crashed asset_id=%s", asset_id, exc_info=True)
            continue
        if asset is None:
            log.warning("media asset unavailable asset_id=%s; keeping the text bridge", asset_id)
            continue
        if asset.mime != expected_mime:
            log.warning(
                "media asset mime mismatch asset_id=%s expected=%s stored=%s",
                asset_id,
                expected_mime,
                asset.mime,
            )
            continue
        if len(asset.data) > _inline_cap(asset.mime, inline_bytes):
            log.warning(
                "media asset exceeds the inline render limit asset_id=%s size=%d",
                asset_id,
                len(asset.data),
            )
            continue
        resolved[asset_id] = asset
    return resolved


def _encoded(asset: ResolvedAsset) -> str:
    return base64.b64encode(asset.data).decode("ascii")


def _frame_label(timestamp_seconds: float) -> str:
    return f"[keyframe at {timestamp_seconds:.1f}s]"


def openai_content_parts(
    message: Message, assets: Mapping[str, ResolvedAsset]
) -> list[dict[str, Any]] | None:
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    rendered = 0
    for block in message.blocks:
        if not isinstance(block, ImageBlock | VideoFrameBlock):
            continue
        asset = assets.get(block.asset_id)
        if asset is None:
            continue
        if isinstance(block, VideoFrameBlock):
            parts.append({"type": "text", "text": _frame_label(block.timestamp_seconds)})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{asset.mime};base64,{_encoded(asset)}"},
            }
        )
        rendered += 1
    if rendered == 0:
        return None
    return parts


def anthropic_user_content(
    message: Message, assets: Mapping[str, ResolvedAsset]
) -> list[dict[str, Any]] | None:
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    rendered = 0
    for block in message.blocks:
        asset = assets.get(getattr(block, "asset_id", ""))
        if asset is None:
            continue
        if isinstance(block, VideoFrameBlock) and asset.mime in ANTHROPIC_IMAGE_MIMES:
            parts.append({"type": "text", "text": _frame_label(block.timestamp_seconds)})
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": asset.mime,
                        "data": _encoded(asset),
                    },
                }
            )
            rendered += 1
        elif isinstance(block, ImageBlock) and asset.mime in ANTHROPIC_IMAGE_MIMES:
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": asset.mime,
                        "data": _encoded(asset),
                    },
                }
            )
            rendered += 1
        elif isinstance(block, DocumentBlock) and asset.mime in ANTHROPIC_DOCUMENT_MIMES:
            parts.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": asset.mime,
                        "data": _encoded(asset),
                    },
                }
            )
            rendered += 1
    if rendered == 0:
        return None
    return parts


def gemini_user_parts(
    message: Message, assets: Mapping[str, ResolvedAsset]
) -> list[dict[str, Any]] | None:
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"text": message.content})
    rendered = 0
    native_videos: set[str] = set()
    for block in message.blocks:
        if isinstance(block, VideoBlock) and assets.get(block.asset_id) is not None:
            native_videos.add(block.asset_id)
    for block in message.blocks:
        if isinstance(block, VideoBlock):
            asset = assets.get(block.asset_id)
            if asset is None or asset.mime not in GEMINI_VIDEO_MIMES:
                continue
            parts.append({"inline_data": {"mime_type": asset.mime, "data": _encoded(asset)}})
            rendered += 1
        elif isinstance(block, VideoFrameBlock):
            if block.source_asset_id in native_videos:
                continue
            asset = assets.get(block.asset_id)
            if asset is None or asset.mime not in GEMINI_IMAGE_MIMES:
                continue
            parts.append({"text": _frame_label(block.timestamp_seconds)})
            parts.append({"inline_data": {"mime_type": asset.mime, "data": _encoded(asset)}})
            rendered += 1
        else:
            asset = assets.get(getattr(block, "asset_id", ""))
            if asset is None:
                continue
            if (
                isinstance(block, ImageBlock)
                and asset.mime in GEMINI_IMAGE_MIMES
                or isinstance(block, DocumentBlock)
                and asset.mime in GEMINI_DOCUMENT_MIMES
            ):
                parts.append({"inline_data": {"mime_type": asset.mime, "data": _encoded(asset)}})
                rendered += 1
    if rendered == 0:
        return None
    return parts


def strip_message_blocks(messages: Sequence[Message]) -> tuple[Message, ...]:
    return tuple(replace(message, blocks=()) if message.blocks else message for message in messages)
