from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eurus.content import AudioBlock, DocumentBlock, ImageBlock, VideoBlock
from eurus.events.store import DomainEventStore
from eurus.events.types import (
    IMAGE_RECEIVED,
    MEDIA_PROCESSING_STARTED,
    MEDIA_RECEIVED,
    MESSAGE_RECEIVED,
)
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.router import ACCESS_DENIED, GatewayRouter
from eurus.gateway.tasks import DurableTask, TaskStore, scope_key
from eurus.gateway.telegram.media import (
    IngestOutcome,
    MediaGroupCollector,
    TelegramMediaIngester,
    TelegramMediaRef,
    TelegramMediaSender,
    extract_telegram_media,
)
from eurus.media.errors import MediaError
from eurus.media.store import MediaStore

Runner = Callable[[DurableTask], Awaitable[str]]

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)
OGG_BYTES = b"OggS\x00\x02hello-opus-payload-bytes"
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00mpeg-frame-data"
MP4_BYTES = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomvideo-data"
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF"


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "media.sqlite", root=tmp_path / "media")


def fake_downloader(sources: dict[str, bytes]) -> Callable[[str], Awaitable[bytes]]:
    async def download(file_id: str) -> bytes:
        if file_id not in sources:
            raise OSError("telegram download failed")
        return sources[file_id]

    return download


def photo_message(
    file_id: str = "photo-1",
    caption: str = "",
    user_id: int = 1,
    chat_id: int = 10,
    message_id: int = 100,
    media_group_id: str = "",
    file_size: int = 1024,
) -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, type="private"),
        message_id=message_id,
        text=None,
        caption=caption,
        media_group_id=media_group_id,
        photo=[SimpleNamespace(file_id=file_id, file_size=file_size, width=800, height=600)],
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        document=None,
    )


def media_message(field: str, payload: Any, **overrides: Any) -> Any:
    base = {
        "from_user": SimpleNamespace(id=overrides.pop("user_id", 1)),
        "chat": SimpleNamespace(id=overrides.pop("chat_id", 10), type="private"),
        "message_id": overrides.pop("message_id", 101),
        "text": None,
        "caption": overrides.pop("caption", ""),
        "media_group_id": overrides.pop("media_group_id", ""),
        "photo": None,
        "voice": None,
        "audio": None,
        "video": None,
        "video_note": None,
        "document": None,
    }
    base[field] = payload
    return SimpleNamespace(**base)


def test_extract_photo_picks_largest_size() -> None:
    message = SimpleNamespace(
        caption="",
        media_group_id="",
        photo=[
            SimpleNamespace(file_id="small", file_size=100),
            SimpleNamespace(file_id="large", file_size=900),
        ],
        voice=None,
        audio=None,
        video=None,
        video_note=None,
        document=None,
    )
    refs = extract_telegram_media(message)
    assert len(refs) == 1
    assert refs[0].file_id == "large"
    assert refs[0].kind_hint == "image"
    assert refs[0].mime_hint == "image/jpeg"


def test_extract_voice_note_and_video_and_document() -> None:
    voice = media_message(
        "voice",
        SimpleNamespace(file_id="v1", file_size=2048, duration=7),
    )
    assert extract_telegram_media(voice)[0].mime_hint == "audio/ogg"
    video = media_message(
        "video",
        SimpleNamespace(
            file_id="vid1", file_size=4096, mime_type="video/mp4", file_name="clip.mp4"
        ),
    )
    assert extract_telegram_media(video)[0].kind_hint == "video"
    note = media_message(
        "video_note",
        SimpleNamespace(file_id="vn1", file_size=4096),
    )
    assert extract_telegram_media(note)[0].mime_hint == "video/mp4"
    doc = media_message(
        "document",
        SimpleNamespace(file_id="d1", file_size=512, mime_type="image/png", file_name="shot.png"),
    )
    assert extract_telegram_media(doc)[0].kind_hint == "image"


