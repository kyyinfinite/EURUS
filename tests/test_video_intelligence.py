import asyncio
import base64
import io
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eurus.content import VideoBlock, VideoFrameBlock
from eurus.events.multimodal import MultimodalEventPublisher
from eurus.events.store import DomainEventStore
from eurus.events.types import (
    MEDIA_ANALYSIS_COMPLETED,
    MEDIA_PROCESSING_STARTED,
    VIDEO_FRAME_EXTRACTED,
)
from eurus.media.limits import MediaLimits
from eurus.media.processing import MediaProcessor, ffprobe_available, run_processing_pipeline
from eurus.media.store import MediaStore
from eurus.media.video import (
    extract_audio_track,
    extract_frame_images,
    ffmpeg_available,
    generate_video_thumbnail,
    sample_frame_timestamps,
    scaled_dimensions,
    video_summary,
)
from eurus.models import Message, ModelRef
from eurus.providers.media_parts import (
    GEMINI_VIDEO_MIMES,
    MediaStoreAssetResolver,
    ResolvedAsset,
    collect_block_assets,
    resolve_request_assets,
)
from eurus.providers.model_capabilities import required_modalities_for_messages

requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")
requires_ffprobe = pytest.mark.skipif(not ffprobe_available(), reason="ffprobe is not installed")

ASSET_A = "a" * 32
ASSET_B = "b" * 32


def _ffmpeg_generate(arguments: list[str]) -> bytes:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("ffmpeg is not installed")
    with tempfile.TemporaryDirectory(prefix="eurus-test-video-") as temp_dir:
        output = Path(temp_dir) / "out.mp4"
        completed = subprocess.run(
            [executable, "-y", "-hide_banner", "-loglevel", "error", *arguments, str(output)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        return output.read_bytes()


@pytest.fixture
def video_payload() -> bytes:
    return b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isom" + b"\x00" * 64


@pytest.fixture(scope="module")
def video_with_audio() -> bytes:
    return _ffmpeg_generate(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=6:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
        ]
    )


@pytest.fixture(scope="module")
def video_silent() -> bytes:
    return _ffmpeg_generate(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=4:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ]
    )


@pytest.fixture(scope="module")
def video_scene_change() -> bytes:
    return _ffmpeg_generate(
        [
            "-f",
            "lavfi",
            "-i",
            "color=red:duration=2:size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "color=blue:duration=2:size=160x120:rate=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ]
    )


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "db.sqlite", root=tmp_path / "media")


def make_processor(**limits: Any) -> MediaProcessor:
    return MediaProcessor(limits=MediaLimits(**limits) if limits else MediaLimits())


def test_sample_frame_timestamps_uniform() -> None:
    stamps = sample_frame_timestamps(10.0, interval_seconds=2.0, max_frames=8)
    assert stamps == [0.0, 2.0, 4.0, 6.0, 8.0]


def test_sample_frame_timestamps_caps_frame_count() -> None:
    stamps = sample_frame_timestamps(3600.0, interval_seconds=2.0, max_frames=6)
    assert len(stamps) == 6
    assert stamps[0] == 0.0
    assert all(later > earlier for earlier, later in zip(stamps, stamps[1:], strict=False))


def test_sample_frame_timestamps_invalid_inputs() -> None:
    assert sample_frame_timestamps(0.0, interval_seconds=2.0, max_frames=8) == []
    assert sample_frame_timestamps(-5.0, interval_seconds=2.0, max_frames=8) == []
    assert sample_frame_timestamps(10.0, interval_seconds=2.0, max_frames=0) == []


def test_sample_frame_timestamps_prefers_scene_changes() -> None:
    stamps = sample_frame_timestamps(
        10.0, interval_seconds=2.0, max_frames=4, scene_timestamps=[3.4, 7.9]
    )
    assert 3.4 in stamps
    assert 7.9 in stamps
    assert stamps[0] == 0.0
    assert len(stamps) == 4


def test_sample_frame_timestamps_dedupes_close_stamps() -> None:
    stamps = sample_frame_timestamps(
        10.0, interval_seconds=2.0, max_frames=8, scene_timestamps=[0.1, 0.2, 2.05]
    )
    assert stamps.count(0.0) == 1
    assert len(stamps) == len(set(stamps))


