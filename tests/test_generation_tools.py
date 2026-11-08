import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import pytest

from eurus.config import build_config
from eurus.errors import ConfigError
from eurus.generation.clients import (
    GeminiImagenClient,
    GeminiVeoClient,
    OpenAICompatImageClient,
    build_image_client,
    build_video_client,
)
from eurus.generation.tools import (
    EDIT_IMAGE_TOOL_NAME,
    GENERATE_IMAGE_TOOL_NAME,
    GENERATE_VIDEO_TOOL_NAME,
    DeliverySlot,
    GenerationJobTracker,
    edit_image_tool,
    generate_image_tool,
    generate_video_tool,
    generation_tools,
    register_generation_tools,
)
from eurus.media.store import MediaStore
from eurus.providers.testing import FakeTransport
from eurus.tools.base import ToolContext

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)
MP4_BYTES = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomvideo-data"


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "db.sqlite", root=tmp_path / "media")


def make_context(**overrides: Any) -> ToolContext:
    values: dict[str, Any] = {
        "workspace": Path("/tmp"),
        "session_id": "telegram:1:10",
        "owner_id": "owner-1",
        "run_id": "run-1",
    }
    values.update(overrides)
    return ToolContext(**values)


def config_with_generation(generation: dict[str, Any]) -> Any:
    raw: dict[str, Any] = {
        "providers": {
            "openai": {
                "protocol": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            },
            "gemini": {
                "protocol": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key_env": "G_KEY",
            },
        },
        "agent": {"model": "openai/gpt-4o"},
        "generation": generation,
    }
    return build_config(raw, environ={"OPENAI_API_KEY": "k", "G_KEY": "g"})


class StubImageClient:
    def __init__(self, images: list[Any] | None = None, error: Exception | None = None) -> None:
        from eurus.generation.clients import GeneratedImage

        self.images = images or [GeneratedImage(data=PNG_BYTES, mime="image/png")]
        self.error = error
        self.generate_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.supports_edit = True

    async def generate(self, prompt: str, *, size: str = "", count: int = 1) -> list[Any]:
        self.generate_calls.append({"prompt": prompt, "size": size, "count": count})
        if self.error is not None:
            raise self.error
        return self.images

    async def edit(
        self, prompt: str, image: bytes, image_mime: str, *, size: str = ""
    ) -> list[Any]:
        self.edit_calls.append(
            {"prompt": prompt, "image": image, "image_mime": image_mime, "size": size}
        )
        if self.error is not None:
            raise self.error
        return self.images


class StubVideoClient:
    def __init__(self, statuses: list[Any] | None = None, video: bytes = MP4_BYTES) -> None:
        from eurus.generation.clients import VideoJob

        self.job = VideoJob(job_id="models/veo-3/operations/op-1", provider="goog", model="veo")
        self.statuses = list(statuses or [])
        self.status_calls = 0
        self.submit_calls: list[dict[str, Any]] = []
        self.download_calls = 0
        self.cancel_calls = 0
        self.video = video

    async def submit(
        self,
        prompt: str,
        *,
        image: bytes | None = None,
        image_mime: str = "",
        aspect_ratio: str = "",
    ) -> Any:
        self.submit_calls.append(
            {
                "prompt": prompt,
                "image": image,
                "image_mime": image_mime,
                "aspect_ratio": aspect_ratio,
            }
        )
        return self.job

    async def status(self, job: Any) -> Any:
        from eurus.generation.clients import VideoJobStatus

        self.status_calls += 1
        if self.statuses:
            item = self.statuses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return VideoJobStatus(done=True, video_uri="https://files/v1")

    async def download(self, job: Any, status: Any) -> bytes:
        self.download_calls += 1
        return self.video

    async def cancel(self, job: Any) -> bool:
        self.cancel_calls += 1
        return True


def _openai_client(transport: FakeTransport, model: str = "gpt-image-1") -> OpenAICompatImageClient:
    from eurus.catalog import BUILTIN_PROVIDERS

    return OpenAICompatImageClient(BUILTIN_PROVIDERS["openai"], "k", transport, model=model)


def _gemini_client(
    transport: FakeTransport, model: str = "imagen-3.0-generate-002"
) -> GeminiImagenClient:
    from eurus.catalog import BUILTIN_PROVIDERS

    return GeminiImagenClient(BUILTIN_PROVIDERS["gemini"], "g", transport, model=model)