async def test_ingest_photo_creates_image_block(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"photo-1": PNG_BYTES}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="photo-1", kind_hint="image", mime_hint="image/jpeg")],
        owner_id=scope_key("telegram", "1"),
    )
    assert outcome.ok
    assert len(outcome.blocks) == 1
    assert isinstance(outcome.blocks[0], ImageBlock)
    media = outcome.files[0]
    assert media.mime == "image/png"
    assert media.owner_id == "1"
    assert media.session_id == "telegram:1:10"
    assert media.status == "ready"
    assert store.get_owned(media.id, "2") is None
    assert store.get_owned(media.id, "1") is not None


async def test_ingest_voice_creates_audio_block(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"v1": OGG_BYTES}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="v1", kind_hint="audio", mime_hint="audio/ogg")],
    )
    assert outcome.ok
    block = outcome.blocks[0]
    assert isinstance(block, AudioBlock)
    assert block.mime_type == "audio/ogg"
    assert outcome.files[0].kind == "audio"


async def test_ingest_video_and_document(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(
        store,
        fake_downloader({"vid1": MP4_BYTES, "doc1": PDF_BYTES}),
    )
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="vid1", kind_hint="video", mime_hint="video/mp4", filename="clip.mp4"
            ),
            TelegramMediaRef(
                file_id="doc1", kind_hint="document", mime_hint="application/pdf", filename="r.pdf"
            ),
        ],
    )
    assert outcome.ok
    assert isinstance(outcome.blocks[0], VideoBlock)
    assert isinstance(outcome.blocks[1], DocumentBlock)


async def test_ingest_publishes_media_events(tmp_path: Path) -> None:
    from eurus.events.multimodal import MultimodalEventPublisher

    store = make_store(tmp_path)
    events = MultimodalEventPublisher(DomainEventStore(tmp_path / "media.sqlite"))
    ingester = TelegramMediaIngester(
        store,
        fake_downloader({"photo-1": PNG_BYTES}),
        events=events,
    )
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="photo-1", kind_hint="image", mime_hint="image/jpeg")],
    )
    assert outcome.ok
    published = DomainEventStore(tmp_path / "media.sqlite").list_events(limit=10)
    types = {event.event_type for event in published}
    assert MEDIA_RECEIVED in types
    assert IMAGE_RECEIVED in types
    assert MEDIA_PROCESSING_STARTED in types
    media_events = [
        event
        for event in published
        if event.subject_id == outcome.files[0].id
        and event.event_type in (MEDIA_RECEIVED, IMAGE_RECEIVED)
    ]
    assert len(media_events) == 2


async def test_ingest_download_failure_reports_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="missing", kind_hint="image", mime_hint="image/jpeg")],
    )
    assert not outcome.ok
    assert outcome.blocks == ()
    assert outcome.errors


async def test_ingest_invalid_file_reports_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(
        store,
        fake_downloader({"bad": b"PK\x03\x04not-an-ooxml-package"}),
    )
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="bad", kind_hint="document", mime_hint="application/pdf")],
    )
    assert not outcome.ok
    assert outcome.errors


async def test_ingest_oversize_rejected_before_download(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    requested: list[str] = []

    async def download(file_id: str) -> bytes:
        requested.append(file_id)
        return PNG_BYTES

    ingester = TelegramMediaIngester(store, download)
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="huge",
                kind_hint="image",
                mime_hint="image/jpeg",
                file_size=store.limits.max_bytes + 1,
            )
        ],
    )
    assert not outcome.ok
    assert requested == []


async def test_ingest_audio_file_mp3(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"a1": MP3_BYTES}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="a1",
                kind_hint="audio",
                mime_hint="audio/mpeg",
                filename="song.mp3",
            )
        ],
    )
    assert outcome.ok
    assert outcome.files[0].mime == "audio/mpeg"


async def test_ingest_timeout_is_reported(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    async def slow_download(file_id: str) -> bytes:
        await asyncio.sleep(0.4)
        return PNG_BYTES

    ingester = TelegramMediaIngester(store, slow_download, download_timeout=0.01)
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="slow", kind_hint="image", mime_hint="image/jpeg")],
    )
    assert not outcome.ok
    assert any("timed out" in error for error in outcome.errors)