def test_scaled_dimensions() -> None:
    assert scaled_dimensions(100, 50, 512) is None
    assert scaled_dimensions(1920, 1080, 512) == (512, 288)
    assert scaled_dimensions(1080, 1920, 512) == (288, 512)
    assert scaled_dimensions(None, 1080, 512) is None


@requires_ffprobe
def test_extract_video_metadata_with_audio(video_with_audio: bytes) -> None:
    from eurus.media.processing import extract_video_metadata

    metadata = extract_video_metadata(video_with_audio)
    assert metadata is not None
    assert metadata.duration_seconds is not None and 5.0 < metadata.duration_seconds < 7.0
    assert metadata.width == 320
    assert metadata.height == 240
    assert metadata.has_audio is True
    assert metadata.codec == "h264"
    assert metadata.frame_rate is not None and 9.0 < metadata.frame_rate < 11.0


@requires_ffprobe
def test_extract_video_metadata_without_audio(video_silent: bytes) -> None:
    from eurus.media.processing import extract_video_metadata

    metadata = extract_video_metadata(video_silent)
    assert metadata is not None
    assert metadata.has_audio is False


@requires_ffmpeg
def test_extract_frame_images_preserves_timestamps(video_with_audio: bytes) -> None:
    stamps = [0.0, 2.5, 5.0]
    frames = extract_frame_images(
        video_with_audio, stamps, source_width=320, source_height=240, max_dimension=1024
    )
    assert [round(frame[1], 3) for frame in frames] == stamps
    for frame_data, _stamp, width, height in frames:
        assert frame_data[:3] == b"\xff\xd8\xff"
        assert width == 320
        assert height == 240


@requires_ffmpeg
def test_extract_frame_images_caps_resolution(video_with_audio: bytes) -> None:
    frames = extract_frame_images(
        video_with_audio, [1.0], source_width=320, source_height=240, max_dimension=100
    )
    assert frames
    _data, _stamp, width, height = frames[0]
    assert width <= 100
    assert height <= 100


@requires_ffmpeg
def test_extract_frame_images_corrupt_payload() -> None:
    frames = extract_frame_images(b"not a video at all", [0.0])
    assert frames == []


@requires_ffmpeg
def test_generate_video_thumbnail(video_with_audio: bytes) -> None:
    thumbnail = generate_video_thumbnail(video_with_audio, duration_seconds=6.0, max_dimension=128)
    assert thumbnail is not None
    assert thumbnail[:3] == b"\xff\xd8\xff"


@requires_ffmpeg
def test_extract_audio_track(video_with_audio: bytes) -> None:
    result = extract_audio_track(video_with_audio, duration_seconds=6.0)
    assert result is not None
    audio, truncated = result
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
    assert truncated is False


@requires_ffmpeg
def test_extract_audio_track_truncates_long_videos(video_with_audio: bytes) -> None:
    result = extract_audio_track(video_with_audio, duration_seconds=100_000.0, max_bytes=32_000 * 5)
    assert result is not None
    audio, truncated = result
    assert truncated is True
    assert len(audio) <= 32_000 * 5


@requires_ffmpeg
def test_extract_audio_track_missing_audio(video_silent: bytes) -> None:
    assert extract_audio_track(video_silent, duration_seconds=4.0) is None


@requires_ffmpeg
def test_detect_scene_changes(video_scene_change: bytes) -> None:
    from eurus.media.video import detect_scene_changes

    stamps = detect_scene_changes(video_scene_change, threshold=0.3)
    assert stamps
    assert any(1.0 <= stamp <= 3.0 for stamp in stamps)


@requires_ffmpeg
async def test_enrich_video_media_full_pipeline(tmp_path: Path, video_with_audio: bytes) -> None:
    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "clip.mp4", video_with_audio)
    event_store = DomainEventStore(tmp_path / "events.sqlite")
    publisher = MultimodalEventPublisher(event_store, owner_id="owner-1")
    from eurus.media.processing import run_processing_pipeline as pipeline

    outcome = await pipeline(store, media, processor, data=video_with_audio, events=publisher)
    assert outcome.status == "ready"
    refreshed = store.get(media.id)
    assert refreshed is not None
    metadata = refreshed.metadata
    assert metadata.get("duration_seconds") is not None
    assert metadata.get("has_audio") is True
    assert metadata.get("thumbnail_asset_id")
    assert metadata.get("audio_asset_id")
    frames = metadata.get("frames")
    assert isinstance(frames, list) and frames
    for entry in frames:
        frame_media = store.get(entry["asset_id"])
        assert frame_media is not None
        assert frame_media.mime == "image/jpeg"
        assert frame_media.metadata.get("source_video_id", media.id) in (media.id, None)
    events = event_store.list_events(limit=50)
    types = [event.event_type for event in events]
    assert MEDIA_PROCESSING_STARTED in types
    assert VIDEO_FRAME_EXTRACTED in types
    assert MEDIA_ANALYSIS_COMPLETED in types
    frame_events = [event for event in events if event.event_type == VIDEO_FRAME_EXTRACTED]
    assert len(frame_events) == len(frames)
    for event in frame_events:
        assert event.payload["frame_media_id"] in {entry["asset_id"] for entry in frames}
        assert event.payload["timestamp_seconds"] >= 0


