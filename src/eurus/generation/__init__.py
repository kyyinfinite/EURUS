from __future__ import annotations

from typing import Any

from eurus.generation.config import GenerationConfig
from eurus.generation.tools import (
    EDIT_IMAGE_TOOL_NAME,
    GENERATE_IMAGE_TOOL_NAME,
    GENERATE_VIDEO_TOOL_NAME,
    GenerationJobTracker,
    edit_image_tool,
    generate_image_tool,
    generate_video_tool,
    generation_tools,
    register_generation_tools,
)


def __getattr__(name: str) -> Any:
    if name == "build_image_client":
        from eurus.generation.clients import build_image_client

        return build_image_client
    if name == "build_video_client":
        from eurus.generation.clients import build_video_client

        return build_video_client
    if name == "ImageGenerator":
        from eurus.generation.clients import ImageGenerator

        return ImageGenerator
    if name == "VideoGenerator":
        from eurus.generation.clients import VideoGenerator

        return VideoGenerator
    if name == "GeneratedImage":
        from eurus.generation.clients import GeneratedImage

        return GeneratedImage
    if name == "VideoJob":
        from eurus.generation.clients import VideoJob

        return VideoJob
    if name == "VideoJobStatus":
        from eurus.generation.clients import VideoJobStatus

        return VideoJobStatus
    if name == "GenerationError":
        from eurus.generation.clients import GenerationError

        return GenerationError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EDIT_IMAGE_TOOL_NAME",
    "GENERATE_IMAGE_TOOL_NAME",
    "GENERATE_VIDEO_TOOL_NAME",
    "GenerationConfig",
    "GenerationJobTracker",
    "edit_image_tool",
    "generate_image_tool",
    "generate_video_tool",
    "generation_tools",
    "register_generation_tools",
]
