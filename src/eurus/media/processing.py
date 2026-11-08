from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.events.multimodal import MultimodalEventPublisher, safe_publish
from eurus.events.types import (
    MEDIA_PROCESSING_FAILED,
    MEDIA_PROCESSING_STARTED,
)
from eurus.media.errors import MediaError, MediaErrorCode, MediaProcessingError
from eurus.media.limits import MediaLimits
from eurus.media.store import MediaFile, MediaStore

log = logging.getLogger(__name__)

FFPROBE_BINARY = "ffprobe"

STATUS_READY = "ready"
STATUS_FAILED = "failed"

_RETRYABLE_CODES = frozenset(
    {MediaErrorCode.PROCESSING_TIMEOUT.value, MediaErrorCode.PROCESSING_FAILED.value}
)


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    width: int
    height: int
    image_format: str


@dataclass(frozen=True, slots=True)
class AudioMetadata:
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    has_audio: bool = False
    codec: str = ""


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    page_count: int | None = None
    title: str = ""
    author: str = ""
    sheet_count: int | None = None
    slide_count: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    media_id: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    attempts: int = 0


def ffprobe_available() -> bool:
    return shutil.which(FFPROBE_BINARY) is not None


def extract_image_metadata(data: bytes) -> ImageMetadata | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return ImageMetadata(
                width=int(image.width),
                height=int(image.height),
                image_format=str(image.format or ""),
            )
    except Exception:
        return None


def _run_ffprobe(path: Path, *, timeout: float) -> dict[str, Any] | None:
    binary = shutil.which(FFPROBE_BINARY)
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [
                binary,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _ffprobe_metadata(data: bytes, *, timeout: float) -> dict[str, Any] | None:
    if shutil.which(FFPROBE_BINARY) is None:
        return None
    with tempfile.TemporaryDirectory(prefix="eurus-media-") as temp_dir:
        probe_path = Path(temp_dir) / "probe.bin"
        probe_path.write_bytes(data)
        return _run_ffprobe(probe_path, timeout=timeout)


def extract_audio_metadata(data: bytes, *, timeout: float = 30.0) -> AudioMetadata | None:
    probed = _ffprobe_metadata(data, timeout=timeout)
    if probed is None:
        return None
    streams = [
        stream for stream in probed.get("streams", []) if stream.get("codec_type") == "audio"
    ]
    if not streams:
        return None
    stream = streams[0]
    duration_raw = probed.get("format", {}).get("duration") or stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    sample_rate_raw = stream.get("sample_rate")
    try:
        sample_rate = int(sample_rate_raw) if sample_rate_raw else None
    except (TypeError, ValueError):
        sample_rate = None
    channels_raw = stream.get("channels")
    try:
        channels = int(channels_raw) if channels_raw else None
    except (TypeError, ValueError):
        channels = None
    return AudioMetadata(duration_seconds=duration, sample_rate=sample_rate, channels=channels)


def _parse_frame_rate(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw.strip() or raw in {"0/0", "N/A"}:
        return None
    if "/" in raw:
        numerator_text, _, denominator_text = raw.partition("/")
        try:
            numerator = float(numerator_text)
            denominator = float(denominator_text)
        except ValueError:
            return None
        if denominator <= 0:
            return None
        rate = numerator / denominator
    else:
        try:
            rate = float(raw)
        except ValueError:
            return None
    return rate if rate > 0 else None


def extract_video_metadata(data: bytes, *, timeout: float = 30.0) -> VideoMetadata | None:
    probed = _ffprobe_metadata(data, timeout=timeout)
    if probed is None:
        return None
    streams = [
        stream for stream in probed.get("streams", []) if stream.get("codec_type") == "video"
    ]
    if not streams:
        return None
    stream = streams[0]
    duration_raw = probed.get("format", {}).get("duration") or stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    width_raw = stream.get("width")
    height_raw = stream.get("height")
    try:
        width = int(width_raw) if width_raw else None
        height = int(height_raw) if height_raw else None
    except (TypeError, ValueError):
        width = None
        height = None
    has_audio = any(
        candidate.get("codec_type") == "audio" for candidate in probed.get("streams", [])
    )
    return VideoMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        frame_rate=_parse_frame_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
        has_audio=has_audio,
        codec=str(stream.get("codec_name") or ""),
    )


_PDF_PAGE_PATTERN = re.compile(rb"/Type\s*/Page(?!s)")


def _pdf_page_count(data: bytes) -> int | None:
    matches = _PDF_PAGE_PATTERN.findall(data)
    if matches:
        return len(matches)
    return None


def extract_document_metadata(data: bytes, mime: str) -> DocumentMetadata | None:
    if mime == "application/pdf":
        page_count = _pdf_page_count(data)
        if page_count is None:
            return DocumentMetadata()
        return DocumentMetadata(page_count=page_count)
    if mime.endswith("wordprocessingml.document"):
        return _docx_metadata(data)
    if mime.endswith("spreadsheetml.sheet"):
        return _xlsx_metadata(data)
    if mime.endswith("presentationml.presentation"):
        return _pptx_metadata(data)
    return None


def _docx_metadata(data: bytes) -> DocumentMetadata | None:
    try:
        from docx import Document
    except ImportError:
        return None
    try:
        document = Document(io.BytesIO(data))
        properties = document.core_properties
        return DocumentMetadata(
            title=str(properties.title or ""),
            author=str(properties.author or ""),
        )
    except Exception:
        return None


def _xlsx_metadata(data: bytes) -> DocumentMetadata | None:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True)
        count = len(workbook.sheetnames)
        workbook.close()
        return DocumentMetadata(sheet_count=count)
    except Exception:
        return None