@requires_ffmpeg
async def test_enrich_video_media_without_audio(tmp_path: Path, video_silent: bytes) -> None:
    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "silent.mp4", video_silent)
    await run_processing_pipeline(store, media, processor, data=video_silent)
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert refreshed.metadata.get("has_audio") is False
    assert "audio_asset_id" not in refreshed.metadata
    assert refreshed.metadata.get("frames")


async def test_processing_pipeline_without_ffmpeg_degrades(
    tmp_path: Path, video_payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eurus.media.video.ffmpeg_available", lambda: False)
    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "clip.mp4", video_payload)
    outcome = await run_processing_pipeline(store, media, processor, data=video_payload)
    assert outcome.status == "ready"
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert "frames" not in refreshed.metadata
    assert "thumbnail_asset_id" not in refreshed.metadata


async def test_processing_pipeline_corrupt_video(tmp_path: Path) -> None:
    garbage = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isom" + b"\x00" * 64
    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "broken.mp4", garbage)
    outcome = await run_processing_pipeline(store, media, processor, data=garbage)
    assert outcome.status == "ready"
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert "frames" not in refreshed.metadata


@requires_ffmpeg
async def test_processing_pipeline_timeout_degrades(
    tmp_path: Path, video_with_audio: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_thumbnail(*args: Any, **kwargs: Any) -> bytes | None:
        import time

        time.sleep(3.0)
        return None

    monkeypatch.setattr("eurus.media.video.generate_video_thumbnail", slow_thumbnail)
    store = make_store(tmp_path)
    processor = make_processor(processing_timeout_seconds=1.0)
    media = store.save("session-1", "clip.mp4", video_with_audio)
    outcome = await run_processing_pipeline(store, media, processor, data=video_with_audio)
    assert outcome.status == "ready"
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert "thumbnail_asset_id" not in refreshed.metadata


async def test_processing_pipeline_preserves_existing_frames_on_failure(
    tmp_path: Path, video_payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eurus.media.video.ffmpeg_available", lambda: False)
    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "clip.mp4", video_payload)
    store.mark_ready(media.id, {"frames": [{"asset_id": ASSET_A, "timestamp_seconds": 1.0}]})
    await run_processing_pipeline(store, media, processor, data=video_payload)
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert refreshed.metadata.get("frames") == [{"asset_id": ASSET_A, "timestamp_seconds": 1.0}]


def test_video_content_blocks_from_metadata(tmp_path: Path) -> None:
    from eurus.media.store import video_content_blocks

    store = make_store(tmp_path)
    media = store.save("session-1", "clip.mp4", b"\x00" * 8 + b"ftypisom" + b"\x00" * 16)
    store.mark_ready(
        media.id,
        {
            "duration_seconds": 12.5,
            "thumbnail_asset_id": ASSET_A,
            "transcript": "hello from the video",
            "frames": [
                {"asset_id": ASSET_B, "timestamp_seconds": 2.0},
                {"asset_id": ASSET_B, "timestamp_seconds": 4.0},
            ],
        },
    )
    refreshed = store.get(media.id)
    assert refreshed is not None
    blocks = video_content_blocks(refreshed)
    assert isinstance(blocks[0], VideoBlock)
    assert blocks[0].duration_seconds == 12.5
    assert blocks[0].thumbnail_asset_id == ASSET_A
    assert blocks[0].transcript == "hello from the video"
    frame_blocks = [block for block in blocks[1:]]
    assert all(isinstance(block, VideoFrameBlock) for block in frame_blocks)
    assert [block.timestamp_seconds for block in frame_blocks] == [2.0, 4.0]
    assert all(block.source_asset_id == media.id for block in frame_blocks)


def test_video_marker_includes_transcript_and_frames(tmp_path: Path) -> None:
    from eurus.media.store import compose_attachment_block

    store = make_store(tmp_path)
    media = store.save("session-1", "clip.mp4", b"\x00" * 8 + b"ftypisom" + b"\x00" * 16)
    store.mark_ready(
        media.id,
        {
            "duration_seconds": 12.5,
            "transcript": "spoken words",
            "frames": [{"asset_id": ASSET_B, "timestamp_seconds": 3.0}],
        },
    )
    refreshed = store.get(media.id)
    assert refreshed is not None
    text = compose_attachment_block([refreshed])
    assert "Duration: 12.5 seconds" in text
    assert "spoken words" in text
    assert "Keyframes sampled at: 3.0s" in text


def test_video_block_transcript_roundtrip() -> None:
    block = VideoBlock(asset_id=ASSET_A, mime_type="video/mp4", transcript="say hi")
    data = block.to_dict()
    assert data["transcript"] == "say hi"
    restored = VideoBlock.from_dict(data)
    assert restored.transcript == "say hi"
    legacy = VideoBlock.from_dict({"type": "video", "asset_id": ASSET_A, "mime_type": "video/mp4"})
    assert legacy.transcript == ""


def test_video_wire_modality_contract() -> None:
    from eurus.providers.media_parts import WIRE_MODALITIES_BY_PROTOCOL

    assert "video" in WIRE_MODALITIES_BY_PROTOCOL["gemini"]
    assert "video" not in WIRE_MODALITIES_BY_PROTOCOL["openai"]
    assert "video" not in WIRE_MODALITIES_BY_PROTOCOL["anthropic"]
    assert "video/mp4" in GEMINI_VIDEO_MIMES
    assert "video/quicktime" in GEMINI_VIDEO_MIMES


def test_required_modalities_video_with_and_without_frames() -> None:
    video_only = Message.user("watch this", (VideoBlock(ASSET_B, "video/mp4"),))
    assert required_modalities_for_messages([video_only]) == ("text", "video")
    framed = Message.user(
        "watch this",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0)),
    )
    assert required_modalities_for_messages([framed]) == ("text", "image")


