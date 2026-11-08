from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from eurus.generation.clients import (
    ImageGenerator,
    VideoGenerator,
    VideoJob,
    VideoJobStatus,
)
from eurus.media.store import MediaFile, MediaStore
from eurus.tools.base import Tool, ToolContext

log = logging.getLogger(__name__)

GENERATE_IMAGE_TOOL_NAME = "generate_image"
EDIT_IMAGE_TOOL_NAME = "edit_image"
GENERATE_VIDEO_TOOL_NAME = "generate_video"

IMAGE_MARKER_TEMPLATE = "[image:{media_id}]"
VIDEO_MARKER_TEMPLATE = "[video:{media_id}]"

ASSET_ID_PATTERN = "^[a-f0-9]{32}$"

GENERATION_DELIVERY = Callable[[MediaFile, dict[str, Any]], Awaitable[None]]

GENERATE_IMAGE_DESCRIPTION = (
    "Generate new images from a text description using the configured image generation provider. "
    "Use it when the user asks to create, draw, design, or make an image or picture "
    "(for example 'buat gambar', 'generate a logo', 'draw me a poster'). "
    "After the tool succeeds, include each returned [image:...] marker EXACTLY as given in your "
    "final answer so the gateway can deliver the image files; also describe the result briefly."
)

EDIT_IMAGE_DESCRIPTION = (
    "Edit an existing stored image using the configured image generation provider. "
    "Reference the image by the asset id from an [attach ...] marker or an [image:...] marker. "
    "Use it when the user asks to modify, restyle, or extend a picture they sent or you generated. "
    "After the tool succeeds, include the returned [image:...] marker EXACTLY as given in your "
    "final answer so the gateway can deliver the edited image."
)

GENERATE_VIDEO_DESCRIPTION = (
    "Generate a short video from a text prompt (and optionally a starting image) using the "
    "configured video generation provider. Video generation runs as an asynchronous job: the tool "
    "waits a bounded time, and if the video is not ready yet it keeps generating in the background "
    "and the result is delivered to the user when ready. Use it when the user asks to create or "
    "make a video or animation. When the tool returns an [video:...] marker include it EXACTLY as "
    "given in your final answer; when it reports the job is still running, tell the user the video "
    "will arrive when it finishes."
)

GENERATE_IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "What the image should show, described clearly and specifically.",
        },
        "size": {
            "type": "string",
            "enum": ["auto", "1024x1024", "1536x1024", "1024x1536", "1792x1024", "1024x1792"],
            "description": "Optional output size; leave 'auto' for the default.",
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 4,
            "description": "How many image variations to generate (bounded by configuration).",
        },
    },
    "required": ["prompt"],
}

EDIT_IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "How the image should be changed.",
        },
        "image_asset_id": {
            "type": "string",
            "pattern": ASSET_ID_PATTERN,
            "description": "The 32-character asset id of the stored image to edit.",
        },
        "size": {
            "type": "string",
            "enum": ["auto", "1024x1024", "1536x1024", "1024x1536"],
            "description": "Optional output size; leave 'auto' to keep the source size.",
        },
    },
    "required": ["prompt", "image_asset_id"],
}

GENERATE_VIDEO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "What the video should show; describe the scene and motion.",
        },
        "image_asset_id": {
            "type": "string",
            "pattern": ASSET_ID_PATTERN,
            "description": (
                "Optional 32-character asset id of a stored image to animate (image-to-video)."
            ),
        },
        "aspect_ratio": {
            "type": "string",
            "enum": ["16:9", "9:16"],
            "description": "Output aspect ratio; default is 16:9.",
        },
    },
    "required": ["prompt"],
}


class DeliverySlot:
    def __init__(self) -> None:
        self._delivery: GENERATION_DELIVERY | None = None

    def set(self, delivery: GENERATION_DELIVERY | None) -> None:
        self._delivery = delivery

    async def deliver(self, media: MediaFile, info: dict[str, Any]) -> None:
        if self._delivery is None:
            return
        await self._delivery(media, info)


