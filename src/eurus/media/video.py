from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from eurus.events.multimodal import MultimodalEventPublisher, safe_publish
from eurus.events.types import (
    MEDIA_ANALYSIS_COMPLETED,
    VIDEO_FRAME_EXTRACTED,
)
from eurus.media.limits import MediaLimits
from eurus.media.store import MediaFile, MediaStore

log = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"

THUMBNAIL_SEEK_FRACTION = 0.1
FRAME_JPEG_QUALITY = "4"
SCENE_DETECTION_MAX_DURATION_SECONDS = 900.0
AUDIO_SAMPLE_RATE = 16_000
AUDIO_BYTES_PER_SECOND = AUDIO_SAMPLE_RATE * 2
MAX_AUDIO_TRACK_BYTES = 19 * 1024 * 1024
AUDIO_WAV_HEADER_MARGIN_BYTES = 4096
AUDIO_MAX_SECONDS = (MAX_AUDIO_TRACK_BYTES - AUDIO_WAV_HEADER_MARGIN_BYTES) / AUDIO_BYTES_PER_SECOND
AUDIO_MIME = "audio/wav"
FRAME_MIME = "image/jpeg"
MAX_FRAME_FILENAME_SECONDS = 100_000.0

_PTS_TIME_PATTERN = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG_BINARY) is not None


def _ffmpeg_binary() -> str | None:
    return shutil.which(FFMPEG_BINARY)


