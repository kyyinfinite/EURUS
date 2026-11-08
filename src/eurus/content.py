from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

BlockType = Literal[
    "text",
    "image",
    "audio",
    "video",
    "video_frame",
    "document",
    "artifact",
]

BLOCK_TYPES: tuple[str, ...] = (
    "text",
    "image",
    "audio",
    "video",
    "video_frame",
    "document",
    "artifact",
)

MAX_BLOCK_COUNT = 64
MAX_TEXT_CHARS = 100_000
MAX_METADATA_CHARS = 500
MAX_FILENAME_CHARS = 120
MAX_DIMENSION = 100_000
MAX_DURATION_SECONDS = 2_592_000
MAX_PAGE_COUNT = 100_000
MAX_TIMESTAMP_SECONDS = 2_592_000

ASSET_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_MIME_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,127}"
_MIME_TYPE_PATTERN = re.compile(rf"^{_MIME_TOKEN}/{_MIME_TOKEN}$")


class ContentBlockError(ValueError):
    pass


def _string(value: Any, field_name: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ContentBlockError(f"{field_name} must be a string")
    if len(value) > max_chars:
        raise ContentBlockError(f"{field_name} exceeds {max_chars} characters")
    return value


def _bounded_string(value: Any, field_name: str) -> str:
    return _string(value, field_name, max_chars=MAX_METADATA_CHARS)


def _asset_id(value: Any, field_name: str = "asset_id") -> str:
    if not isinstance(value, str) or not ASSET_ID_PATTERN.match(value):
        raise ContentBlockError(f"{field_name} must be a 32-character lowercase hex reference")
    return value


def _optional_asset_id(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _asset_id(value, field_name)


def _mime_type(value: Any) -> str:
    if not isinstance(value, str) or not _MIME_TYPE_PATTERN.match(value):
        raise ContentBlockError("mime_type must look like 'type/subtype'")
    return value


def _optional_dimension(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentBlockError(f"{field_name} must be an integer")
    if not 1 <= value <= MAX_DIMENSION:
        raise ContentBlockError(f"{field_name} must be between 1 and {MAX_DIMENSION}")
    return value


def _optional_seconds(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContentBlockError(f"{field_name} must be a number")
    seconds = float(value)
    if seconds < 0.0 or seconds > MAX_DURATION_SECONDS:
        raise ContentBlockError(f"{field_name} must be between 0 and {MAX_DURATION_SECONDS}")
    return seconds


def _optional_page_count(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentBlockError("page_count must be an integer")
    if not 1 <= value <= MAX_PAGE_COUNT:
        raise ContentBlockError(f"page_count must be between 1 and {MAX_PAGE_COUNT}")
    return value


def _require_field(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise ContentBlockError(f"missing block field: {key}")
    return data[key]


def _reject_unknown_fields(data: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if not isinstance(data, Mapping):
        raise ContentBlockError("block payload must be an object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ContentBlockError(f"unexpected block fields: {', '.join(unknown)}")


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str

    @property
    def type(self) -> BlockType:
        return "text"

    def __post_init__(self) -> None:
        _string(self.text, "text", max_chars=MAX_TEXT_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TextBlock:
        _reject_unknown_fields(data, frozenset({"type", "text"}))
        return cls(text=_string(_require_field(data, "text"), "text", max_chars=MAX_TEXT_CHARS))


@dataclass(frozen=True, slots=True)
class ImageBlock:
    asset_id: str
    mime_type: str
    alt_text: str = ""
    width: int | None = None
    height: int | None = None

    @property
    def type(self) -> BlockType:
        return "image"

    def __post_init__(self) -> None:
        _asset_id(self.asset_id)
        _mime_type(self.mime_type)
        _bounded_string(self.alt_text, "alt_text")
        _optional_dimension(self.width, "width")
        _optional_dimension(self.height, "height")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "image", "asset_id": self.asset_id}
        data["mime_type"] = self.mime_type
        if self.alt_text:
            data["alt_text"] = self.alt_text
        if self.width is not None:
            data["width"] = self.width
        if self.height is not None:
            data["height"] = self.height
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ImageBlock:
        _reject_unknown_fields(
            data, frozenset({"type", "asset_id", "mime_type", "alt_text", "width", "height"})
        )
        return cls(
            asset_id=_asset_id(_require_field(data, "asset_id")),
            mime_type=_mime_type(_require_field(data, "mime_type")),
            alt_text=_bounded_string(data.get("alt_text", ""), "alt_text"),
            width=_optional_dimension(data.get("width"), "width"),
            height=_optional_dimension(data.get("height"), "height"),
        )


@dataclass(frozen=True, slots=True)
class AudioBlock:
    asset_id: str
    mime_type: str
    duration_seconds: float | None = None
    transcript: str = ""

    @property
    def type(self) -> BlockType:
        return "audio"

    def __post_init__(self) -> None:
        _asset_id(self.asset_id)
        _mime_type(self.mime_type)
        _optional_seconds(self.duration_seconds, "duration_seconds")
        _string(self.transcript, "transcript", max_chars=MAX_TEXT_CHARS)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "audio", "asset_id": self.asset_id}
        data["mime_type"] = self.mime_type
        if self.duration_seconds is not None:
            data["duration_seconds"] = self.duration_seconds
        if self.transcript:
            data["transcript"] = self.transcript
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AudioBlock:
        _reject_unknown_fields(
            data,
            frozenset({"type", "asset_id", "mime_type", "duration_seconds", "transcript"}),
        )
        return cls(
            asset_id=_asset_id(_require_field(data, "asset_id")),
            mime_type=_mime_type(_require_field(data, "mime_type")),
            duration_seconds=_optional_seconds(data.get("duration_seconds"), "duration_seconds"),
            transcript=_string(data.get("transcript", ""), "transcript", max_chars=MAX_TEXT_CHARS),
        )


@dataclass(frozen=True, slots=True)
class VideoBlock:
    asset_id: str
    mime_type: str
    duration_seconds: float | None = None
    thumbnail_asset_id: str | None = None
    transcript: str = ""

    @property
    def type(self) -> BlockType:
        return "video"

    def __post_init__(self) -> None:
        _asset_id(self.asset_id)
        _mime_type(self.mime_type)
        _optional_seconds(self.duration_seconds, "duration_seconds")
        _optional_asset_id(self.thumbnail_asset_id, "thumbnail_asset_id")
        _string(self.transcript, "transcript", max_chars=MAX_TEXT_CHARS)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "video", "asset_id": self.asset_id}
        data["mime_type"] = self.mime_type
        if self.duration_seconds is not None:
            data["duration_seconds"] = self.duration_seconds
        if self.thumbnail_asset_id is not None:
            data["thumbnail_asset_id"] = self.thumbnail_asset_id
        if self.transcript:
            data["transcript"] = self.transcript
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VideoBlock:
        _reject_unknown_fields(
            data,
            frozenset(
                {
                    "type",
                    "asset_id",
                    "mime_type",
                    "duration_seconds",
                    "thumbnail_asset_id",
                    "transcript",
                }
            ),
        )
        return cls(
            asset_id=_asset_id(_require_field(data, "asset_id")),
            mime_type=_mime_type(_require_field(data, "mime_type")),
            duration_seconds=_optional_seconds(data.get("duration_seconds"), "duration_seconds"),
            thumbnail_asset_id=_optional_asset_id(
                data.get("thumbnail_asset_id"), "thumbnail_asset_id"
            ),
            transcript=_string(data.get("transcript", ""), "transcript", max_chars=MAX_TEXT_CHARS),
        )


@dataclass(frozen=True, slots=True)
class VideoFrameBlock:
    asset_id: str
    source_asset_id: str
    mime_type: str
    timestamp_seconds: float

    @property
    def type(self) -> BlockType:
        return "video_frame"

    def __post_init__(self) -> None:
        _asset_id(self.asset_id)
        _asset_id(self.source_asset_id, "source_asset_id")
        _mime_type(self.mime_type)
        if not 0.0 <= self.timestamp_seconds <= MAX_TIMESTAMP_SECONDS:
            raise ContentBlockError(
                f"timestamp_seconds must be between 0 and {MAX_TIMESTAMP_SECONDS}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "video_frame",
            "asset_id": self.asset_id,
            "source_asset_id": self.source_asset_id,
            "mime_type": self.mime_type,
            "timestamp_seconds": self.timestamp_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VideoFrameBlock:
        _reject_unknown_fields(
            data,
            frozenset({"type", "asset_id", "source_asset_id", "mime_type", "timestamp_seconds"}),
        )
        return cls(
            asset_id=_asset_id(_require_field(data, "asset_id")),
            source_asset_id=_asset_id(_require_field(data, "source_asset_id"), "source_asset_id"),
            mime_type=_mime_type(_require_field(data, "mime_type")),
            timestamp_seconds=float(_require_field(data, "timestamp_seconds")),
        )


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    asset_id: str
    mime_type: str
    filename: str = ""
    page_count: int | None = None

    @property
    def type(self) -> BlockType:
        return "document"

    def __post_init__(self) -> None:
        _asset_id(self.asset_id)
        _mime_type(self.mime_type)
        _string(self.filename, "filename", max_chars=MAX_FILENAME_CHARS)
        _optional_page_count(self.page_count)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "document", "asset_id": self.asset_id}
        data["mime_type"] = self.mime_type
        if self.filename:
            data["filename"] = self.filename
        if self.page_count is not None:
            data["page_count"] = self.page_count
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DocumentBlock:
        _reject_unknown_fields(
            data,
            frozenset({"type", "asset_id", "mime_type", "filename", "page_count"}),
        )
        return cls(
            asset_id=_asset_id(_require_field(data, "asset_id")),
            mime_type=_mime_type(_require_field(data, "mime_type")),
            filename=_string(data.get("filename", ""), "filename", max_chars=MAX_FILENAME_CHARS),
            page_count=_optional_page_count(data.get("page_count")),
        )


@dataclass(frozen=True, slots=True)
class ArtifactBlock:
    artifact_id: str
    artifact_type: str = ""
    media_type: str = ""
    name: str = ""

    @property
    def type(self) -> BlockType:
        return "artifact"

    def __post_init__(self) -> None:
        _asset_id(self.artifact_id, "artifact_id")
        _bounded_string(self.artifact_type, "artifact_type")
        _bounded_string(self.media_type, "media_type")
        _bounded_string(self.name, "name")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "artifact", "artifact_id": self.artifact_id}
        if self.artifact_type:
            data["artifact_type"] = self.artifact_type
        if self.media_type:
            data["media_type"] = self.media_type
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArtifactBlock:
        _reject_unknown_fields(
            data,
            frozenset({"type", "artifact_id", "artifact_type", "media_type", "name"}),
        )
        return cls(
            artifact_id=_asset_id(_require_field(data, "artifact_id"), "artifact_id"),
            artifact_type=_bounded_string(data.get("artifact_type", ""), "artifact_type"),
            media_type=_bounded_string(data.get("media_type", ""), "media_type"),
            name=_bounded_string(data.get("name", ""), "name"),
        )


ContentBlock = (
    TextBlock
    | ImageBlock
    | AudioBlock
    | VideoBlock
    | VideoFrameBlock
    | DocumentBlock
    | ArtifactBlock
)


def block_from_dict(data: Any) -> ContentBlock:
    if not isinstance(data, Mapping):
        raise ContentBlockError("block payload must be an object")
    block_type = data.get("type")
    if block_type == "text":
        return TextBlock.from_dict(data)
    if block_type == "image":
        return ImageBlock.from_dict(data)
    if block_type == "audio":
        return AudioBlock.from_dict(data)
    if block_type == "video":
        return VideoBlock.from_dict(data)
    if block_type == "video_frame":
        return VideoFrameBlock.from_dict(data)
    if block_type == "document":
        return DocumentBlock.from_dict(data)
    if block_type == "artifact":
        return ArtifactBlock.from_dict(data)
    raise ContentBlockError(f"unknown block type: {block_type!r}")


def blocks_from_dicts(items: Any) -> tuple[ContentBlock, ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ContentBlockError("blocks must be a sequence of block objects")
    if len(items) > MAX_BLOCK_COUNT:
        raise ContentBlockError(f"messages may carry at most {MAX_BLOCK_COUNT} blocks")
    return tuple(block_from_dict(item) for item in items)


def blocks_to_dicts(blocks: Sequence[ContentBlock]) -> list[dict[str, Any]]:
    return [block.to_dict() for block in blocks]


def text_projection(blocks: Sequence[ContentBlock]) -> str:
    parts = [block.text for block in blocks if isinstance(block, TextBlock)]
    return "\n".join(parts)


def referenced_asset_ids(blocks: Sequence[ContentBlock]) -> tuple[str, ...]:
    ids: list[str] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            continue
        if isinstance(block, ArtifactBlock):
            ids.append(block.artifact_id)
            continue
        ids.append(block.asset_id)
        if isinstance(block, VideoBlock) and block.thumbnail_asset_id is not None:
            ids.append(block.thumbnail_asset_id)
        if isinstance(block, VideoFrameBlock):
            ids.append(block.source_asset_id)
    ordered: list[str] = []
    seen: set[str] = set()
    for asset_id in ids:
        if asset_id not in seen:
            seen.add(asset_id)
            ordered.append(asset_id)
    return tuple(ordered)
