from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from eurus.media.errors import MediaLimitError

MAX_BYTES_ENV = "EURUS_MEDIA_MAX_BYTES"
MAX_DURATION_SECONDS_ENV = "EURUS_MEDIA_MAX_DURATION_SECONDS"
MAX_PIXELS_ENV = "EURUS_MEDIA_MAX_PIXELS"
MAX_DIMENSION_ENV = "EURUS_MEDIA_MAX_DIMENSION"
PROCESSING_TIMEOUT_SECONDS_ENV = "EURUS_MEDIA_PROCESSING_TIMEOUT_SECONDS"
MAX_CONCURRENT_PROCESSING_ENV = "EURUS_MEDIA_MAX_CONCURRENT_PROCESSING"
MAX_PROCESSING_RETRIES_ENV = "EURUS_MEDIA_MAX_PROCESSING_RETRIES"
MAX_THUMBNAIL_DIMENSION_ENV = "EURUS_MEDIA_MAX_THUMBNAIL_DIMENSION"
RETENTION_DAYS_ENV = "EURUS_MEDIA_RETENTION_DAYS"
MAX_VIDEO_FRAMES_ENV = "EURUS_MEDIA_MAX_VIDEO_FRAMES"
VIDEO_FRAME_INTERVAL_SECONDS_ENV = "EURUS_VIDEO_FRAME_INTERVAL_SECONDS"
VIDEO_FRAME_MAX_DIMENSION_ENV = "EURUS_VIDEO_FRAME_MAX_DIMENSION"
VIDEO_SCENE_THRESHOLD_ENV = "EURUS_VIDEO_SCENE_THRESHOLD"