def _run_ffmpeg(
    arguments: list[str], *, timeout: float
) -> subprocess.CompletedProcess[bytes] | None:
    binary = _ffmpeg_binary()
    if binary is None:
        return None
    try:
        return subprocess.run(
            [binary, "-hide_banner", "-loglevel", "error", *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def frame_filename(source: str, timestamp_seconds: float) -> str:
    stamp = min(max(timestamp_seconds, 0.0), MAX_FRAME_FILENAME_SECONDS)
    base = source.rsplit(".", 1)[0] if "." in source else source
    return f"{base or 'video'}-frame-{stamp:.1f}s.jpg"


def scaled_dimensions(
    width: int | None, height: int | None, max_dimension: int
) -> tuple[int, int] | None:
    if not width or not height or width <= 0 or height <= 0:
        return None
    if max(width, height) <= max_dimension:
        return None
    ratio = max_dimension / max(width, height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))


def sample_frame_timestamps(
    duration_seconds: float,
    *,
    interval_seconds: float,
    max_frames: int,
    scene_timestamps: tuple[float, ...] | list[float] = (),
) -> list[float]:
    if max_frames < 1 or duration_seconds <= 0:
        return []
    duration = float(duration_seconds)
    interval = max(float(interval_seconds), duration / float(max_frames))
    separation = max(interval / 2.0, 0.25)
    scenes = sorted(
        {round(float(stamp), 3) for stamp in scene_timestamps if 0.0 <= float(stamp) < duration}
    )
    scene_budget = max(1, max_frames // 2)
    chosen: list[float] = [0.0]
    for stamp in scenes[:scene_budget]:
        if len(chosen) >= max_frames:
            break
        if all(abs(stamp - taken) >= separation for taken in chosen):
            chosen.append(stamp)
    stamp = interval
    while stamp < duration and len(chosen) < max_frames:
        if all(abs(stamp - taken) >= separation for taken in chosen):
            chosen.append(round(stamp, 3))
        stamp += interval
    return sorted(chosen)[:max_frames]


def detect_scene_changes(data: bytes, *, threshold: float, timeout: float = 30.0) -> list[float]:
    binary = _ffmpeg_binary()
    if binary is None or not data:
        return []
    with tempfile.TemporaryDirectory(prefix="eurus-video-") as temp_dir:
        source = Path(temp_dir) / "source.bin"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [
                    binary,
                    "-hide_banner",
                    "-loglevel",
                    "info",
                    "-i",
                    str(source),
                    "-an",
                    "-vf",
                    f"select='gt(scene,{threshold})',showinfo",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
    if completed.returncode != 0:
        return []
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return sorted({float(match) for match in _PTS_TIME_PATTERN.findall(stderr)})


def _frame_dimensions(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        return 0, 0
    try:
        import io

        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def extract_frame_images(
    data: bytes,
    timestamps: list[float],
    *,
    source_width: int | None = None,
    source_height: int | None = None,
    max_dimension: int = 1024,
    timeout: float = 60.0,
) -> list[tuple[bytes, float, int, int]]:
    if not data or not timestamps:
        return []
    binary = _ffmpeg_binary()
    if binary is None:
        return []
    target = scaled_dimensions(source_width, source_height, max_dimension)
    if target is None:
        scale_filter = f"scale=min(iw\\,{max_dimension}):min(ih\\,{max_dimension})"
    else:
        scale_filter = f"scale={target[0]}:{target[1]}"
    frames: list[tuple[bytes, float, int, int]] = []
    with tempfile.TemporaryDirectory(prefix="eurus-video-") as temp_dir:
        source = Path(temp_dir) / "source.bin"
        source.write_bytes(data)
        for stamp in timestamps:
            output = Path(temp_dir) / f"frame-{len(frames):03d}.jpg"
            completed = _run_ffmpeg(
                [
                    "-ss",
                    f"{max(float(stamp), 0.0):.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    scale_filter,
                    "-q:v",
                    FRAME_JPEG_QUALITY,
                    str(output),
                ],
                timeout=timeout,
            )
            if completed is None or completed.returncode != 0 or not output.exists():
                continue
            frame_data = output.read_bytes()
            if not frame_data:
                continue
            width, height = _frame_dimensions(frame_data)
            frames.append((frame_data, float(stamp), width, height))
            output.unlink(missing_ok=True)
    return frames


def generate_video_thumbnail(
    data: bytes,
    *,
    duration_seconds: float | None = None,
    max_dimension: int = 512,
    timeout: float = 30.0,
) -> bytes | None:
    if not data:
        return None
    seek = 0.0
    if duration_seconds is not None and duration_seconds > 0:
        seek = min(THUMBNAIL_SEEK_FRACTION * duration_seconds, max(duration_seconds - 0.1, 0.0))
    frames = extract_frame_images(
        data,
        [seek],
        max_dimension=max_dimension,
        timeout=timeout,
    )
    if not frames:
        return None
    return frames[0][0]


def extract_audio_track(
    data: bytes,
    *,
    duration_seconds: float | None = None,
    timeout: float = 60.0,
    max_bytes: int = MAX_AUDIO_TRACK_BYTES,
) -> tuple[bytes, bool] | None:
    binary = _ffmpeg_binary()
    if binary is None or not data:
        return None
    max_seconds = (
        (max_bytes - AUDIO_WAV_HEADER_MARGIN_BYTES) / AUDIO_BYTES_PER_SECOND
        if max_bytes > AUDIO_WAV_HEADER_MARGIN_BYTES
        else 0.0
    )
    truncated = bool(duration_seconds is not None and duration_seconds > max_seconds)
    with tempfile.TemporaryDirectory(prefix="eurus-video-") as temp_dir:
        source = Path(temp_dir) / "source.bin"
        source.write_bytes(data)
        output = Path(temp_dir) / "audio.wav"
        arguments = [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
        ]
        if truncated:
            arguments.extend(["-t", f"{max_seconds:.3f}"])
        arguments.append(str(output))
        completed = _run_ffmpeg(arguments, timeout=timeout)
        if completed is None or completed.returncode != 0 or not output.exists():
            return None
        audio = output.read_bytes()
    if not audio or len(audio) > max_bytes:
        return None
    return audio, truncated


def _read_payload(store: MediaStore, media: MediaFile) -> bytes:
    path = store.storage_path_for(media.id)
    if path is None:
        raise FileNotFoundError(f"media payload missing for {media.id}")
    data = path.read_bytes()
    if not data:
        raise ValueError(f"media payload is empty for {media.id}")
    return data


async def enrich_video_media(
    store: MediaStore,
    media: MediaFile,
    processor: Any,
    *,
    data: bytes | None = None,
    base_metadata: dict[str, Any] | None = None,
    events: MultimodalEventPublisher | None = None,
) -> dict[str, Any]:
    if media.kind != "video" or not ffmpeg_available():
        return {}
    base = base_metadata if base_metadata is not None else {}
    extra: dict[str, Any] = {}
    try:
        payload = data if data is not None else await asyncio.to_thread(_read_payload, store, media)
    except (OSError, ValueError, FileNotFoundError):
        log.warning("video payload unreadable media_id=%s", media.id, exc_info=True)
        return {}
    timeout = processor.limits.processing_timeout_seconds
    limits: MediaLimits = processor.limits
    duration = _as_float(base.get("duration_seconds"))
    width = _as_int(base.get("width"))
    height = _as_int(base.get("height"))
    thumbnail = await processor.run_governed(
        generate_video_thumbnail,
        payload,
        duration_seconds=duration,
        max_dimension=limits.max_thumbnail_dimension,
        timeout=timeout,
    )
    if thumbnail is not None:
        thumb_media = await _save_asset(
            store,
            media,
            _audio_like_name(media.filename, "thumb.jpg"),
            thumbnail,
            FRAME_MIME,
        )
        if thumb_media is not None:
            extra["thumbnail_asset_id"] = thumb_media.id
    scene_timestamps: list[float] = []
    if duration is not None and 0 < duration <= SCENE_DETECTION_MAX_DURATION_SECONDS:
        scene_timestamps = await processor.run_governed(
            detect_scene_changes,
            payload,
            threshold=limits.video_scene_threshold,
            timeout=timeout,
        )
    if duration is not None and duration > 0:
        timestamps = sample_frame_timestamps(
            duration,
            interval_seconds=limits.video_frame_interval_seconds,
            max_frames=limits.max_video_frames,
            scene_timestamps=scene_timestamps,
        )
        frames = await processor.run_governed(
            extract_frame_images,
            payload,
            timestamps,
            source_width=width,
            source_height=height,
            max_dimension=limits.video_frame_max_dimension,
            timeout=timeout,
        )
        frame_entries: list[dict[str, Any]] = []
        for frame_data, stamp, frame_width, frame_height in frames:
            frame_media = await _save_asset(
                store,
                media,
                frame_filename(media.filename, stamp),
                frame_data,
                FRAME_MIME,
            )
            if frame_media is None:
                continue
            frame_entries.append(
                {
                    "asset_id": frame_media.id,
                    "timestamp_seconds": stamp,
                    "width": frame_width,
                    "height": frame_height,
                }
            )
            await _publish_frame_event(events, media, frame_media.id, stamp)
        if frame_entries:
            extra["frames"] = frame_entries
    if base.get("has_audio"):
        audio = await processor.run_governed(
            extract_audio_track,
            payload,
            duration_seconds=duration,
            timeout=timeout,
        )
        if audio is not None:
            audio_data, truncated = audio
            audio_media = await _save_asset(
                store,
                media,
                _audio_like_name(media.filename, "audio.wav"),
                audio_data,
                AUDIO_MIME,
            )
            if audio_media is not None:
                extra["audio_asset_id"] = audio_media.id
                if truncated:
                    extra["audio_truncated"] = True
    return extra


async def _save_asset(
    store: MediaStore, media: MediaFile, filename: str, data: bytes, mime: str
) -> MediaFile | None:
    def _save() -> MediaFile | None:
        try:
            return store.save(
                media.session_id,
                filename,
                data,
                declared_mime=mime,
                owner_id=media.owner_id,
            )
        except Exception:
            log.warning("video derived asset storage failed filename=%s", filename, exc_info=True)
            return None

    return await asyncio.to_thread(_save)


async def _publish_frame_event(
    events: MultimodalEventPublisher | None, media: MediaFile, frame_media_id: str, stamp: float
) -> None:
    if events is None:
        return
    payload = {
        "frame_media_id": frame_media_id,
        "timestamp_seconds": stamp,
    }
    await asyncio.to_thread(
        safe_publish,
        lambda: events.media_event(
            VIDEO_FRAME_EXTRACTED,
            media.id,
            payload,
            session_id=media.session_id,
            dedup_key=f"video-frame:{media.id}:{frame_media_id}",
        ),
        context=f"video-frame:{media.id}",
    )


async def publish_analysis_completed(
    events: MultimodalEventPublisher | None, media: MediaFile, metadata: dict[str, Any]
) -> None:
    if events is None:
        return
    summary = video_summary(metadata)
    await asyncio.to_thread(
        safe_publish,
        lambda: events.media_event(
            MEDIA_ANALYSIS_COMPLETED,
            media.id,
            {"summary": summary},
            session_id=media.session_id,
        ),
        context=f"video-analysis:{media.id}",
    )


def video_summary(metadata: dict[str, Any]) -> str:
    if not metadata:
        return ""
    parts: list[str] = []
    duration = _as_float(metadata.get("duration_seconds"))
    if duration is not None:
        parts.append(f"duration {duration:.1f}s")
    if metadata.get("width") and metadata.get("height"):
        parts.append(f"{_as_int(metadata.get('width'))}x{_as_int(metadata.get('height'))}")
    if metadata.get("frame_rate"):
        parts.append(f"{metadata['frame_rate']:.1f} fps")
    frames = metadata.get("frames")
    if isinstance(frames, list) and frames:
        parts.append(f"{len(frames)} keyframes extracted")
    if metadata.get("has_audio"):
        parts.append(
            "audio track extracted" if metadata.get("audio_asset_id") else "audio track present"
        )
    if metadata.get("transcript"):
        parts.append("transcript available")
    return ", ".join(parts)


def _audio_like_name(source: str, suffix: str) -> str:
    base = source.rsplit(".", 1)[0] if "." in source else source
    return f"{base or 'video'}-{suffix}"


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if result >= 0 else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
