from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from eurus.agent import Agent
from eurus.catalog import BUILTIN_PROVIDERS
from eurus.content import AudioBlock, ImageBlock
from eurus.events.multimodal import MultimodalEventPublisher
from eurus.events.store import DomainEventStore
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.router import GatewayRouter
from eurus.gateway.tasks import TaskStore
from eurus.gateway.telegram.media import (
    TelegramMediaIngester,
    TelegramMediaRef,
    deliver_voice_responses,
)
from eurus.media.store import MediaStore
from eurus.models import ModelRef
from eurus.providers.base import RetryPolicy
from eurus.providers.media_parts import MediaStoreAssetResolver
from eurus.providers.openai_compat import OpenAICompatProvider
from eurus.providers.router import Router
from eurus.providers.testing import FakeTransport, RecordingSleep, openai_stream
from eurus.tools.base import ToolContext, ToolRegistry

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)
OGG_BYTES = b"OggS\x00\x02opus-payload"
OPUS_REPLY = b"OggS\x00\x02tts-audio-reply"


class FakeMultipartTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post_multipart(self, url, headers, data, files):
        self.calls.append({"url": url, "data": dict(data), "files": dict(files)})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBinaryTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post_binary(self, url, headers, payload):
        self.calls.append({"url": url, "payload": dict(payload)})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class VoiceBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_voice(self, **kwargs: Any) -> None:
        self.calls.append(("send_voice", kwargs))

    async def send_audio(self, **kwargs: Any) -> None:
        self.calls.append(("send_audio", kwargs))