def test_collect_block_assets_includes_video_and_frames() -> None:
    message = Message.user(
        "watch",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0)),
    )
    requested = collect_block_assets([message])
    assert requested == {ASSET_B: "video/mp4", ASSET_A: "image/jpeg"}


class StaticResolver:
    def __init__(self, assets: dict[str, ResolvedAsset]) -> None:
        self.assets = assets

    def resolve(self, asset_id: str) -> ResolvedAsset | None:
        return self.assets.get(asset_id)


def _asset(asset_id: str, mime: str, data: bytes) -> ResolvedAsset:
    return ResolvedAsset(asset_id=asset_id, mime=mime, data=data, filename="asset")


@requires_ffmpeg
async def test_gemini_renders_native_video_and_skips_frames(
    video_with_audio: bytes,
) -> None:
    from eurus.providers.media_parts import GEMINI_IMAGE_MIMES, gemini_user_parts

    frame = extract_frame_images(video_with_audio, [1.0], max_dimension=64)
    assert frame
    assets = {
        ASSET_B: _asset(ASSET_B, "video/mp4", video_with_audio),
        ASSET_A: _asset(ASSET_A, "image/jpeg", frame[0][0]),
    }
    message = Message.user(
        "explain",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0)),
    )
    parts = gemini_user_parts(message, assets)
    assert parts is not None
    inline = [part for part in parts if "inline_data" in part]
    assert len(inline) == 1
    assert inline[0]["inline_data"]["mime_type"] == "video/mp4"
    assert base64.b64decode(inline[0]["inline_data"]["data"]) == video_with_audio
    resolved = await resolve_request_assets(
        [message],
        StaticResolver(assets),
        allowed_mimes=GEMINI_IMAGE_MIMES | GEMINI_VIDEO_MIMES,
        inline_bytes={"image/": 15 * 1024 * 1024, "video/": 10 * 1024 * 1024},
    )
    assert ASSET_B in resolved
    assert ASSET_A in resolved