class GenerationJobTracker:
    def __init__(self) -> None:
        self._active: dict[str, tuple[VideoJob, asyncio.Task[None]]] = {}

    def key(self, prompt: str, image_asset_id: str) -> str:
        digest = hashlib.sha256(f"{prompt}\n{image_asset_id}".encode()).hexdigest()
        return digest[:32]

    def register(self, key: str, job: VideoJob, task: asyncio.Task[None]) -> None:
        self._active[key] = (job, task)

    def lookup(self, key: str) -> VideoJob | None:
        entry = self._active.get(key)
        return entry[0] if entry is not None else None

    def release(self, key: str) -> None:
        self._active.pop(key, None)

    def active_count(self) -> int:
        return sum(1 for _entry, task in self._active.values() if not task.done())


def _session_id(context: ToolContext, fallback: str) -> str:
    return (context.session_id or "").strip() or fallback


def _owner_id(context: ToolContext) -> str:
    return (context.owner_id or "local").strip() or "local"


def _emit(context: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
    sink = context.event_sink
    if sink is None:
        return
    try:
        sink(event_type, payload)
    except Exception:
        log.debug("generation event emission failed", exc_info=True)


async def _publish_artifact(
    context: ToolContext,
    media: MediaFile,
    *,
    source_tool: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    provider = context.artifacts
    if provider is None:
        return None
    from eurus.multiagent.artifacts import STANDALONE_MISSION_ID

    try:
        data = await asyncio.to_thread(_read_media, media)
    except OSError:
        return None
    try:
        record = provider().publish(
            data,
            owner_id=_owner_id(context),
            mission_id=(context.mission_id or "").strip() or STANDALONE_MISSION_ID,
            task_id=(context.task_id or "").strip() or None,
            artifact_type="image" if media.kind == "image" else "video",
            media_type=media.mime,
            metadata=metadata,
            name=media.filename,
            source_tool=source_tool,
            run_id=(context.run_id or "").strip(),
        )
    except Exception:
        log.warning("generation artifact publication failed", exc_info=True)
        return None
    return {
        "artifact_id": record.artifact_id,
        "name": record.name,
        "artifact_type": record.artifact_type,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "run_id": record.run_id,
    }


def _read_media(media: MediaFile) -> bytes:
    from pathlib import Path

    return Path(media.storage_path).read_bytes()


async def _save_generated(
    store: MediaStore,
    context: ToolContext,
    filename: str,
    data: bytes,
    mime: str,
) -> MediaFile | None:
    try:
        return await asyncio.to_thread(
            store.save,
            _session_id(context, "generation-output"),
            filename,
            data,
            declared_mime=mime,
            owner_id=_owner_id(context),
            deduplicate=False,
        )
    except Exception:
        log.warning("generated media storage failed filename=%s", filename, exc_info=True)
        return None


def generate_image_tool(
    store: MediaStore,
    client: ImageGenerator,
    *,
    default_size: str = "",
    max_outputs: int = 4,
    timeout_seconds: float = 180.0,
) -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "image generation failed: prompt is required"
        size = str(arguments.get("size") or "") or default_size
        try:
            count = int(arguments.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, max_outputs))
        _emit(
            context,
            "media.generation.started",
            {"kind": "image", "prompt": prompt[:200], "count": count},
        )
        try:
            images = await asyncio.wait_for(
                client.generate(prompt, size=size, count=count), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit(
                context,
                "media.generation.failed",
                {"kind": "image", "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            return f"image generation failed: {exc}"
        saved: list[MediaFile] = []
        for index, image in enumerate(images):
            filename = f"generated-image-{index + 1}.png"
            media = await _save_generated(store, context, filename, image.data, image.mime)
            if media is None:
                continue
            saved.append(media)
            artifact = await _publish_artifact(
                context,
                media,
                source_tool=GENERATE_IMAGE_TOOL_NAME,
                metadata={
                    "prompt": prompt[:1000],
                    "revised_prompt": image.revised_prompt[:1000],
                    "requested_size": size,
                },
            )
            _emit(
                context,
                "media.generation.ready",
                {
                    "kind": "image",
                    "asset_id": media.id,
                    "mime": media.mime,
                    "size": media.size,
                    "artifact_id": (artifact or {}).get("artifact_id", ""),
                },
            )
        if not saved:
            return "image generation produced no usable output"
        markers = "\n".join(IMAGE_MARKER_TEMPLATE.format(media_id=media.id) for media in saved)
        detail = ", ".join(f"{media.mime} ({media.size} bytes)" for media in saved)
        note = ""
        if images and images[0].revised_prompt:
            note = f" The provider revised the prompt to: {images[0].revised_prompt}"
        return (
            f"{markers}\nImage{'s' if len(saved) > 1 else ''} generated ({detail}).{note} "
            "Include the marker line above verbatim in your final answer so the images "
            "are delivered."
        )

    return Tool(
        name=GENERATE_IMAGE_TOOL_NAME,
        description=GENERATE_IMAGE_DESCRIPTION,
        parameters=GENERATE_IMAGE_SCHEMA,
        handler=handler,
        mutates=False,
    )


def edit_image_tool(
    store: MediaStore,
    client: ImageGenerator,
    *,
    max_outputs: int = 4,
    timeout_seconds: float = 180.0,
) -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "image edit failed: prompt is required"
        asset_id = str(arguments.get("image_asset_id", "")).strip()
        size = str(arguments.get("size") or "")
        if not client.supports_edit:
            return (
                "image editing is not supported by the configured image model; "
                "configure an OpenAI-compatible image model (for example gpt-image-1)"
            )
        media = await asyncio.to_thread(store.get, asset_id)
        if media is None:
            return f"image edit failed: unknown asset {asset_id}"
        owned = await asyncio.to_thread(store.get_owned, asset_id, _owner_id(context))
        if owned is None and media.owner_id:
            return f"image edit failed: asset {asset_id} belongs to another owner"
        if media.kind != "image":
            return f"image edit failed: asset {asset_id} is not an image"
        try:
            data = await asyncio.to_thread(_read_media, media)
        except OSError:
            return f"image edit failed: asset {asset_id} is not readable"
        _emit(
            context,
            "media.generation.started",
            {"kind": "image", "prompt": prompt[:200], "source_asset_id": asset_id},
        )
        try:
            images = await asyncio.wait_for(
                client.edit(prompt, data, media.mime, size=size), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit(
                context,
                "media.generation.failed",
                {"kind": "image", "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            return f"image edit failed: {exc}"
        saved: list[MediaFile] = []
        for index, image in enumerate(images[:max_outputs]):
            filename = f"edited-image-{index + 1}.png"
            edited = await _save_generated(store, context, filename, image.data, image.mime)
            if edited is None:
                continue
            saved.append(edited)
            await _publish_artifact(
                context,
                edited,
                source_tool=EDIT_IMAGE_TOOL_NAME,
                metadata={"prompt": prompt[:1000], "source_asset_id": asset_id},
            )
            _emit(
                context,
                "media.generation.ready",
                {
                    "kind": "image",
                    "asset_id": edited.id,
                    "mime": edited.mime,
                    "size": edited.size,
                    "source_asset_id": asset_id,
                },
            )
        if not saved:
            return "image edit produced no usable output"
        markers = "\n".join(IMAGE_MARKER_TEMPLATE.format(media_id=media.id) for media in saved)
        detail = ", ".join(f"{media.mime} ({media.size} bytes)" for media in saved)
        return (
            f"{markers}\nEdited image{'s' if len(saved) > 1 else ''} saved ({detail}). "
            "Include the marker line above verbatim in your final answer so the images "
            "are delivered."
        )

    return Tool(
        name=EDIT_IMAGE_TOOL_NAME,
        description=EDIT_IMAGE_DESCRIPTION,
        parameters=EDIT_IMAGE_SCHEMA,
        handler=handler,
        mutates=False,
    )


def generate_video_tool(
    store: MediaStore,
    client: VideoGenerator,
    *,
    poll_interval_seconds: float = 10.0,
    job_wait_seconds: float = 120.0,
    timeout_seconds: float = 900.0,
    delivery: GENERATION_DELIVERY | None = None,
    tracker: GenerationJobTracker | None = None,
) -> Tool:
    resolved_tracker = tracker if tracker is not None else GenerationJobTracker()

    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "video generation failed: prompt is required"
        image_asset_id = str(arguments.get("image_asset_id") or "").strip()
        aspect_ratio = str(arguments.get("aspect_ratio") or "").strip()
        image_bytes: bytes | None = None
        image_mime = ""
        if image_asset_id:
            media = await asyncio.to_thread(store.get_owned, image_asset_id, _owner_id(context))
            if media is None:
                media = await asyncio.to_thread(store.get, image_asset_id)
                if media is not None and media.owner_id:
                    return (
                        f"video generation failed: asset {image_asset_id} belongs to another owner"
                    )
            if media is None:
                return f"video generation failed: unknown asset {image_asset_id}"
            if media.kind != "image":
                return f"video generation failed: asset {image_asset_id} is not an image"
            try:
                image_bytes = await asyncio.to_thread(_read_media, media)
                image_mime = media.mime
            except OSError:
                return f"video generation failed: asset {image_asset_id} is not readable"
        job_key = resolved_tracker.key(prompt, image_asset_id)
        existing = resolved_tracker.lookup(job_key)
        if existing is not None:
            return (
                f"A video generation job for this exact prompt is already running "
                f"(job {existing.job_id}). Wait for its delivery instead of submitting a duplicate."
            )
        _emit(
            context,
            "media.generation.started",
            {"kind": "video", "prompt": prompt[:200], "image_asset_id": image_asset_id},
        )
        try:
            job = await client.submit(
                prompt, image=image_bytes, image_mime=image_mime, aspect_ratio=aspect_ratio
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit(
                context,
                "media.generation.failed",
                {"kind": "video", "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            return f"video generation failed: {exc}"
        deadline = time.monotonic() + max(job_wait_seconds, 0.0)
        status: VideoJobStatus | None = None
        while True:
            try:
                status = await client.status(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _emit(
                    context,
                    "media.generation.failed",
                    {"kind": "video", "error": f"{type(exc).__name__}: {exc}"[:500]},
                )
                return f"video generation failed while polling: {exc}"
            if status.done:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval_seconds)
        if status is not None and status.done and status.error:
            resolved_tracker.release(job_key)
            _emit(
                context,
                "media.generation.failed",
                {"kind": "video", "job_id": job.job_id, "error": status.error[:500]},
            )
            return f"video generation failed: {status.error}"
        if status is not None and status.done and status.video_uri:
            resolved_tracker.release(job_key)
            return await _finish_video(store, client, context, job, status, prompt, delivery)
        completion = asyncio.create_task(
            _complete_video_later(
                store,
                client,
                context,
                job,
                prompt,
                poll_interval_seconds,
                timeout_seconds,
                delivery,
            ),
            name=f"eurus-video-job-{job.job_id[-12:]}",
        )
        resolved_tracker.register(job_key, job, completion)
        return (
            f"Video generation is in progress (job {job.job_id}). "
            "It typically takes a few minutes. Tell the user the video will be delivered "
            "automatically when it is ready; do not resubmit the same request."
        )

    return Tool(
        name=GENERATE_VIDEO_TOOL_NAME,
        description=GENERATE_VIDEO_DESCRIPTION,
        parameters=GENERATE_VIDEO_SCHEMA,
        handler=handler,
        mutates=False,
    )


async def _finish_video(
    store: MediaStore,
    client: VideoGenerator,
    context: ToolContext,
    job: VideoJob,
    status: VideoJobStatus,
    prompt: str,
    delivery: GENERATION_DELIVERY | None,
) -> str:
    try:
        data = await client.download(job, status)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _emit(
            context,
            "media.generation.failed",
            {"kind": "video", "job_id": job.job_id, "error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        return f"video generation failed while downloading: {exc}"
    media = await _save_generated(store, context, "generated-video.mp4", data, "video/mp4")
    if media is None:
        return "video generation completed but the result could not be stored"
    artifact = await _publish_artifact(
        context,
        media,
        source_tool=GENERATE_VIDEO_TOOL_NAME,
        metadata={"prompt": prompt[:1000], "job_id": job.job_id},
    )
    _emit(
        context,
        "media.generation.ready",
        {
            "kind": "video",
            "asset_id": media.id,
            "mime": media.mime,
            "size": media.size,
            "job_id": job.job_id,
            "artifact_id": (artifact or {}).get("artifact_id", ""),
        },
    )
    if delivery is not None:
        try:
            await delivery(
                media,
                {"job_id": job.job_id, "prompt": prompt[:500]},
            )
        except Exception:
            log.warning("video generation delivery callback failed", exc_info=True)
    marker = VIDEO_MARKER_TEMPLATE.format(media_id=media.id)
    return (
        f"{marker}\nVideo generated ({media.mime}, {media.size} bytes). "
        "Include the marker above verbatim in your final answer so the video is delivered."
    )


async def _complete_video_later(
    store: MediaStore,
    client: VideoGenerator,
    context: ToolContext,
    job: VideoJob,
    prompt: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
    delivery: GENERATION_DELIVERY | None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            status = await client.status(job)
            if status.done:
                break
            if time.monotonic() >= deadline:
                _emit(
                    context,
                    "media.generation.failed",
                    {
                        "kind": "video",
                        "job_id": job.job_id,
                        "error": "video generation timed out",
                    },
                )
                return
            await asyncio.sleep(poll_interval_seconds)
        if status.error or not status.video_uri:
            _emit(
                context,
                "media.generation.failed",
                {
                    "kind": "video",
                    "job_id": job.job_id,
                    "error": (status.error or "no video")[:500],
                },
            )
            return
        await _finish_video(store, client, context, job, status, prompt, delivery)
    except asyncio.CancelledError:
        try:
            await client.cancel(job)
        except Exception:
            log.debug("video job cancellation propagation failed", exc_info=True)
        raise
    except Exception:
        log.warning("background video job completion failed job_id=%s", job.job_id, exc_info=True)
        _emit(
            context,
            "media.generation.failed",
            {"kind": "video", "job_id": job.job_id, "error": "background completion crashed"},
        )


def generation_tools(
    store: MediaStore,
    image_client: ImageGenerator,
    video_client: VideoGenerator | None,
    *,
    default_size: str = "",
    max_outputs: int = 4,
    image_timeout_seconds: float = 180.0,
    video_poll_interval_seconds: float = 10.0,
    video_job_wait_seconds: float = 120.0,
    video_timeout_seconds: float = 900.0,
    delivery: GENERATION_DELIVERY | None = None,
    tracker: GenerationJobTracker | None = None,
) -> list[Tool]:
    tools = [
        generate_image_tool(
            store,
            image_client,
            default_size=default_size,
            max_outputs=max_outputs,
            timeout_seconds=image_timeout_seconds,
        ),
        edit_image_tool(
            store,
            image_client,
            max_outputs=max_outputs,
            timeout_seconds=image_timeout_seconds,
        ),
    ]
    if video_client is not None:
        tools.append(
            generate_video_tool(
                store,
                video_client,
                poll_interval_seconds=video_poll_interval_seconds,
                job_wait_seconds=video_job_wait_seconds,
                timeout_seconds=video_timeout_seconds,
                delivery=delivery,
                tracker=tracker,
            )
        )
    return tools


def register_generation_tools(
    registry: Any,
    tools: list[Tool],
) -> int:
    registered = 0
    for tool in tools:
        if tool.name in registry:
            continue
        registry.register(tool)
        registered += 1
    return registered