def _veo_client(transport: FakeTransport, model: str = "veo-3.0-generate-001") -> GeminiVeoClient:
    from eurus.catalog import BUILTIN_PROVIDERS

    return GeminiVeoClient(BUILTIN_PROVIDERS["gemini"], "g", transport, model=model)


async def test_openai_image_generation_request_shape() -> None:
    transport = FakeTransport(
        [
            {
                "data": [
                    {"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
                ]
            }
        ]
    )
    client = _openai_client(transport)
    images = await client.generate("a copper coffee mug", size="1024x1024", count=1)
    assert images[0].data == PNG_BYTES
    assert images[0].mime == "image/png"
    call = transport.calls[0]
    assert call.url.endswith("/images/generations")
    assert call.payload["model"] == "gpt-image-1"
    assert call.payload["prompt"] == "a copper coffee mug"
    assert call.payload["size"] == "1024x1024"
    assert "response_format" not in call.payload


async def test_openai_dalle_requests_b64_format() -> None:
    transport = FakeTransport(
        [{"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}]
    )
    client = _openai_client(transport, model="dall-e-3")
    await client.generate("a poster")
    assert transport.calls[0].payload["response_format"] == "b64_json"


async def test_openai_image_generation_downloads_url_results() -> None:
    transport = FakeTransport(
        [{"data": [{"url": "https://files.example/img.png"}]}],
        get_bytes_responses=[PNG_BYTES],
    )
    client = _openai_client(transport)
    images = await client.generate("a cat")
    assert images[0].data == PNG_BYTES
    assert transport.get_calls[0][0] == "https://files.example/img.png"


async def test_openai_image_generation_rejects_empty_response() -> None:
    transport = FakeTransport([{"data": []}])
    client = _openai_client(transport)
    from eurus.generation.clients import GenerationError

    with pytest.raises(GenerationError, match="no data"):
        await client.generate("a cat")


async def test_openai_image_edit_request_shape() -> None:
    transport = FakeTransport(
        [{"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}]
    )
    client = _openai_client(transport)
    images = await client.edit("make the mug red", PNG_BYTES, "image/png", size="1024x1024")
    assert images[0].data == PNG_BYTES
    call = transport.calls[0]
    assert call.url.endswith("/images/edits")
    assert call.payload["form"]["model"] == "gpt-image-1"
    assert call.payload["form"]["prompt"] == "make the mug red"
    assert call.payload["files"]["image"][1] == PNG_BYTES
    assert call.payload["files"]["image"][2] == "image/png"


async def test_gemini_imagen_request_shape() -> None:
    transport = FakeTransport(
        [
            {
                "predictions": [
                    {
                        "bytesBase64Encoded": base64.b64encode(PNG_BYTES).decode("ascii"),
                        "mimeType": "image/png",
                    }
                ]
            }
        ]
    )
    client = _gemini_client(transport)
    images = await client.generate("a mountain at dawn", count=2)
    assert images[0].mime == "image/png"
    call = transport.calls[0]
    assert call.url.endswith("/models/imagen-3.0-generate-002:predict")
    assert call.payload["instances"][0]["prompt"] == "a mountain at dawn"
    assert call.payload["parameters"]["sampleCount"] == 2
    assert call.headers["x-goog-api-key"] == "g"


async def test_gemini_imagen_edit_is_unsupported() -> None:
    client = _gemini_client(FakeTransport())
    assert client.supports_edit is False
    from eurus.generation.clients import GenerationError

    with pytest.raises(GenerationError, match="not supported"):
        await client.edit("edit", PNG_BYTES, "image/png")


async def test_gemini_veo_submit_and_poll() -> None:
    from eurus.generation.clients import VideoJobStatus

    transport = FakeTransport(
        [{"name": "models/veo-3.0-generate-001/operations/abc123"}],
        get_json_responses=[
            {"name": "models/veo-3.0-generate-001/operations/abc123", "done": False},
            {
                "name": "models/veo-3.0-generate-001/operations/abc123",
                "done": True,
                "response": {
                    "generateVideoResponse": {
                        "generatedSamples": [{"video": {"uri": "https://f.example/v?alt=media"}}]
                    }
                },
            },
        ],
        get_bytes_responses=[MP4_BYTES],
    )
    client = _veo_client(transport)
    job = await client.submit("waves on a beach", aspect_ratio="16:9")
    assert job.job_id == "models/veo-3.0-generate-001/operations/abc123"
    assert transport.calls[0].url.endswith("/models/veo-3.0-generate-001:predictLongRunning")
    assert transport.calls[0].payload["instances"][0]["prompt"] == "waves on a beach"
    assert transport.calls[0].payload["parameters"]["aspectRatio"] == "16:9"
    pending = await client.status(job)
    assert pending.done is False
    finished = await client.status(job)
    assert finished.done is True
    assert finished.video_uri == "https://f.example/v?alt=media"
    data = await client.download(job, finished)
    assert data == MP4_BYTES
    assert transport.get_calls[-1][0] == "https://f.example/v?alt=media&alt=media"
    _ = VideoJobStatus(done=False)


async def test_gemini_veo_submit_with_image() -> None:
    transport = FakeTransport([{"name": "models/veo/operations/xyz"}])
    client = _veo_client(transport)
    await client.submit("animate this", image=PNG_BYTES, image_mime="image/png")
    instance = transport.calls[0].payload["instances"][0]
    assert instance["image"]["mimeType"] == "image/png"
    assert base64.b64decode(instance["image"]["bytesBase64Encoded"]) == PNG_BYTES


async def test_gemini_veo_error_status() -> None:

    transport = FakeTransport(
        [{"name": "models/veo/operations/oops"}],
        get_json_responses=[{"done": True, "error": {"message": "safety rejection"}}],
    )
    client = _veo_client(transport)
    job = await client.submit("something")
    status = await client.status(job)
    assert status.done is True
    assert "safety rejection" in status.error


async def test_gemini_veo_cancel() -> None:
    transport = FakeTransport([{"name": "models/veo/operations/x"}, {}])
    client = _veo_client(transport)
    job = await client.submit("cancel me")
    assert await client.cancel(job) is True
    assert transport.calls[-1].url.endswith(":cancel")


async def test_gemini_veo_cancel_failure_returns_false() -> None:
    from eurus.providers.transport import HttpError

    transport = FakeTransport(
        [{"name": "models/veo/operations/x"}],
    )
    transport._json.append(HttpError(404, "not found"))
    client = _veo_client(transport)
    job = await client.submit("cancel me")
    assert await client.cancel(job) is False


def test_build_image_client_none_without_model() -> None:
    config = config_with_generation({})
    assert build_image_client(config) is None
    assert build_video_client(config) is None


def test_build_image_client_openai() -> None:
    config = config_with_generation({"image_model": "openai/gpt-image-1"})
    client = build_image_client(config)
    assert isinstance(client, OpenAICompatImageClient)
    assert client.model == "gpt-image-1"


def test_build_image_client_gemini() -> None:
    config = config_with_generation({"image_model": "gemini/imagen-3.0-generate-002"})
    client = build_image_client(config)
    assert isinstance(client, GeminiImagenClient)


def test_build_image_client_rejects_non_generation_model() -> None:
    config = config_with_generation({"image_model": "openai/gpt-4o"})
    with pytest.raises(ConfigError, match="image generation support"):
        build_image_client(config)


def test_build_image_client_unknown_provider() -> None:
    config = config_with_generation({"image_model": "nope/gpt-image-1"})
    with pytest.raises(ConfigError, match="unknown provider"):
        build_image_client(config)


def test_build_video_client_requires_gemini_protocol() -> None:
    from eurus.providers.model_capabilities import CapabilityRegistry, ModelCapabilities

    registry = CapabilityRegistry(
        model_declarations={
            ("openai", "video-wizard"): ModelCapabilities(
                text_input=True, text_output=False, video_generation=True
            )
        }
    )
    config = config_with_generation({"video_model": "openai/video-wizard"})
    with pytest.raises(ConfigError, match="Gemini provider"):
        build_video_client(config, registry=registry)


def test_build_video_client_gemini() -> None:
    config = config_with_generation({"video_model": "gemini/veo-3.0-generate-001"})
    client = build_video_client(config)
    assert isinstance(client, GeminiVeoClient)


def test_build_video_client_rejects_non_video_model() -> None:
    config = config_with_generation({"video_model": "gemini/gemini-2.0"})
    with pytest.raises(ConfigError, match="video generation support"):
        build_video_client(config)


async def test_generate_image_tool_saves_and_returns_marker(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = StubImageClient()
    events: list[tuple[str, dict[str, Any]]] = []
    context = make_context(event_sink=lambda kind, payload: events.append((kind, payload)))
    tool = generate_image_tool(store, client)
    result = await tool.handler({"prompt": "a copper kettle", "count": 1}, context)
    assert result.startswith("[image:")
    media_id = result.split("]", 1)[0][len("[image:") :]
    media = store.get(media_id)
    assert media is not None
    assert media.kind == "image"
    assert media.mime == "image/png"
    assert media.owner_id == "owner-1"
    assert media.session_id == "telegram:1:10"
    kinds = [kind for kind, _payload in events]
    assert kinds == ["media.generation.started", "media.generation.ready"]
    assert events[1][1]["asset_id"] == media_id


async def test_generate_image_tool_requires_prompt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    tool = generate_image_tool(store, StubImageClient())
    result = await tool.handler({"prompt": "  "}, make_context())
    assert result.startswith("image generation failed")


async def test_generate_image_tool_bounds_count(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = StubImageClient()
    tool = generate_image_tool(store, client, max_outputs=2)
    await tool.handler({"prompt": "x", "count": 99}, make_context())
    assert client.generate_calls[0]["count"] == 2


async def test_generate_image_tool_reports_provider_failure(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = StubImageClient(error=RuntimeError("quota exceeded"))
    events: list[tuple[str, dict[str, Any]]] = []
    context = make_context(event_sink=lambda kind, payload: events.append((kind, payload)))
    tool = generate_image_tool(store, client)
    result = await tool.handler({"prompt": "x"}, context)
    assert "image generation failed" in result
    assert "quota exceeded" in result
    assert events[-1][0] == "media.generation.failed"


async def test_generate_image_tool_timeout(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    class SlowClient(StubImageClient):
        async def generate(self, prompt: str, *, size: str = "", count: int = 1) -> list[Any]:
            await asyncio.sleep(5.0)
            return self.images

    tool = generate_image_tool(store, SlowClient(), timeout_seconds=0.05)
    result = await tool.handler({"prompt": "x"}, make_context())
    assert "image generation failed" in result


async def test_edit_image_tool_requires_image_asset(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    tool = edit_image_tool(store, StubImageClient())
    result = await tool.handler({"prompt": "x", "image_asset_id": "a" * 32}, make_context())
    assert "unknown asset" in result


async def test_edit_image_tool_rejects_other_owners(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "photo.png", PNG_BYTES, owner_id="someone-else")
    tool = edit_image_tool(store, StubImageClient())
    result = await tool.handler(
        {"prompt": "x", "image_asset_id": media.id}, make_context(owner_id="owner-1")
    )
    assert "belongs to another owner" in result


async def test_edit_image_tool_edits_and_saves(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("telegram:1:10", "photo.png", PNG_BYTES, owner_id="owner-1")
    client = StubImageClient()
    tool = edit_image_tool(store, client)
    result = await tool.handler(
        {"prompt": "make it warmer", "image_asset_id": media.id}, make_context()
    )
    assert result.startswith("[image:")
    assert client.edit_calls[0]["image"] == PNG_BYTES
    edited_id = result.split("]", 1)[0][len("[image:") :]
    edited = store.get(edited_id)
    assert edited is not None
    assert edited.id != media.id


async def test_edit_image_tool_unsupported_client(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    client = StubImageClient()
    client.supports_edit = False
    tool = edit_image_tool(store, client)
    result = await tool.handler({"prompt": "x", "image_asset_id": "a" * 32}, make_context())
    assert "not supported" in result


async def test_generate_video_tool_sync_completion(tmp_path: Path) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    client = StubVideoClient(statuses=[VideoJobStatus(done=True, video_uri="https://f.example/v")])
    delivered: list[Any] = []

    async def delivery(media: Any, info: dict[str, Any]) -> None:
        delivered.append(media)

    tool = generate_video_tool(
        store, client, poll_interval_seconds=0.01, job_wait_seconds=1.0, delivery=delivery
    )
    result = await tool.handler({"prompt": "sunset timelapse"}, make_context())
    assert result.startswith("[video:")
    media_id = result.split("]", 1)[0][len("[video:") :]
    media = store.get(media_id)
    assert media is not None
    assert media.kind == "video"
    assert delivered and delivered[0].id == media_id
    assert client.download_calls == 1


async def test_generate_video_tool_async_completion_background(
    tmp_path: Path,
) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    client = StubVideoClient(
        statuses=[
            VideoJobStatus(done=False),
            VideoJobStatus(done=False),
            VideoJobStatus(done=True, video_uri="https://f.example/v"),
        ]
    )
    delivered: list[Any] = []

    async def delivery(media: Any, info: dict[str, Any]) -> None:
        delivered.append(media)

    tracker = GenerationJobTracker()
    tool = generate_video_tool(
        store,
        client,
        poll_interval_seconds=0.01,
        job_wait_seconds=0.0,
        timeout_seconds=5.0,
        delivery=delivery,
        tracker=tracker,
    )
    result = await tool.handler({"prompt": "slow render"}, make_context())
    assert "in progress" in result
    assert client.submit_calls
    deadline = time.monotonic() + 5.0
    while not delivered and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert delivered
    media = delivered[0]
    assert media.kind == "video"
    stored = store.get(media.id)
    assert stored is not None


async def test_generate_video_tool_duplicate_prompt_is_prevented(tmp_path: Path) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    client = StubVideoClient(statuses=[VideoJobStatus(done=False)] * 10)
    tracker = GenerationJobTracker()
    tool = generate_video_tool(
        store,
        client,
        poll_interval_seconds=0.05,
        job_wait_seconds=0.0,
        timeout_seconds=30.0,
        tracker=tracker,
    )
    first = await tool.handler({"prompt": "same prompt"}, make_context())
    assert "in progress" in first
    second = await tool.handler({"prompt": "same prompt"}, make_context())
    assert "already running" in second
    assert len(client.submit_calls) == 1


async def test_generate_video_tool_reports_job_error(tmp_path: Path) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    client = StubVideoClient(statuses=[VideoJobStatus(done=True, error="content policy violation")])
    tool = generate_video_tool(store, client, poll_interval_seconds=0.01, job_wait_seconds=1.0)
    result = await tool.handler({"prompt": "x"}, make_context())
    assert "video generation failed" in result
    assert "content policy violation" in result


async def test_generate_video_tool_image_to_video(tmp_path: Path) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    media = store.save("telegram:1:10", "start.png", PNG_BYTES, owner_id="owner-1")
    client = StubVideoClient(statuses=[VideoJobStatus(done=True, video_uri="https://f.example/v")])
    tool = generate_video_tool(store, client, poll_interval_seconds=0.01, job_wait_seconds=1.0)
    result = await tool.handler({"prompt": "animate", "image_asset_id": media.id}, make_context())
    assert result.startswith("[video:")
    assert client.submit_calls[0]["image"] == PNG_BYTES


async def test_generate_video_tool_rejects_other_owners_image(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "start.png", PNG_BYTES, owner_id="someone-else")
    client = StubVideoClient()
    tool = generate_video_tool(store, client, poll_interval_seconds=0.01, job_wait_seconds=1.0)
    result = await tool.handler(
        {"prompt": "animate", "image_asset_id": media.id}, make_context(owner_id="owner-1")
    )
    assert "belongs to another owner" in result


async def test_generate_video_tool_requires_image_kind(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "notes.txt", b"hello world")
    tool = generate_video_tool(
        store, StubVideoClient(), poll_interval_seconds=0.01, job_wait_seconds=1.0
    )
    result = await tool.handler({"prompt": "animate", "image_asset_id": media.id}, make_context())
    assert "is not an image" in result


async def test_generation_publishes_artifacts(tmp_path: Path) -> None:
    from eurus.multiagent.artifacts import ArtifactStore

    store = make_store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_store = ArtifactStore(tmp_path / "db.sqlite", artifact_root)

    class Provider:
        def __call__(self) -> ArtifactStore:
            return artifact_store

    client = StubImageClient()
    tool = generate_image_tool(store, client)
    context = make_context(artifacts=Provider())
    result = await tool.handler({"prompt": "a kettle"}, context)
    media_id = result.split("]", 1)[0][len("[image:") :]
    records = artifact_store.list_for_run("owner-1", "run-1")
    assert records
    assert any(record.media_type == "image/png" for record in records)
    _ = media_id


def test_generation_tools_registry_registration(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    video_client = StubVideoClient()
    tools = generation_tools(store, StubImageClient(), video_client)
    assert [tool.name for tool in tools] == [
        GENERATE_IMAGE_TOOL_NAME,
        EDIT_IMAGE_TOOL_NAME,
        GENERATE_VIDEO_TOOL_NAME,
    ]
    from eurus.tools.base import ToolRegistry

    registry = ToolRegistry()
    registered = register_generation_tools(registry, tools)
    assert registered == 3
    assert register_generation_tools(registry, tools) == 0


def test_delivery_slot_defaults_to_noop(tmp_path: Path) -> None:
    slot = DeliverySlot()
    media = make_store(tmp_path).save("s1", "x.png", PNG_BYTES)

    async def run() -> None:
        await slot.deliver(media, {})

    asyncio.run(run())
    received: list[Any] = []

    async def delivery(item: Any, info: dict[str, Any]) -> None:
        received.append(item)

    slot.set(delivery)

    async def run_again() -> None:
        await slot.deliver(media, {"prompt": "p"})

    asyncio.run(run_again())
    assert received == [media]


async def test_video_tool_emits_lifecycle_events(tmp_path: Path) -> None:
    from eurus.generation.clients import VideoJobStatus

    store = make_store(tmp_path)
    client = StubVideoClient(statuses=[VideoJobStatus(done=True, video_uri="https://f.example/v")])
    events: list[tuple[str, dict[str, Any]]] = []
    context = make_context(event_sink=lambda kind, payload: events.append((kind, payload)))
    tool = generate_video_tool(store, client, poll_interval_seconds=0.01, job_wait_seconds=1.0)
    await tool.handler({"prompt": "waves"}, context)
    kinds = [kind for kind, _payload in events]
    assert kinds == ["media.generation.started", "media.generation.ready"]
    assert events[1][1]["kind"] == "video"


async def test_gateway_marker_delivery_for_generated_media(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import deliver_media_responses

    store = make_store(tmp_path)
    image = store.save("s1", "gen.png", PNG_BYTES)
    video = store.save("s1", "gen.mp4", MP4_BYTES)
    sent: list[tuple[str, str, str]] = []

    class Sender:
        async def send_media(self, chat_id: int, media: Any, caption: str = "") -> None:
            sent.append((str(chat_id), media.mime, media.filename))

    answer = f"Here is your image:\n[image:{image.id}]\nAnd the video:\n[video:{video.id}]"
    cleaned = await deliver_media_responses(10, answer, store, Sender())
    assert cleaned == "Here is your image:\n\nAnd the video:"
    assert sorted(sent) == [
        ("10", "image/png", "gen.png"),
        ("10", "video/mp4", "gen.mp4"),
    ]


async def test_gateway_marker_delivery_missing_asset(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import deliver_media_responses

    store = make_store(tmp_path)

    class Sender:
        async def send_media(self, chat_id: int, media: Any, caption: str = "") -> None:
            raise AssertionError("sender must not be called for missing assets")

    cleaned = await deliver_media_responses(10, f"only [image:{'a' * 32}] here", store, Sender())
    assert cleaned == "only  here"


async def test_gateway_marker_delivery_only_media(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import deliver_media_responses

    store = make_store(tmp_path)
    video = store.save("s1", "gen.mp4", MP4_BYTES)

    class Sender:
        async def send_media(self, chat_id: int, media: Any, caption: str = "") -> None:
            pass

    cleaned = await deliver_media_responses(10, f"[video:{video.id}]", store, Sender())
    assert cleaned == "Video response delivered."


def test_generation_config_validation() -> None:
    from eurus.generation.config import GenerationConfig

    with pytest.raises(ValueError):
        GenerationConfig(image_timeout_seconds=0)
    with pytest.raises(ValueError):
        GenerationConfig(video_poll_interval_seconds=-1)
    with pytest.raises(ValueError):
        GenerationConfig(max_outputs_per_request=0)
    with pytest.raises(ValueError):
        GenerationConfig(image_size="999x999")
    with pytest.raises(ValueError):
        GenerationConfig(video_aspect_ratio="4:3")


def test_generation_config_env_overrides() -> None:
    from eurus.config import env_overrides

    env = {
        "EURUS_IMAGE_MODEL": "openai/gpt-image-1",
        "EURUS_VIDEO_MODEL": "gemini/veo-3.0-generate-001",
        "EURUS_VIDEO_JOB_WAIT_SECONDS": "30",
        "EURUS_GENERATION_MAX_OUTPUTS": "2",
    }
    config = build_config(env_overrides(env), environ=env)
    assert config.generation.image_model == "openai/gpt-image-1"
    assert config.generation.video_model == "gemini/veo-3.0-generate-001"
    assert config.generation.video_job_wait_seconds == 30.0
    assert config.generation.max_outputs_per_request == 2