def _pptx_metadata(data: bytes) -> DocumentMetadata | None:
    try:
        from pptx import Presentation
    except ImportError:
        return None
    try:
        presentation = Presentation(io.BytesIO(data))
        return DocumentMetadata(slide_count=len(presentation.slides))
    except Exception:
        return None


class MediaProcessor:
    def __init__(self, *, limits: MediaLimits | None = None) -> None:
        self.limits = limits if limits is not None else MediaLimits.from_environ()
        self._governance_slot = asyncio.Semaphore(self.limits.max_concurrent_processing)

    async def process(self, media: MediaFile, *, data: bytes | None = None) -> ProcessingOutcome:
        try:
            payload = data if data is not None else await self._read_payload(media)
        except MediaProcessingError as exc:
            return ProcessingOutcome(
                media_id=media.id,
                status=STATUS_FAILED,
                error_code=exc.code,
                attempts=1,
            )
        attempts = 0
        last_error = ""
        while attempts <= self.limits.max_processing_retries:
            attempts += 1
            try:
                metadata = await self._extract(media, payload)
                return ProcessingOutcome(
                    media_id=media.id,
                    status=STATUS_READY,
                    metadata=metadata,
                    attempts=attempts,
                )
            except TimeoutError:
                last_error = MediaErrorCode.PROCESSING_TIMEOUT.value
                if attempts > self.limits.max_processing_retries:
                    break
            except MediaProcessingError as exc:
                last_error = exc.code
                if not exc.retryable or attempts > self.limits.max_processing_retries:
                    break
            except MediaError as exc:
                last_error = exc.code or MediaErrorCode.PROCESSING_FAILED.value
                break
            except Exception:
                log.warning(
                    "media extraction crashed media_id=%s kind=%s",
                    media.id,
                    media.kind,
                    exc_info=True,
                )
                last_error = MediaErrorCode.CORRUPT_MEDIA.value
                break
        return ProcessingOutcome(
            media_id=media.id,
            status=STATUS_FAILED,
            error_code=last_error or MediaErrorCode.PROCESSING_FAILED.value,
            attempts=attempts,
        )

    async def _read_payload(self, media: MediaFile) -> bytes:
        path = Path(media.storage_path)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise MediaProcessingError(
                f"media payload unreadable: {exc}",
                code=MediaErrorCode.CORRUPT_MEDIA.value,
            ) from exc

    async def _extract(self, media: MediaFile, data: bytes) -> dict[str, Any]:
        self.limits.validate_size(len(data))
        if media.kind == "image":
            return await self._with_governance(self._image, media, data)
        if media.kind == "audio":
            return await self._with_governance(self._audio, media, data)
        if media.kind == "video":
            return await self._with_governance(self._video, media, data)
        if media.kind == "document":
            return await self._with_governance(self._document, media, data)
        return {}

    async def _with_governance(
        self,
        extract: Callable[[MediaFile, bytes], dict[str, Any]],
        media: MediaFile,
        data: bytes,
    ) -> dict[str, Any]:
        async with self._governance_slot:
            return await asyncio.wait_for(
                asyncio.to_thread(extract, media, data),
                timeout=self.limits.processing_timeout_seconds,
            )

    async def run_governed(self, work: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        async with self._governance_slot:
            return await asyncio.wait_for(
                asyncio.to_thread(work, *args, **kwargs),
                timeout=self.limits.processing_timeout_seconds,
            )

    def _image(self, media: MediaFile, data: bytes) -> dict[str, Any]:
        metadata = extract_image_metadata(data)
        if metadata is None:
            return {"decoder": "unavailable"}
        self.limits.validate_dimension(metadata.width, metadata.height)
        self.limits.validate_pixels(metadata.width, metadata.height)
        return {
            "width": metadata.width,
            "height": metadata.height,
            "image_format": metadata.image_format,
        }

    def _audio(self, media: MediaFile, data: bytes) -> dict[str, Any]:
        metadata = extract_audio_metadata(data, timeout=self.limits.processing_timeout_seconds)
        if metadata is None:
            return {"decoder": "unavailable"}
        result: dict[str, Any] = {}
        if metadata.duration_seconds is not None:
            self.limits.validate_duration(metadata.duration_seconds)
            result["duration_seconds"] = metadata.duration_seconds
        if metadata.sample_rate is not None:
            result["sample_rate"] = metadata.sample_rate
        if metadata.channels is not None:
            result["channels"] = metadata.channels
        return result

    def _video(self, media: MediaFile, data: bytes) -> dict[str, Any]:
        metadata = extract_video_metadata(data, timeout=self.limits.processing_timeout_seconds)
        if metadata is None:
            return {"decoder": "unavailable"}
        result: dict[str, Any] = {}
        if metadata.duration_seconds is not None:
            self.limits.validate_duration(metadata.duration_seconds)
            result["duration_seconds"] = metadata.duration_seconds
        if metadata.width is not None and metadata.height is not None:
            self.limits.validate_dimension(metadata.width, metadata.height)
            result["width"] = metadata.width
            result["height"] = metadata.height
        if metadata.frame_rate is not None:
            result["frame_rate"] = metadata.frame_rate
        if metadata.codec:
            result["codec"] = metadata.codec
        result["has_audio"] = metadata.has_audio
        return result

    def _document(self, media: MediaFile, data: bytes) -> dict[str, Any]:
        metadata = extract_document_metadata(data, media.mime)
        if metadata is None:
            return {}
        result: dict[str, Any] = {}
        if metadata.page_count is not None:
            result["page_count"] = metadata.page_count
        if metadata.title:
            result["title"] = metadata.title
        if metadata.author:
            result["author"] = metadata.author
        if metadata.sheet_count is not None:
            result["sheet_count"] = metadata.sheet_count
        if metadata.slide_count is not None:
            result["slide_count"] = metadata.slide_count
        return result


async def run_processing_pipeline(
    store: MediaStore,
    media: MediaFile,
    processor: MediaProcessor,
    *,
    data: bytes | None = None,
    events: MultimodalEventPublisher | None = None,
) -> ProcessingOutcome:
    await _publish_pipeline_event(events, MEDIA_PROCESSING_STARTED, media, {"stage": "metadata"})
    outcome = await processor.process(media, data=data)
    if outcome.status != STATUS_READY:
        store.mark_failed(media.id, outcome.error_code)
        await _publish_pipeline_event(
            events,
            MEDIA_PROCESSING_FAILED,
            media,
            {"stage": "metadata", "error_code": outcome.error_code, "attempt": outcome.attempts},
        )
        return outcome
    metadata = dict(outcome.metadata)
    if media.kind == "video":
        metadata = await _enrich_video(store, media, processor, metadata, data=data, events=events)
    store.mark_ready(media.id, metadata)
    if media.kind == "video":
        from eurus.media.video import publish_analysis_completed

        await publish_analysis_completed(events, media, metadata)
    return ProcessingOutcome(
        media_id=media.id,
        status=STATUS_READY,
        metadata=metadata,
        attempts=outcome.attempts,
    )


async def _enrich_video(
    store: MediaStore,
    media: MediaFile,
    processor: MediaProcessor,
    metadata: dict[str, Any],
    *,
    data: bytes | None,
    events: MultimodalEventPublisher | None,
) -> dict[str, Any]:
    from eurus.media.video import enrich_video_media

    merged = dict(metadata)
    try:
        extra = await enrich_video_media(
            store,
            media,
            processor,
            data=data,
            base_metadata=metadata,
            events=events,
        )
    except TimeoutError:
        log.warning("video enrichment timed out media_id=%s", media.id)
        return merged
    except MediaError as exc:
        log.warning(
            "video enrichment rejected media_id=%s code=%s", media.id, exc.code or "unknown"
        )
        return merged
    except Exception:
        log.warning("video enrichment failed media_id=%s", media.id, exc_info=True)
        return merged
    merged.update(extra)
    return merged


async def _publish_pipeline_event(
    events: MultimodalEventPublisher | None,
    event_type: str,
    media: MediaFile,
    payload: dict[str, Any],
) -> None:
    if events is None:
        return
    enriched = dict(payload)
    enriched["media_id"] = media.id
    if media.session_id:
        enriched.setdefault("session_id", media.session_id)
    await asyncio.to_thread(
        safe_publish,
        lambda: events.media_event(
            event_type,
            media.id,
            enriched,
            session_id=media.session_id,
        ),
        context=f"media-pipeline:{media.id}:{event_type}",
    )