async def test_gemini_falls_back_to_frames_when_video_too_large() -> None:
    from eurus.providers.media_parts import (
        GEMINI_IMAGE_MIMES,
        GEMINI_VIDEO_MIMES,
        gemini_user_parts,
    )

    big_video = b"\x00" * (11 * 1024 * 1024)
    frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    assets = {
        ASSET_B: _asset(ASSET_B, "video/mp4", big_video),
        ASSET_A: _asset(ASSET_A, "image/jpeg", frame),
    }
    message = Message.user(
        "explain",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 1.0)),
    )
    resolved = await resolve_request_assets(
        [message],
        StaticResolver(assets),
        allowed_mimes=GEMINI_IMAGE_MIMES | GEMINI_VIDEO_MIMES,
        inline_bytes={"image/": 15 * 1024 * 1024, "video/": 10 * 1024 * 1024},
    )
    assert ASSET_B not in resolved
    assert ASSET_A in resolved
    parts = gemini_user_parts(message, resolved)
    assert parts is not None
    inline = [part for part in parts if "inline_data" in part]
    assert len(inline) == 1
    assert inline[0]["inline_data"]["mime_type"] == "image/jpeg"
    labels = [part for part in parts if "text" in part and "keyframe" in part["text"]]
    assert labels == [{"text": "[keyframe at 1.0s]"}]


