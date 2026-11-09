from __future__ import annotations

from dataclasses import dataclass

IMAGE_SIZES = (
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "1792x1024",
    "1024x1792",
    "512x512",
)
VIDEO_ASPECT_RATIOS = ("16:9", "9:16")
DEFAULT_IMAGE_TIMEOUT_SECONDS = 180.0
DEFAULT_VIDEO_TIMEOUT_SECONDS = 900.0
DEFAULT_VIDEO_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_VIDEO_JOB_WAIT_SECONDS = 120.0
DEFAULT_MAX_OUTPUTS_PER_REQUEST = 4
MAX_GENERATION_PROMPT_CHARS = 4000


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    image_model: str = ""
    image_size: str = ""
    image_timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS
    video_model: str = ""
    video_aspect_ratio: str = "16:9"
    video_timeout_seconds: float = DEFAULT_VIDEO_TIMEOUT_SECONDS
    video_poll_interval_seconds: float = DEFAULT_VIDEO_POLL_INTERVAL_SECONDS
    video_job_wait_seconds: float = DEFAULT_VIDEO_JOB_WAIT_SECONDS
    max_outputs_per_request: int = DEFAULT_MAX_OUTPUTS_PER_REQUEST

    def __post_init__(self) -> None:
        if self.image_timeout_seconds <= 0:
            raise ValueError("image_timeout_seconds must be positive")
        if self.video_timeout_seconds <= 0:
            raise ValueError("video_timeout_seconds must be positive")
        if self.video_poll_interval_seconds <= 0:
            raise ValueError("video_poll_interval_seconds must be positive")
        if self.video_job_wait_seconds < 0:
            raise ValueError("video_job_wait_seconds cannot be negative")
        if self.max_outputs_per_request < 1:
            raise ValueError("max_outputs_per_request must be positive")
        if self.image_size and self.image_size not in IMAGE_SIZES:
            raise ValueError(f"image_size must be one of {IMAGE_SIZES}")
        if self.video_aspect_ratio and self.video_aspect_ratio not in VIDEO_ASPECT_RATIOS:
            raise ValueError(f"video_aspect_ratio must be one of {VIDEO_ASPECT_RATIOS}")
