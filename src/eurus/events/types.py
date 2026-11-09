from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
MEDIA_RECEIVED = "MEDIA_RECEIVED"
IMAGE_RECEIVED = "IMAGE_RECEIVED"
AUDIO_RECEIVED = "AUDIO_RECEIVED"
VIDEO_RECEIVED = "VIDEO_RECEIVED"
DOCUMENT_RECEIVED = "DOCUMENT_RECEIVED"
MEDIA_VALIDATION_STARTED = "MEDIA_VALIDATION_STARTED"
MEDIA_VALIDATION_COMPLETED = "MEDIA_VALIDATION_COMPLETED"
MEDIA_PROCESSING_STARTED = "MEDIA_PROCESSING_STARTED"
MEDIA_TRANSCRIPTION_STARTED = "MEDIA_TRANSCRIPTION_STARTED"
MEDIA_TRANSCRIPTION_DELTA = "MEDIA_TRANSCRIPTION_DELTA"
MEDIA_TRANSCRIPTION_COMPLETED = "MEDIA_TRANSCRIPTION_COMPLETED"
VIDEO_FRAME_EXTRACTED = "VIDEO_FRAME_EXTRACTED"
MEDIA_ANALYSIS_COMPLETED = "MEDIA_ANALYSIS_COMPLETED"
MEDIA_PROCESSING_FAILED = "MEDIA_PROCESSING_FAILED"

INBOUND_EVENT_TYPES: tuple[str, ...] = (
    MESSAGE_RECEIVED,
    MEDIA_RECEIVED,
    IMAGE_RECEIVED,
    AUDIO_RECEIVED,
    VIDEO_RECEIVED,
    DOCUMENT_RECEIVED,
)

MEDIA_PROCESSING_EVENT_TYPES: tuple[str, ...] = (
    MEDIA_VALIDATION_STARTED,
    MEDIA_VALIDATION_COMPLETED,
    MEDIA_PROCESSING_STARTED,
    MEDIA_TRANSCRIPTION_STARTED,
    MEDIA_TRANSCRIPTION_DELTA,
    MEDIA_TRANSCRIPTION_COMPLETED,
    VIDEO_FRAME_EXTRACTED,
    MEDIA_ANALYSIS_COMPLETED,
    MEDIA_PROCESSING_FAILED,
)

MULTIMODAL_EVENT_TYPES: tuple[str, ...] = INBOUND_EVENT_TYPES + MEDIA_PROCESSING_EVENT_TYPES

DEFAULT_SCHEMA_VERSION = 1
SCHEMA_VERSIONS: dict[str, int] = {event_type: 1 for event_type in MULTIMODAL_EVENT_TYPES}

MEDIA_KINDS: frozenset[str] = frozenset({"image", "audio", "video", "document"})

KIND_EVENT_BY_MEDIA: dict[str, str] = {
    "image": IMAGE_RECEIVED,
    "audio": AUDIO_RECEIVED,
    "video": VIDEO_RECEIVED,
    "document": DOCUMENT_RECEIVED,
}

MEDIA_SUBJECT_TYPE = "media"
MESSAGE_SUBJECT_TYPE = "message"

MEDIA_STAGES: frozenset[str] = frozenset(
    {
        "ingestion",
        "signature",
        "mime",
        "metadata",
        "thumbnail",
        "transcription",
        "frame_extraction",
        "analysis",
        "conversion",
    }
)

MAX_PAYLOAD_TEXT_CHARS = 4000
MAX_MEDIA_IDS_PER_MESSAGE = 8

_ASSET_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_EVENT_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class EventPayloadError(ValueError):
    pass


def schema_version_for(event_type: str) -> int:
    return SCHEMA_VERSIONS.get(event_type, DEFAULT_SCHEMA_VERSION)


def is_multimodal_event_type(event_type: str) -> bool:
    return event_type in MULTIMODAL_EVENT_TYPES