async def test_openai_renders_frames_not_raw_video() -> None:
    from eurus.providers.media_parts import OPENAI_IMAGE_MIMES, openai_content_parts

    frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    assets = {
        ASSET_B: _asset(ASSET_B, "video/mp4", b"video-bytes"),
        ASSET_A: _asset(ASSET_A, "image/jpeg", frame),
    }
    message = Message.user(
        "explain",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 2.5)),
    )
    resolved = await resolve_request_assets(
        [message], StaticResolver(assets), allowed_mimes=OPENAI_IMAGE_MIMES, inline_bytes={}
    )
    assert ASSET_B not in resolved
    assert ASSET_A in resolved
    parts = openai_content_parts(message, resolved)
    assert parts is not None
    image_parts = [part for part in parts if part["type"] == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    labels = [part for part in parts if part["type"] == "text" and "keyframe" in part["text"]]
    assert labels == [{"type": "text", "text": "[keyframe at 2.5s]"}]


async def test_anthropic_renders_frames_with_labels() -> None:
    from eurus.providers.media_parts import ANTHROPIC_IMAGE_MIMES, anthropic_user_content

    frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    assets = {ASSET_A: _asset(ASSET_A, "image/jpeg", frame)}
    message = Message.user(
        "explain",
        (VideoBlock(ASSET_B, "video/mp4"), VideoFrameBlock(ASSET_A, ASSET_B, "image/jpeg", 4.0)),
    )
    resolved = await resolve_request_assets(
        [message], StaticResolver(assets), allowed_mimes=ANTHROPIC_IMAGE_MIMES, inline_bytes={}
    )
    parts = anthropic_user_content(message, resolved)
    assert parts is not None
    images = [part for part in parts if part["type"] == "image"]
    assert len(images) == 1
    labels = [part for part in parts if part["type"] == "text" and "keyframe" in part["text"]]
    assert labels == [{"type": "text", "text": "[keyframe at 4.0s]"}]


async def test_router_video_without_frames_degrades_to_text_bridge(
    video_payload: bytes,
) -> None:
    from eurus.providers.media_parts import strip_message_blocks

    store_assets = {ASSET_B: _asset(ASSET_B, "video/mp4", video_payload)}
    message = Message.user("explain", (VideoBlock(ASSET_B, "video/mp4"),))
    resolved = await resolve_request_assets(
        [message],
        StaticResolver(store_assets),
        allowed_mimes=frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"}),
        inline_bytes={"image/": 20 * 1024 * 1024},
    )
    assert resolved == {}
    stripped = strip_message_blocks((Message.system("sys"), message))
    assert all(not entry.blocks for entry in stripped)
    assert stripped[-1].content == "explain"


@requires_ffmpeg
async def test_router_routes_framed_video_to_openai_vision(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    from eurus.providers.base import RetryPolicy
    from eurus.providers.openai_compat import OpenAICompatProvider
    from eurus.providers.router import Router
    from eurus.providers.testing import FakeTransport, RecordingSleep

    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "clip.mp4", video_with_audio)
    await run_processing_pipeline(store, media, processor, data=video_with_audio)
    refreshed = store.get(media.id)
    assert refreshed is not None
    from eurus.media.store import video_content_blocks

    blocks = video_content_blocks(refreshed)
    assert len(blocks) >= 2
    message = Message.user("explain this video", blocks)
    transport = FakeTransport(
        [
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "it shows a test pattern"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        ]
    )
    from eurus.catalog import BUILTIN_PROVIDERS

    provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["openai"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=MediaStoreAssetResolver(database=store.database, root=store.root),
    )
    router = Router({"openai": provider}, [ModelRef.parse("openai/gpt-4o")])
    response = await router.chat((Message.system("sys"), message))
    assert response.message.content == "it shows a test pattern"
    payload = transport.calls[0].payload
    user_wire = payload["messages"][-1]
    assert isinstance(user_wire["content"], list)
    image_parts = [part for part in user_wire["content"] if part["type"] == "image_url"]
    assert image_parts
    labels = [
        part
        for part in user_wire["content"]
        if part["type"] == "text" and "keyframe" in part["text"]
    ]
    assert labels


@requires_ffmpeg
async def test_transcribe_video_audio_with_existing_asset(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    from eurus.audio.pipeline import transcribe_video_audio
    from eurus.audio.stt import TranscriptionResult

    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("session-1", "clip.mp4", video_with_audio)
    await run_processing_pipeline(store, media, processor, data=video_with_audio)
    refreshed = store.get(media.id)
    assert refreshed is not None
    assert refreshed.metadata.get("audio_asset_id")

    class StubTranscriber:
        async def transcribe(
            self, data: bytes, mime: str, *, language: str | None = None
        ) -> TranscriptionResult:
            assert data[:4] == b"RIFF"
            assert mime == "audio/wav"
            return TranscriptionResult(
                text="transcribed words", language="id", duration_seconds=6.0
            )

    event_store = DomainEventStore(tmp_path / "events.sqlite")
    publisher = MultimodalEventPublisher(event_store, owner_id="owner-1")
    result = await transcribe_video_audio(store, refreshed, StubTranscriber(), events=publisher)
    assert result is not None
    assert result.text == "transcribed words"
    final = store.get(media.id)
    assert final is not None
    assert final.metadata.get("transcript") == "transcribed words"
    types = [event.event_type for event in event_store.list_events(limit=20)]
    assert "MEDIA_TRANSCRIPTION_STARTED" in types
    assert "MEDIA_TRANSCRIPTION_COMPLETED" in types


@requires_ffmpeg
async def test_transcribe_video_audio_lazy_extraction(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    from eurus.audio.pipeline import transcribe_video_audio
    from eurus.audio.stt import TranscriptionResult

    store = make_store(tmp_path)
    media = store.save("session-1", "clip.mp4", video_with_audio)
    store.mark_ready(
        media.id,
        {"duration_seconds": 6.0, "has_audio": True},
    )
    refreshed = store.get(media.id)
    assert refreshed is not None

    class StubTranscriber:
        async def transcribe(
            self, data: bytes, mime: str, *, language: str | None = None
        ) -> TranscriptionResult:
            return TranscriptionResult(text="lazy words")

    result = await transcribe_video_audio(store, refreshed, StubTranscriber())
    assert result is not None
    final = store.get(media.id)
    assert final is not None
    assert final.metadata.get("transcript") == "lazy words"
    assert final.metadata.get("audio_asset_id")


async def test_transcribe_video_audio_no_audio_track(tmp_path: Path) -> None:
    from eurus.audio.pipeline import transcribe_video_audio

    store = make_store(tmp_path)
    garbage = b"\x00" * 8 + b"ftypisom" + b"\x00" * 16
    media = store.save("session-1", "clip.mp4", garbage)
    store.mark_ready(media.id, {"has_audio": False})
    refreshed = store.get(media.id)
    assert refreshed is not None

    class FailingTranscriber:
        async def transcribe(self, data: bytes, mime: str, *, language: str | None = None) -> Any:
            raise AssertionError("transcriber must not be called without an audio track")

    result = await transcribe_video_audio(store, refreshed, FailingTranscriber())
    assert result is None


@requires_ffmpeg
async def test_telegram_video_ingest_produces_frame_blocks(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    from eurus.gateway.telegram.media import TelegramMediaIngester, TelegramMediaRef

    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, lambda file_id: _async_bytes(video_with_audio))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="vid1", kind_hint="video", mime_hint="video/mp4", filename="clip.mp4"
            )
        ],
    )
    assert outcome.ok
    files = list(outcome.files)
    assert files
    video_blocks = [block for block in outcome.blocks if isinstance(block, VideoBlock)]
    frame_blocks = [block for block in outcome.blocks if isinstance(block, VideoFrameBlock)]
    assert video_blocks
    assert frame_blocks
    assert all(block.source_asset_id == files[0].id for block in frame_blocks)
    objective = ingester.objective_text("ringkas video ini", outcome)
    assert "keyframe extraction is unavailable" not in objective