def _env_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float_finite(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0.0 < value < 1.0 else default


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_DURATION_SECONDS = 7200.0
DEFAULT_MAX_PIXELS = 100_000_000
DEFAULT_MAX_DIMENSION = 30_000
DEFAULT_PROCESSING_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_CONCURRENT_PROCESSING = 2
DEFAULT_MAX_PROCESSING_RETRIES = 2
DEFAULT_MAX_THUMBNAIL_DIMENSION = 512
DEFAULT_RETENTION_DAYS = 0
DEFAULT_MAX_VIDEO_FRAMES = 8
DEFAULT_VIDEO_FRAME_INTERVAL_SECONDS = 2.0
DEFAULT_VIDEO_FRAME_MAX_DIMENSION = 1024
DEFAULT_VIDEO_SCENE_THRESHOLD = 0.3


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS
    max_pixels: int = DEFAULT_MAX_PIXELS
    max_dimension: int = DEFAULT_MAX_DIMENSION
    processing_timeout_seconds: float = DEFAULT_PROCESSING_TIMEOUT_SECONDS
    max_concurrent_processing: int = DEFAULT_MAX_CONCURRENT_PROCESSING
    max_processing_retries: int = DEFAULT_MAX_PROCESSING_RETRIES
    max_thumbnail_dimension: int = DEFAULT_MAX_THUMBNAIL_DIMENSION
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_video_frames: int = DEFAULT_MAX_VIDEO_FRAMES
    video_frame_interval_seconds: float = DEFAULT_VIDEO_FRAME_INTERVAL_SECONDS
    video_frame_max_dimension: int = DEFAULT_VIDEO_FRAME_MAX_DIMENSION
    video_scene_threshold: float = DEFAULT_VIDEO_SCENE_THRESHOLD

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if self.max_pixels < 1:
            raise ValueError("max_pixels must be positive")
        if self.max_dimension < 1:
            raise ValueError("max_dimension must be positive")
        if self.processing_timeout_seconds <= 0:
            raise ValueError("processing_timeout_seconds must be positive")
        if self.max_concurrent_processing < 1:
            raise ValueError("max_concurrent_processing must be positive")
        if self.max_processing_retries < 0:
            raise ValueError("max_processing_retries cannot be negative")
        if self.max_thumbnail_dimension < 1:
            raise ValueError("max_thumbnail_dimension must be positive")
        if self.retention_days < 0:
            raise ValueError("retention_days cannot be negative")
        if self.max_video_frames < 1:
            raise ValueError("max_video_frames must be positive")
        if self.video_frame_interval_seconds <= 0:
            raise ValueError("video_frame_interval_seconds must be positive")
        if self.video_frame_max_dimension < 1:
            raise ValueError("video_frame_max_dimension must be positive")
        if not 0.0 < self.video_scene_threshold < 1.0:
            raise ValueError("video_scene_threshold must be between 0 and 1")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> MediaLimits:
        env = os.environ if environ is None else environ
        return cls(
            max_bytes=_env_int(env, MAX_BYTES_ENV, DEFAULT_MAX_BYTES),
            max_duration_seconds=_env_float(
                env, MAX_DURATION_SECONDS_ENV, DEFAULT_MAX_DURATION_SECONDS
            ),
            max_pixels=_env_int(env, MAX_PIXELS_ENV, DEFAULT_MAX_PIXELS),
            max_dimension=_env_int(env, MAX_DIMENSION_ENV, DEFAULT_MAX_DIMENSION),
            processing_timeout_seconds=_env_float(
                env, PROCESSING_TIMEOUT_SECONDS_ENV, DEFAULT_PROCESSING_TIMEOUT_SECONDS
            ),
            max_concurrent_processing=_env_int(
                env, MAX_CONCURRENT_PROCESSING_ENV, DEFAULT_MAX_CONCURRENT_PROCESSING
            ),
            max_processing_retries=_env_int(
                env, MAX_PROCESSING_RETRIES_ENV, DEFAULT_MAX_PROCESSING_RETRIES
            ),
            max_thumbnail_dimension=_env_int(
                env, MAX_THUMBNAIL_DIMENSION_ENV, DEFAULT_MAX_THUMBNAIL_DIMENSION
            ),
            retention_days=_env_int(env, RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS),
            max_video_frames=_env_int(env, MAX_VIDEO_FRAMES_ENV, DEFAULT_MAX_VIDEO_FRAMES),
            video_frame_interval_seconds=_env_float(
                env,
                VIDEO_FRAME_INTERVAL_SECONDS_ENV,
                DEFAULT_VIDEO_FRAME_INTERVAL_SECONDS,
            ),
            video_frame_max_dimension=_env_int(
                env, VIDEO_FRAME_MAX_DIMENSION_ENV, DEFAULT_VIDEO_FRAME_MAX_DIMENSION
            ),
            video_scene_threshold=_env_float_finite(
                env, VIDEO_SCENE_THRESHOLD_ENV, DEFAULT_VIDEO_SCENE_THRESHOLD
            ),
        )

    def validate_size(self, size: int) -> None:
        if size > self.max_bytes:
            raise MediaLimitError(
                f"file exceeds the {self.max_bytes} byte limit",
                code="too_large",
                limit=self.max_bytes,
                actual=size,
            )

    def validate_pixels(self, width: int, height: int) -> None:
        pixels = width * height
        if pixels > self.max_pixels:
            raise MediaLimitError(
                f"image exceeds the {self.max_pixels} pixel limit",
                code="limit_exceeded",
                limit=self.max_pixels,
                actual=pixels,
            )

    def validate_dimension(self, width: int, height: int) -> None:
        if width > self.max_dimension or height > self.max_dimension:
            raise MediaLimitError(
                f"resolution exceeds the {self.max_dimension} pixel dimension limit",
                code="limit_exceeded",
                limit=self.max_dimension,
                actual=max(width, height),
            )

    def validate_duration(self, seconds: float) -> None:
        if seconds > self.max_duration_seconds:
            raise MediaLimitError(
                f"media exceeds the {self.max_duration_seconds} second duration limit",
                code="limit_exceeded",
                limit=self.max_duration_seconds,
                actual=seconds,
            )