def test_batch2_full_multimodal_agent_flow(tmp_path: Path) -> None:
    database = tmp_path / "gateway.sqlite"
    media_store = MediaStore(database=database, root=tmp_path / "media")
    events = MultimodalEventPublisher(DomainEventStore(database))

    async def download(file_id: str) -> bytes:
        if file_id == "photo-1":
            return PNG_BYTES
        if file_id == "voice-1":
            return OGG_BYTES
        raise OSError("unknown file")

    transcriber = OpenAICompatTranscriberForTest(
        FakeMultipartTransport([{"text": "buatkan ringkasan laporan ini", "language": "id"}])
    )
    ingester = TelegramMediaIngester(
        media_store,
        download,
        events=events,
        transcriber=transcriber,
    )

    chat_transport = FakeTransport(
        streams=[
            openai_stream(
                "Laporan ini berisi ringkasan pendapatan kuartal. "
                "[voice:00000000000000000000000000000000]"
            )
        ]
    )
    resolver = MediaStoreAssetResolver(database=database, root=tmp_path / "media")
    chat_provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["openai"],
        "k",
        chat_transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    router = Router(
        {"openai": chat_provider},
        [ModelRef.parse("openai/gpt-4o")],
    )
    context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5.0)
    agent = Agent(router, ToolRegistry(), context, system_prompt="sys", max_steps=3)

    task_store = TaskStore(database)
    runner_blocks: list[dict[str, Any]] = []

    async def runner(task: Any) -> str:
        from eurus.content import blocks_from_dicts

        raw_blocks = task.state.get("blocks") or ()
        runner_blocks.extend(raw_blocks)
        blocks = blocks_from_dicts(raw_blocks) if raw_blocks else ()
        output: list[str] = []
        async for event in agent.run(task.objective, blocks=blocks):
            if event.kind == "text":
                output.append(event.text)
        answer = "".join(output)

        from eurus.audio.tts import OpenAICompatSpeechSynthesizer

        synthesizer = OpenAICompatSpeechSynthesizer(
            BUILTIN_PROVIDERS["openai"],
            "k",
            FakeBinaryTransport([OPUS_REPLY]),
            model="tts-1",
            default_format="opus",
        )
        from eurus.audio.pipeline import synthesize_speech

        media = await synthesize_speech(
            media_store,
            answer.replace("[voice:" + "0" * 32 + "]", ""),
            synthesizer,
            session_id="telegram:1:10",
            owner_id="1",
        )
        if media is None:
            return answer
        marker = f"[voice:{media.id}]"
        return f"{answer} {marker}"

    class RecordingAdapter:
        sent: list[tuple[str, str]] = []

        @property
        def platform(self) -> str:
            return "telegram"

        async def send_text(self, conversation_id: str, text: str) -> None:
            self.sent.append((conversation_id, text))

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    adapter = RecordingAdapter()
    manager = BackgroundTaskManager(task_store, runner, adapter.send_text, max_concurrent=1)
    gateway_router = GatewayRouter(
        adapter=adapter,
        access=lambda message: message.user_id == "1",
        store=task_store,
        manager=manager,
        broker=None,
    )

    async def scenario() -> None:
        photo_outcome = await ingester.ingest(
            "telegram:1:10",
            [TelegramMediaRef(file_id="photo-1", kind_hint="image", mime_hint="image/jpeg")],
            owner_id="1",
        )
        assert photo_outcome.ok
        voice_outcome = await ingester.ingest(
            "telegram:1:10",
            [TelegramMediaRef(file_id="voice-1", kind_hint="audio", mime_hint="audio/ogg")],
            owner_id="1",
        )
        assert voice_outcome.ok
        audio_block = voice_outcome.blocks[0]
        assert isinstance(audio_block, AudioBlock)
        assert audio_block.transcript == "buatkan ringkasan laporan ini"

        message = GatewayMessage(
            platform="telegram",
            user_id="1",
            conversation_id="10",
            message_id="700",
            text=ingester.objective_text("buatkan ringkasan laporan ini", voice_outcome),
            blocks=voice_outcome.blocks,
        )
        await gateway_router.handle(message)
        workers = list(manager.active.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        published = DomainEventStore(database).list_events(limit=30)
        types = {event.event_type for event in published}
        assert "MEDIA_RECEIVED" in types
        assert "AUDIO_RECEIVED" in types
        assert "MESSAGE_RECEIVED" not in types

        user_wire = chat_transport.calls[0].payload["messages"][1]["content"]
        assert isinstance(user_wire, str)
        assert "buatkan ringkasan laporan ini" in user_wire

        task = task_store.list_owner("1", 5)[0]
        assert runner_blocks[0]["type"] == "audio"

        from eurus.gateway.telegram.media import TelegramMediaSender

        bot = VoiceBot()
        sender = TelegramMediaSender(bot, media_store)
        final = task.state.get("result", "")
        cleaned = await deliver_voice_responses(10, str(final), media_store, sender)
        assert "[voice:" not in cleaned
        assert "ringkasan" in cleaned
        assert [method for method, _ in bot.calls] == ["send_voice"]
        await manager.shutdown()

    asyncio.run(scenario())


class OpenAICompatTranscriberForTest:
    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def transcribe(self, data: bytes, mime: str, *, language: str | None = None):
        from eurus.audio.stt import TranscriptionResult

        response = await self.transport.post_multipart(
            "https://api.openai.com/v1/audio/transcriptions",
            {},
            {},
            {"file": ("audio.ogg", data, mime)},
        )
        return TranscriptionResult(
            text=str(response.get("text", "")),
            language=str(response.get("language", "")),
        )


def test_vision_router_integrates_with_media_resolver(tmp_path: Path) -> None:
    store = MediaStore(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    asset_id = store.save("s", "photo.png", PNG_BYTES).id
    resolver = MediaStoreAssetResolver(database=tmp_path / "db.sqlite", root=tmp_path / "media")
    transport = FakeTransport(streams=[openai_stream("sebuah kotak merah kecil")])
    provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["openai"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        RecordingSleep(),
        asset_resolver=resolver,
    )
    router = Router({"openai": provider}, [ModelRef.parse("openai/gpt-4o")])
    context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5.0)
    agent = Agent(router, ToolRegistry(), context, system_prompt="sys", max_steps=3)
    block = ImageBlock(asset_id=asset_id, mime_type="image/png")

    async def scenario() -> list[str]:
        texts: list[str] = []
        async for event in agent.run("apa yang ada di gambar ini?", blocks=(block,)):
            if event.kind == "media_input_analyzing":
                texts.append(f"analyzing:{event.asset_id}:{event.media_kind}")
            elif event.kind == "text":
                texts.append(event.text)
        return texts

    texts = asyncio.run(scenario())
    assert f"analyzing:{asset_id}:image" in texts
    assert "sebuah kotak merah kecil" in "".join(texts)
    content = transport.calls[0].payload["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "apa yang ada di gambar ini?"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
