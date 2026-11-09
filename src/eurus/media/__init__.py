from __future__ import annotations

from typing import Any

from eurus.media.errors import (
    MediaError,
    MediaErrorCode,
    MediaLimitError,
    MediaOwnershipError,
    MediaProcessingError,
    MediaValidationError,
)
from eurus.media.limits import MediaLimits
from eurus.media.signatures import (
    ALLOWED_MEDIA,
    INLINE_TEXT_MAX_CHARS,
    INLINE_TEXT_MIMES,
    detect_signature,
    sniff_mime,
)
from eurus.media.store import (
    MAX_MEDIA_BYTES,
    MEDIA_STATUSES,
    MediaFile,
    MediaStore,
    allowed_media,
    attachment_content_blocks,
    attachment_marker,
    compose_attachment_block,
    sanitize_filename,
)

__all__ = [
    "ALLOWED_MEDIA",
    "INLINE_TEXT_MAX_CHARS",
    "INLINE_TEXT_MIMES",
    "MAX_MEDIA_BYTES",
    "MEDIA_STATUSES",
    "MediaError",
    "MediaErrorCode",
    "MediaFile",
    "MediaLimitError",
    "MediaLimits",
    "MediaOwnershipError",
    "MediaProcessingError",
    "MediaStore",
    "MediaValidationError",
    "allowed_media",
    "attachment_content_blocks",
    "attachment_marker",
    "compose_attachment_block",
    "detect_signature",
    "sanitize_filename",
    "sniff_mime",
]


def __getattr__(name: str) -> Any:
    if name == "MediaProcessor":
        from eurus.media.processing import MediaProcessor

        return MediaProcessor
    if name == "ProcessingOutcome":
        from eurus.media.processing import ProcessingOutcome

        return ProcessingOutcome
    if name == "run_processing_pipeline":
        from eurus.media.processing import run_processing_pipeline

        return run_processing_pipeline
    if name == "ffprobe_available":
        from eurus.media.processing import ffprobe_available

        return ffprobe_available
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