async def _async_bytes(data: bytes) -> bytes:
    await asyncio.sleep(0)
    return data


def test_video_summary() -> None:
    summary = video_summary(
        {
            "duration_seconds": 12.0,
            "width": 320,
            "height": 240,
            "frame_rate": 10.0,
            "has_audio": True,
            "audio_asset_id": ASSET_A,
            "frames": [{"asset_id": ASSET_B, "timestamp_seconds": 1.0}],
            "transcript": "words",
        }
    )
    assert "duration 12.0s" in summary
    assert "320x240" in summary
    assert "1 keyframes extracted" in summary
    assert "audio track extracted" in summary
    assert "transcript available" in summary


def test_media_limits_video_knobs() -> None:
    limits = MediaLimits.from_environ(
        {
            "EURUS_MEDIA_MAX_VIDEO_FRAMES": "3",
            "EURUS_VIDEO_FRAME_INTERVAL_SECONDS": "5",
            "EURUS_VIDEO_FRAME_MAX_DIMENSION": "256",
            "EURUS_VIDEO_SCENE_THRESHOLD": "0.5",
        }
    )
    assert limits.max_video_frames == 3
    assert limits.video_frame_interval_seconds == 5.0
    assert limits.video_frame_max_dimension == 256
    assert limits.video_scene_threshold == 0.5
    with pytest.raises(ValueError):
        MediaLimits(max_video_frames=0)
    with pytest.raises(ValueError):
        MediaLimits(video_frame_interval_seconds=0)
    with pytest.raises(ValueError):
        MediaLimits(video_scene_threshold=1.5)


def test_quicktime_video_is_accepted_by_store(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  mov-data"
    media = store.save("session-1", "clip.mov", mov, declared_mime="video/quicktime")
    assert media.mime == "video/quicktime"
    assert media.kind == "video"


@requires_ffmpeg
async def test_web_upload_video_metadata_has_frames(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    io_mod = io
    from fastapi.testclient import TestClient

    from eurus.config import load_config
    from eurus.gateway.web.app import build_context, create_app
    from eurus.gateway.web.auth import GatewaySecurity

    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer unit-test-token"}
        session_id = client.post("/api/sessions", json={"title": "s"}, headers=auth).json()["id"]
        response = client.post(
            "/api/media",
            data={"session_id": session_id},
            files={"file": ("clip.mp4", io_mod.BytesIO(video_with_audio), "video/mp4")},
            headers=auth,
        )
        assert response.status_code in (200, 201)
        body = response.json()
        assert body["kind"] == "video"
        metadata = body.get("metadata", {})
        assert metadata.get("frames")
        assert metadata.get("thumbnail_asset_id")
        assert metadata.get("duration_seconds")
    await ctx.runtime.stop()


@requires_ffmpeg
async def test_final_batch3_chain_video_to_blocks_to_routing(
    tmp_path: Path, video_with_audio: bytes
) -> None:
    from eurus.media.store import attachment_content_blocks

    store = make_store(tmp_path)
    processor = make_processor()
    media = store.save("telegram:1:10", "clip.mp4", video_with_audio, owner_id="owner-1")
    await run_processing_pipeline(store, media, processor, data=video_with_audio)
    refreshed = store.get(media.id)
    assert refreshed is not None
    blocks = attachment_content_blocks([refreshed])
    video_blocks = [block for block in blocks if isinstance(block, VideoBlock)]
    frame_blocks = [block for block in blocks if isinstance(block, VideoFrameBlock)]
    assert video_blocks and frame_blocks
    assert video_blocks[0].duration_seconds is not None
    from eurus.models import Message
    from eurus.providers.model_capabilities import required_modalities_for_messages

    message = Message.user("jelaskan video ini", blocks)
    assert required_modalities_for_messages([message]) == ("text", "image")