def _require_str(payload: Mapping[str, Any], key: str, *, max_chars: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EventPayloadError(f"{key} must be a non-empty string")
    if len(value) > max_chars:
        raise EventPayloadError(f"{key} exceeds {max_chars} characters")
    return value


def _optional_str(payload: Mapping[str, Any], key: str, *, max_chars: int) -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EventPayloadError(f"{key} must be a string")
    if len(value) > max_chars:
        raise EventPayloadError(f"{key} exceeds {max_chars} characters")
    return value


def _require_asset_id(payload: Mapping[str, Any], key: str = "media_id") -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not _ASSET_ID_RE.match(value):
        raise EventPayloadError(f"{key} must be a 32-character lowercase hex reference")
    return value


def _optional_sha256(payload: Mapping[str, Any], key: str = "sha256") -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise EventPayloadError(f"{key} must be a 64-character lowercase hex digest")
    return value


def _require_size(payload: Mapping[str, Any]) -> int:
    value = payload.get("size")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventPayloadError("size must be a non-negative integer")
    return value


def _require_kind(payload: Mapping[str, Any], *, expected: str | None = None) -> str:
    kind = _require_str(payload, "kind", max_chars=32)
    if kind not in MEDIA_KINDS:
        raise EventPayloadError(f"kind must be one of {sorted(MEDIA_KINDS)}")
    if expected is not None and kind != expected:
        raise EventPayloadError(f"kind must be {expected!r} for this event type")
    return kind


def _optional_stage(payload: Mapping[str, Any]) -> str:
    stage = _optional_str(payload, "stage", max_chars=64)
    if stage and stage not in MEDIA_STAGES:
        raise EventPayloadError(f"stage must be one of {sorted(MEDIA_STAGES)}")
    return stage


def _require_stage(payload: Mapping[str, Any]) -> str:
    stage = _require_str(payload, "stage", max_chars=64)
    if stage not in MEDIA_STAGES:
        raise EventPayloadError(f"stage must be one of {sorted(MEDIA_STAGES)}")
    return stage


def _optional_seconds(payload: Mapping[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventPayloadError(f"{key} must be a number")
    seconds = float(value)
    if seconds < 0.0 or seconds > 2_592_000.0:
        raise EventPayloadError(f"{key} must be between 0 and 2592000 seconds")
    return seconds


def _require_media_ids(payload: Mapping[str, Any]) -> list[str]:
    value = payload.get("media_ids", [])
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise EventPayloadError("media_ids must be a list of asset references")
    if len(value) > MAX_MEDIA_IDS_PER_MESSAGE:
        raise EventPayloadError(f"media_ids may list at most {MAX_MEDIA_IDS_PER_MESSAGE} assets")
    checked: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _ASSET_ID_RE.match(item):
            raise EventPayloadError("media_ids entries must be 32-character hex references")
        checked.append(item)
    return checked


def _validate_common_media_fields(
    payload: Mapping[str, Any], *, expected_kind: str | None
) -> dict[str, Any]:
    validated: dict[str, Any] = {
        "media_id": _require_asset_id(payload),
        "session_id": _require_str(payload, "session_id", max_chars=200),
        "kind": _require_kind(payload, expected=expected_kind),
        "mime": _require_str(payload, "mime", max_chars=127),
        "size": _require_size(payload),
        "sha256": _optional_sha256(payload),
        "filename": _optional_str(payload, "filename", max_chars=120),
        "source": _optional_str(payload, "source", max_chars=32),
    }
    return validated


PayloadValidator = Callable[[Mapping[str, Any]], dict[str, Any]]

_PAYLOAD_VALIDATORS: dict[str, PayloadValidator] = {
    MESSAGE_RECEIVED: lambda payload: {
        "session_id": _require_str(payload, "session_id", max_chars=200),
        "message_id": _optional_str(payload, "message_id", max_chars=128),
        "text": _optional_str(payload, "text", max_chars=MAX_PAYLOAD_TEXT_CHARS),
        "media_ids": _require_media_ids(payload),
        "source": _optional_str(payload, "source", max_chars=32),
    },
    MEDIA_RECEIVED: lambda payload: _validate_common_media_fields(payload, expected_kind=None),
    IMAGE_RECEIVED: lambda payload: _validate_common_media_fields(payload, expected_kind="image"),
    AUDIO_RECEIVED: lambda payload: _validate_common_media_fields(payload, expected_kind="audio"),
    VIDEO_RECEIVED: lambda payload: _validate_common_media_fields(payload, expected_kind="video"),
    DOCUMENT_RECEIVED: lambda payload: _validate_common_media_fields(
        payload, expected_kind="document"
    ),
    MEDIA_VALIDATION_STARTED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "checks": _optional_str(payload, "checks", max_chars=200),
    },
    MEDIA_VALIDATION_COMPLETED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "valid": bool(payload.get("valid", True)),
        "reason": _optional_str(payload, "reason", max_chars=500),
    },
    MEDIA_PROCESSING_STARTED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "stage": _require_stage(payload),
        "attempt": payload.get("attempt", 1),
    },
    MEDIA_TRANSCRIPTION_STARTED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "language": _optional_str(payload, "language", max_chars=32),
    },
    MEDIA_TRANSCRIPTION_DELTA: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "text": _require_str(payload, "text", max_chars=MAX_PAYLOAD_TEXT_CHARS),
        "sequence": payload.get("sequence", 0),
    },
    MEDIA_TRANSCRIPTION_COMPLETED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "text": _optional_str(payload, "text", max_chars=MAX_PAYLOAD_TEXT_CHARS),
        "language": _optional_str(payload, "language", max_chars=32),
        "duration_seconds": _optional_seconds(payload, "duration_seconds"),
    },
    VIDEO_FRAME_EXTRACTED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "frame_media_id": _require_asset_id(payload, "frame_media_id"),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "timestamp_seconds": _optional_seconds(payload, "timestamp_seconds"),
    },
    MEDIA_ANALYSIS_COMPLETED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "summary": _optional_str(payload, "summary", max_chars=MAX_PAYLOAD_TEXT_CHARS),
    },
    MEDIA_PROCESSING_FAILED: lambda payload: {
        "media_id": _require_asset_id(payload),
        "session_id": _optional_str(payload, "session_id", max_chars=200),
        "stage": _require_stage(payload),
        "error_code": _require_str(payload, "error_code", max_chars=64),
        "detail": _optional_str(payload, "detail", max_chars=500),
        "attempt": payload.get("attempt", 1),
    },
}


def validate_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.match(event_type or ""):
        raise EventPayloadError("event_type must be an uppercase snake-case identifier")
    validator = _PAYLOAD_VALIDATORS.get(event_type)
    if validator is None:
        raise EventPayloadError(f"no multimodal payload contract for event type {event_type}")
    if not isinstance(payload, Mapping):
        raise EventPayloadError("payload must be a mapping")
    _reject_binary_values(payload)
    return validator(payload)


def _reject_binary_values(payload: Mapping[str, Any]) -> None:
    for key, value in payload.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise EventPayloadError(f"payload field {key} must not contain raw binary data")
        if isinstance(value, Mapping):
            _reject_binary_values(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (bytes, bytearray, memoryview)):
                    raise EventPayloadError(f"payload field {key} must not contain binary items")


def media_received_event_type(kind: str) -> str:
    event_type = KIND_EVENT_BY_MEDIA.get(kind)
    if event_type is None:
        raise EventPayloadError(f"unknown media kind: {kind!r}")
    return event_type