def test_objective_text_combines_caption_and_markers(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"p": PNG_BYTES}))
    outcome = asyncio.run(
        ingester.ingest(
            "s",
            [TelegramMediaRef(file_id="p", kind_hint="image", mime_hint="image/jpeg")],
        )
    )
    objective = ingester.objective_text("describe this", outcome)
    assert objective.startswith("describe this")
    assert "[attach id=" in objective
    assert "kind=image" in objective
    bare = ingester.objective_text("", outcome)
    assert bare.startswith("\n[attach id=")


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def platform(self) -> str:
        return "telegram"

    async def send_text(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class GatewayRig:
    def __init__(self, tmp_path: Path, runner: Runner) -> None:
        self.adapter = RecordingAdapter()
        self.store = TaskStore(tmp_path / "tasks.sqlite3")
        self.manager = BackgroundTaskManager(
            self.store,
            runner,
            self.adapter.send_text,
            max_concurrent=1,
        )
        self.router = GatewayRouter(
            adapter=self.adapter,
            access=lambda message: message.user_id == "1",
            store=self.store,
            manager=self.manager,
            broker=None,
        )

    async def settle(self) -> None:
        workers = list(self.manager.active.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def close(self) -> None:
        await self.manager.shutdown()


async def test_router_forwards_blocks_into_task_state(tmp_path: Path) -> None:
    seen: list[DurableTask] = []

    async def runner(task: DurableTask) -> str:
        seen.append(task)
        return "ok"

    rig = GatewayRig(tmp_path, runner)
    image = ImageBlock(asset_id="a" * 32, mime_type="image/png")
    message = GatewayMessage(
        platform="telegram",
        user_id="1",
        conversation_id="10",
        message_id="555",
        text="explain the image\n\n[attach id=aaaa]",
        blocks=(image,),
    )
    await rig.router.handle(message)
    await rig.settle()
    await rig.close()
    assert len(seen) == 1
    task = seen[0]
    assert task.state["blocks"] == [
        {"type": "image", "asset_id": "a" * 32, "mime_type": "image/png"}
    ]
    assert "[attach id=aaaa]" in task.objective


async def test_router_rejects_unauthorized_media_user(tmp_path: Path) -> None:
    seen: list[DurableTask] = []

    async def runner(task: DurableTask) -> str:
        seen.append(task)
        return "ok"

    rig = GatewayRig(tmp_path, runner)
    message = GatewayMessage(
        platform="telegram",
        user_id="999",
        conversation_id="10",
        message_id="556",
        text="explain\n\n[attach id=aaaa]",
        blocks=(ImageBlock(asset_id="a" * 32, mime_type="image/png"),),
    )
    await rig.router.handle(message)
    await rig.settle()
    await rig.close()
    assert seen == []
    assert rig.adapter.sent and rig.adapter.sent[0][1] == ACCESS_DENIED


async def test_duplicate_update_is_ignored(tmp_path: Path) -> None:
    seen: list[DurableTask] = []

    async def runner(task: DurableTask) -> str:
        seen.append(task)
        return "ok"

    rig = GatewayRig(tmp_path, runner)
    message = GatewayMessage(
        platform="telegram",
        user_id="1",
        conversation_id="10",
        message_id="557",
        text="hello",
    )
    await rig.router.handle(message)
    await rig.router.handle(message)
    await rig.settle()
    await rig.close()
    assert len(seen) == 1


async def test_media_only_message_without_text_is_processed(tmp_path: Path) -> None:
    seen: list[DurableTask] = []

    async def runner(task: DurableTask) -> str:
        seen.append(task)
        return "ok"

    rig = GatewayRig(tmp_path, runner)
    message = GatewayMessage(
        platform="telegram",
        user_id="1",
        conversation_id="10",
        message_id="558",
        text="[attach id=bbbb kind=image]",
        blocks=(ImageBlock(asset_id="b" * 32, mime_type="image/jpeg"),),
    )
    await rig.router.handle(message)
    await rig.settle()
    await rig.close()
    assert len(seen) == 1


async def test_media_group_collector_batches_album(tmp_path: Path) -> None:
    flushed: list[tuple[int, str, list[TelegramMediaRef], str]] = []

    async def flush(
        chat_id: int, group_id: str, refs: list[TelegramMediaRef], caption: str
    ) -> None:
        flushed.append((chat_id, group_id, refs, caption))

    collector = MediaGroupCollector(flush, window_seconds=0.05)
    for index in range(3):
        await collector.add(
            10,
            "group-1",
            TelegramMediaRef(
                file_id=f"p{index}",
                kind_hint="image",
                mime_hint="image/jpeg",
                caption="album caption" if index == 0 else "",
                media_group_id="group-1",
            ),
        )
    await asyncio.sleep(0.2)
    assert len(flushed) == 1
    chat_id, group_id, refs, caption = flushed[0]
    assert chat_id == 10
    assert group_id == "group-1"
    assert len(refs) == 3
    assert caption == "album caption"


async def test_media_group_collector_flushes_at_capacity() -> None:
    flushed: list[tuple[int, str, list[TelegramMediaRef], str]] = []

    async def flush(
        chat_id: int, group_id: str, refs: list[TelegramMediaRef], caption: str
    ) -> None:
        flushed.append((chat_id, group_id, refs, caption))

    collector = MediaGroupCollector(flush, window_seconds=5.0, max_items=2)
    await collector.add(10, "g", TelegramMediaRef(file_id="a", kind_hint="image", caption="one"))
    await collector.add(10, "g", TelegramMediaRef(file_id="b", kind_hint="image", caption=""))
    assert len(flushed) == 1
    assert len(flushed[0][2]) == 2


class FakeBot:
    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures
        self.error = error

    async def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise self.error or OSError("network down")
        self.calls.append((method, kwargs))

    async def send_photo(self, **kwargs: Any) -> None:
        await self._record("send_photo", kwargs)

    async def send_voice(self, **kwargs: Any) -> None:
        await self._record("send_voice", kwargs)

    async def send_audio(self, **kwargs: Any) -> None:
        await self._record("send_audio", kwargs)

    async def send_video(self, **kwargs: Any) -> None:
        await self._record("send_video", kwargs)

    async def send_document(self, **kwargs: Any) -> None:
        await self._record("send_document", kwargs)


async def test_sender_dispatches_by_mime(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    voice = store.save("s", "voice.ogg", OGG_BYTES)
    song = store.save("s", "song.mp3", MP3_BYTES)
    clip = store.save("s", "clip.mp4", MP4_BYTES)
    report = store.save("s", "report.pdf", PDF_BYTES)
    bot = FakeBot()
    sender = TelegramMediaSender(bot, store)
    await sender.send_media(10, photo, caption="look")
    await sender.send_media(10, voice)
    await sender.send_media(10, song)
    await sender.send_media(10, clip)
    await sender.send_media(10, report, caption="report")
    methods = [method for method, _ in bot.calls]
    assert methods == [
        "send_photo",
        "send_voice",
        "send_audio",
        "send_video",
        "send_document",
    ]
    assert bot.calls[0][1]["chat_id"] == 10
    assert bot.calls[0][1]["caption"] == "look"
    assert bot.calls[1][1]["caption"] is None


async def test_sender_retries_network_errors(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    bot = FakeBot(failures=1)
    sender = TelegramMediaSender(bot, store)
    await sender.send_media(10, photo)
    assert len(bot.calls) == 1


async def test_sender_raises_after_retries_exhausted(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    bot = FakeBot(failures=9)
    sender = TelegramMediaSender(bot, store, max_attempts=2)
    with pytest.raises(OSError):
        await sender.send_media(10, photo)
    assert bot.calls == []


async def test_sender_non_retryable_error_is_not_retried(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    bot = FakeBot(failures=9, error=RuntimeError("bad request"))
    sender = TelegramMediaSender(bot, store, max_attempts=3)
    with pytest.raises(RuntimeError):
        await sender.send_media(10, photo)
    assert bot.calls == []


async def test_sender_rejects_oversized_upload(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    bot = FakeBot()
    sender = TelegramMediaSender(bot, store, upload_limit_bytes=4)
    with pytest.raises(MediaError):
        await sender.send_media(10, photo)
    assert bot.calls == []


async def test_sender_missing_file_raises(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    photo = store.save("s", "photo.png", PNG_BYTES)
    store.delete(photo.id)
    bot = FakeBot()
    sender = TelegramMediaSender(bot, store)
    with pytest.raises(MediaError):
        await sender.send_media(10, photo)
    assert bot.calls == []


async def test_full_incoming_media_flow_to_gateway_message(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    from eurus.events.multimodal import MultimodalEventPublisher

    events = MultimodalEventPublisher(DomainEventStore(tmp_path / "media.sqlite"))
    ingester = TelegramMediaIngester(
        store,
        fake_downloader({"photo-9": PNG_BYTES}),
        events=events,
    )
    message = photo_message(file_id="photo-9", caption="jelaskan gambar ini", message_id=900)
    refs = extract_telegram_media(message)
    session_id = "telegram:1:10"
    outcome = await ingester.ingest(session_id, refs, owner_id=scope_key("telegram", "1"))
    assert outcome.ok

    seen: list[DurableTask] = []

    async def runner(task: DurableTask) -> str:
        seen.append(task)
        return "ok"

    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    adapter = RecordingAdapter()
    manager = BackgroundTaskManager(task_store, runner, adapter.send_text, max_concurrent=1)
    router = GatewayRouter(
        adapter=adapter,
        access=lambda m: m.user_id == "1",
        store=task_store,
        manager=manager,
        broker=None,
    )
    await router.handle(
        GatewayMessage(
            platform="telegram",
            user_id="1",
            conversation_id="10",
            message_id="900",
            text=ingester.objective_text("jelaskan gambar ini", outcome),
            blocks=outcome.blocks,
        )
    )
    workers = list(manager.active.values())
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    await manager.shutdown()
    assert len(seen) == 1
    task = seen[0]
    stored_blocks = task.state["blocks"]
    assert stored_blocks[0]["type"] == "image"
    assert stored_blocks[0]["asset_id"] == outcome.files[0].id
    published = DomainEventStore(tmp_path / "media.sqlite").list_events(limit=20)
    types = {event.event_type for event in published}
    assert MESSAGE_RECEIVED not in types


def test_gateway_message_blocks_default_empty() -> None:
    message = GatewayMessage(
        platform="telegram",
        user_id="1",
        conversation_id="10",
        message_id="1",
        text="hello",
    )
    assert message.blocks == ()


async def test_video_objective_includes_unavailable_note(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"vid1": MP4_BYTES}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="vid1", kind_hint="video", mime_hint="video/mp4", filename="clip.mp4"
            )
        ],
    )
    objective = ingester.objective_text("ringkas video ini", outcome)
    assert "keyframe extraction is unavailable" in objective
    assert "kind=video" in objective


async def test_video_objective_omits_note_when_frames_exist(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ingester = TelegramMediaIngester(store, fake_downloader({"vid1": MP4_BYTES}))
    outcome = await ingester.ingest(
        "telegram:1:10",
        [
            TelegramMediaRef(
                file_id="vid1", kind_hint="video", mime_hint="video/mp4", filename="clip.mp4"
            )
        ],
    )
    files = list(outcome.files)
    assert files
    store.mark_ready(files[0].id, {"frames": [{"asset_id": "a" * 32, "timestamp_seconds": 0.0}]})
    refreshed = store.get(files[0].id)
    assert refreshed is not None
    outcome = IngestOutcome(blocks=outcome.blocks, files=(refreshed,), errors=outcome.errors)
    objective = ingester.objective_text("ringkas video ini", outcome)
    assert "keyframe extraction is unavailable" not in objective
    assert "kind=video" in objective
