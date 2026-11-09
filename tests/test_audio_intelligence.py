from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from eurus.audio.config import AudioConfig
from eurus.audio.pipeline import synthesize_speech, transcribe_media
from eurus.audio.speak_tool import SPEAK_TOOL_NAME, speak_tool
from eurus.audio.stt import (
    OpenAICompatTranscriber,
    TranscriptionError,
    build_transcriber,
)
from eurus.audio.tts import (
    OpenAICompatSpeechSynthesizer,
    SpeechSynthesisError,
    build_speech_synthesizer,
)
from eurus.catalog import BUILTIN_PROVIDERS
from eurus.config import build_config
from eurus.errors import ConfigError, ProviderError, ProviderUnavailableError
from eurus.events.multimodal import MultimodalEventPublisher
from eurus.events.store import DomainEventStore
from eurus.events.types import (
    MEDIA_PROCESSING_FAILED,
    MEDIA_TRANSCRIPTION_COMPLETED,
    MEDIA_TRANSCRIPTION_STARTED,
)
from eurus.gateway.telegram.media import (
    TelegramMediaIngester,
    TelegramMediaRef,
    deliver_voice_responses,
)
from eurus.media.store import MediaStore
from eurus.providers.httpx_transport import HttpxTransport
from eurus.providers.transport import HttpError
from eurus.tools.base import ToolContext

OGG_BYTES = b"OggS\x00\x02opus-audio-payload"
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00mpeg-frame"


class FakeMultipartTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post_multipart(self, url, headers, data, files):
        self.calls.append(
            {"url": url, "headers": dict(headers), "data": dict(data), "files": dict(files)}
        )
        if not self.responses:
            raise AssertionError("no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBinaryTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post_binary(self, url, headers, payload):
        self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        if not self.responses:
            raise AssertionError("no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_transcriber(transport: Any, *, language: str = "") -> OpenAICompatTranscriber:
    return OpenAICompatTranscriber(
        BUILTIN_PROVIDERS["openai"],
        "k",
        transport,
        model="whisper-1",
        language=language,
    )


def make_synthesizer(
    transport: Any, *, voice: str = "alloy", audio_format: str = "mp3"
) -> OpenAICompatSpeechSynthesizer:
    return OpenAICompatSpeechSynthesizer(
        BUILTIN_PROVIDERS["openai"],
        "k",
        transport,
        model="tts-1",
        default_voice=voice,
        default_format=audio_format,
    )


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "media.sqlite", root=tmp_path / "media")


async def test_transcriber_sends_multipart_request() -> None:
    transport = FakeMultipartTransport([{"text": "halo dunia", "language": "id"}])
    transcriber = make_transcriber(transport)
    result = await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert result.text == "halo dunia"
    assert result.language == "id"
    assert result.model == "whisper-1"
    call = transport.calls[0]
    assert call["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert call["data"]["model"] == "whisper-1"
    assert "language" not in call["data"]
    filename, payload, content_type = call["files"]["file"]
    assert filename == "audio.ogg"
    assert payload == OGG_BYTES
    assert content_type == "audio/ogg"
    assert call["headers"]["Authorization"] == "Bearer k"


async def test_transcriber_language_passthrough() -> None:
    transport = FakeMultipartTransport([{"text": "good morning"}])
    transcriber = make_transcriber(transport, language="en")
    result = await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert result.text == "good morning"
    assert transport.calls[0]["data"]["language"] == "en"


async def test_transcriber_english_explicit_language() -> None:
    transport = FakeMultipartTransport([{"text": "english speech"}])
    transcriber = make_transcriber(transport)
    await transcriber.transcribe(MP3_BYTES, "audio/mpeg", language="en")
    assert transport.calls[0]["data"]["language"] == "en"


async def test_transcriber_parses_verbose_duration() -> None:
    transport = FakeMultipartTransport(
        [{"text": "long recording", "language": "id", "duration": 12.5}]
    )
    transcriber = make_transcriber(transport)
    result = await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert result.duration_seconds == 12.5


async def test_transcriber_rejects_unsupported_format() -> None:
    transport = FakeMultipartTransport([])
    transcriber = make_transcriber(transport)
    with pytest.raises(TranscriptionError, match="unsupported audio format"):
        await transcriber.transcribe(b"data", "audio/x-custom")


async def test_transcriber_rejects_empty_and_oversize() -> None:
    transcriber = make_transcriber(FakeMultipartTransport([]))
    with pytest.raises(TranscriptionError, match="empty"):
        await transcriber.transcribe(b"", "audio/ogg")
    small = OpenAICompatTranscriber(
        BUILTIN_PROVIDERS["openai"],
        "k",
        FakeMultipartTransport([]),
        model="whisper-1",
        max_bytes=8,
    )
    with pytest.raises(TranscriptionError, match="limit"):
        await small.transcribe(b"x" * 16, "audio/ogg")


async def test_transcriber_retries_provider_errors() -> None:
    transport = FakeMultipartTransport([HttpError(500, "down"), {"text": "recovered"}])
    transcriber = make_transcriber(transport)
    result = await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert result.text == "recovered"
    assert len(transport.calls) == 2


async def test_transcriber_gives_up_after_retries() -> None:
    transport = FakeMultipartTransport(
        [HttpError(500, "down"), HttpError(500, "down"), HttpError(500, "down")]
    )
    transcriber = make_transcriber(transport)
    with pytest.raises(ProviderError):
        await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert len(transport.calls) == 3


async def test_transcriber_non_retryable_error_raises_once() -> None:
    transport = FakeMultipartTransport([HttpError(401, "bad key")])
    transcriber = make_transcriber(transport)
    with pytest.raises(ProviderError):
        await transcriber.transcribe(OGG_BYTES, "audio/ogg")
    assert len(transport.calls) == 1


async def test_transcriber_maps_transport_errors() -> None:
    from eurus.providers.transport import TransportError

    transport = FakeMultipartTransport(
        [TransportError("network error"), TransportError("network error"), TransportError("x")]
    )
    transcriber = make_transcriber(transport)
    with pytest.raises(ProviderUnavailableError):
        await transcriber.transcribe(OGG_BYTES, "audio/ogg")


async def test_synthesizer_request_shape() -> None:
    transport = FakeBinaryTransport([b"mp3-bytes"])
    synthesizer = make_synthesizer(transport, voice="nova")
    result = await synthesizer.synthesize("jawab pakai suara", audio_format="opus")
    assert result.data == b"mp3-bytes"
    assert result.mime == "audio/ogg"
    call = transport.calls[0]
    assert call["url"] == "https://api.openai.com/v1/audio/speech"
    assert call["payload"] == {
        "model": "tts-1",
        "input": "jawab pakai suara",
        "voice": "nova",
        "response_format": "opus",
    }


async def test_synthesizer_output_format_mimes() -> None:
    transport = FakeBinaryTransport([b"a", b"b", b"c"])
    synthesizer = make_synthesizer(transport)
    mp3 = await synthesizer.synthesize("one")
    wav = await synthesizer.synthesize("two", audio_format="wav")
    flac = await synthesizer.synthesize("three", audio_format="flac")
    assert mp3.mime == "audio/mpeg"
    assert wav.mime == "audio/wav"
    assert flac.mime == "audio/flac"


async def test_synthesizer_validates_input() -> None:
    synthesizer = make_synthesizer(FakeBinaryTransport([]))
    with pytest.raises(SpeechSynthesisError, match="empty"):
        await synthesizer.synthesize("   ")
    with pytest.raises(SpeechSynthesisError, match="character limit"):
        await synthesizer.synthesize("x" * 5000)
    with pytest.raises(SpeechSynthesisError, match="format"):
        await synthesizer.synthesize("hello", audio_format="pcm")


async def test_synthesizer_rejects_empty_response() -> None:
    synthesizer = make_synthesizer(FakeBinaryTransport([b""]))
    with pytest.raises(SpeechSynthesisError, match="empty"):
        await synthesizer.synthesize("hello")


async def test_synthesizer_retries_then_raises() -> None:
    from eurus.providers.transport import TransportError

    transport = FakeBinaryTransport([TransportError("net"), b"ok"])
    synthesizer = make_synthesizer(transport)
    result = await synthesizer.synthesize("hello")
    assert result.data == b"ok"
    assert len(transport.calls) == 2
    failing = FakeBinaryTransport(
        [TransportError("net"), TransportError("net"), TransportError("net")]
    )
    synthesizer2 = make_synthesizer(failing)
    with pytest.raises(ProviderUnavailableError):
        await synthesizer2.synthesize("hello")


def config_with_audio(tmp_path: Path, audio_toml: str) -> Any:
    raw = {
        "providers": {
            "openai": {
                "protocol": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            },
            "deepseek": {
                "protocol": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_KEY",
            },
        },
        "agent": {"model": "deepseek/deepseek-chat"},
    }
    import tomllib

    audio = tomllib.loads(audio_toml)
    raw["audio"] = audio
    return build_config(raw, environ={})


def test_build_transcriber_requires_declared_capability(tmp_path: Path) -> None:
    config = config_with_audio(tmp_path, 'stt_model = "deepseek/deepseek-chat"')
    with pytest.raises(ConfigError, match="transcription support"):
        build_transcriber(config)


def test_build_transcriber_unknown_provider(tmp_path: Path) -> None:
    config = config_with_audio(tmp_path, 'stt_model = "nope/whisper-1"')
    with pytest.raises(ConfigError, match="unknown provider"):
        build_transcriber(config)


def test_build_transcriber_rejects_non_openai_protocol(tmp_path: Path) -> None:
    raw = {
        "providers": {
            "goog": {
                "protocol": "gemini",
                "base_url": "https://x",
                "api_key_env": "G_KEY",
            }
        },
        "agent": {"model": "goog/gemini-2.0"},
        "audio": {"stt_model": "goog/gemini-2.0"},
    }
    config = build_config(raw, environ={})
    with pytest.raises(ConfigError, match="OpenAI-compatible"):
        build_transcriber(config)


def test_build_transcriber_returns_none_without_model(tmp_path: Path) -> None:
    config = config_with_audio(tmp_path, "")
    assert build_transcriber(config) is None
    assert build_speech_synthesizer(config) is None


def test_build_transcriber_constructs_client(tmp_path: Path) -> None:
    config = config_with_audio(tmp_path, 'stt_model = "openai/whisper-1"')
    transcriber = build_transcriber(config)
    assert transcriber is not None
    assert transcriber.model == "whisper-1"


def test_build_speech_synthesizer_capability_mismatch(tmp_path: Path) -> None:
    config = config_with_audio(tmp_path, 'tts_model = "openai/whisper-1"')
    with pytest.raises(ConfigError, match="speech synthesis support"):
        build_speech_synthesizer(config)


def test_build_speech_synthesizer_constructs_client(tmp_path: Path) -> None:
    config = config_with_audio(
        tmp_path, 'tts_model = "openai/tts-1"\ntts_voice = "shimmer"\ntts_format = "opus"'
    )
    synthesizer = build_speech_synthesizer(config)
    assert synthesizer is not None
    assert synthesizer.default_voice == "shimmer"
    assert synthesizer.default_format == "opus"


def test_audio_config_validation() -> None:
    with pytest.raises(ValueError):
        AudioConfig(tts_format="pcm")
    with pytest.raises(ValueError):
        AudioConfig(stt_timeout_seconds=0)
    with pytest.raises(ValueError):
        AudioConfig(tts_voice=" ")


def test_audio_config_section_parsing(tmp_path: Path) -> None:
    config = config_with_audio(
        tmp_path,
        'stt_model = "openai/whisper-1"\nstt_language = "id"\n'
        'tts_model = "openai/tts-1"\ntts_voice = "alloy"\ntts_format = "opus"',
    )
    assert config.audio.stt_model == "openai/whisper-1"
    assert config.audio.stt_language == "id"
    assert config.audio.tts_format == "opus"
    with pytest.raises(ConfigError):
        config_with_audio(tmp_path, "unknown_key = 1")


async def test_transcribe_media_stores_transcript_and_events(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "note.ogg", OGG_BYTES, declared_mime="audio/ogg")
    events = MultimodalEventPublisher(DomainEventStore(tmp_path / "media.sqlite"))
    transport = FakeMultipartTransport([{"text": "selamat pagi", "language": "id"}])
    transcriber = make_transcriber(transport)
    result = await transcribe_media(store, media, transcriber, events=events)
    assert result is not None
    assert result.text == "selamat pagi"
    stored = store.get(media.id)
    assert stored is not None
    assert stored.metadata["transcript"] == "selamat pagi"
    assert stored.metadata["transcript_language"] == "id"
    published = DomainEventStore(tmp_path / "media.sqlite").list_events(limit=10)
    types = [event.event_type for event in published]
    assert MEDIA_TRANSCRIPTION_STARTED in types
    assert MEDIA_TRANSCRIPTION_COMPLETED in types
    completed = next(
        event for event in published if event.event_type == MEDIA_TRANSCRIPTION_COMPLETED
    )
    assert completed.payload["text"] == "selamat pagi"
    assert completed.payload["language"] == "id"


async def test_transcribe_media_failure_emits_failed_event(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "note.ogg", OGG_BYTES, declared_mime="audio/ogg")
    events = MultimodalEventPublisher(DomainEventStore(tmp_path / "media.sqlite"))
    transport = FakeMultipartTransport([HttpError(401, "bad key")])
    transcriber = make_transcriber(transport)
    result = await transcribe_media(store, media, transcriber, events=events)
    assert result is None
    published = DomainEventStore(tmp_path / "media.sqlite").list_events(limit=10)
    types = [event.event_type for event in published]
    assert MEDIA_TRANSCRIPTION_STARTED in types
    assert MEDIA_PROCESSING_FAILED in types
    failed = next(event for event in published if event.event_type == MEDIA_PROCESSING_FAILED)
    assert failed.payload["stage"] == "transcription"


async def test_transcribe_media_ignores_non_audio(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "photo.png", b"\x89PNG\r\n\x1a\nrest", declared_mime="image/png")
    result = await transcribe_media(store, media, make_transcriber(FakeMultipartTransport([])))
    assert result is None


async def test_synthesize_speech_stores_asset(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    transport = FakeBinaryTransport([b"opus-voice-bytes"])
    synthesizer = make_synthesizer(transport, audio_format="opus")
    media = await synthesize_speech(
        store,
        "bacakan jawabanmu",
        synthesizer,
        session_id="telegram:1:10",
        owner_id="1",
    )
    assert media is not None
    assert media.mime == "audio/ogg"
    assert media.session_id == "telegram:1:10"
    assert media.owner_id == "1"
    assert media.filename == "speech.ogg"


async def test_synthesize_speech_failure_returns_none(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    synthesizer = make_synthesizer(FakeBinaryTransport([]))
    media = await synthesize_speech(store, "hello", synthesizer, session_id="s", audio_format="bad")
    assert media is None


async def test_speak_tool_returns_marker(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    transport = FakeBinaryTransport([b"voice-bytes"])
    synthesizer = make_synthesizer(transport, audio_format="opus")
    tool = speak_tool(store, synthesizer, default_format="opus")
    assert tool.name == SPEAK_TOOL_NAME
    context = ToolContext(
        workspace=tmp_path,
        session_id="telegram:1:10",
        owner_id="1",
    )
    emitted: list[tuple[str, dict[str, Any]]] = []
    context = ToolContext(
        workspace=tmp_path,
        session_id="telegram:1:10",
        owner_id="1",
        event_sink=lambda event_type, payload: emitted.append((event_type, payload)),
    )
    output = await tool.handler({"text": "ini jawaban saya"}, context)
    assert output.startswith("[voice:")
    assert "audio/ogg" in output
    assert emitted and emitted[0][0] == "audio.output.ready"
    asset_id = output.split("]")[0].removeprefix("[voice:")
    media = store.get(asset_id)
    assert media is not None
    assert media.size == len(b"voice-bytes")


async def test_speak_tool_failure_message(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    synthesizer = make_synthesizer(FakeBinaryTransport([]))
    tool = speak_tool(store, synthesizer)
    context = ToolContext(workspace=tmp_path, session_id="s")
    output = await tool.handler({"text": "hello"}, context)
    assert output.startswith("speech synthesis failed")


async def test_ingester_transcribes_voice_notes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    events = MultimodalEventPublisher(DomainEventStore(tmp_path / "media.sqlite"))
    transport = FakeMultipartTransport([{"text": "tolong ringkas laporan ini", "language": "id"}])

    async def download(file_id: str) -> bytes:
        return OGG_BYTES

    ingester = TelegramMediaIngester(
        store,
        download,
        events=events,
        transcriber=make_transcriber(transport),
    )
    outcome = await ingester.ingest(
        "telegram:1:10",
        [TelegramMediaRef(file_id="v1", kind_hint="audio", mime_hint="audio/ogg")],
    )
    assert outcome.ok
    block = outcome.blocks[0]
    assert block.transcript == "tolong ringkas laporan ini"
    objective = ingester.objective_text("tolong ringkas", outcome)
    assert "begin transcript of audio-v1" in objective
    assert "tolong ringkas laporan ini" in objective


class VoiceBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_voice(self, **kwargs: Any) -> None:
        self.calls.append(("send_voice", kwargs))

    async def send_audio(self, **kwargs: Any) -> None:
        self.calls.append(("send_audio", kwargs))

    async def send_document(self, **kwargs: Any) -> None:
        self.calls.append(("send_document", kwargs))


async def test_deliver_voice_responses_sends_and_strips_markers(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import TelegramMediaSender

    store = make_store(tmp_path)
    transport = FakeBinaryTransport([b"opus-bytes"])
    synthesizer = make_synthesizer(transport, audio_format="opus")
    media = await synthesize_speech(
        store, "jawaban suara", synthesizer, session_id="telegram:1:10", owner_id="1"
    )
    assert media is not None
    bot = VoiceBot()
    sender = TelegramMediaSender(bot, store)
    answer = f"Berikut penjelasannya.\n\n[voice:{media.id}]"
    cleaned = await deliver_voice_responses(10, answer, store, sender)
    assert cleaned == "Berikut penjelasannya."
    assert [method for method, _ in bot.calls] == ["send_voice"]
    assert bot.calls[0][1]["chat_id"] == 10


async def test_deliver_voice_responses_uses_audio_for_non_ogg(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import TelegramMediaSender

    store = make_store(tmp_path)
    transport = FakeBinaryTransport([b"mp3-bytes"])
    synthesizer = make_synthesizer(transport, audio_format="mp3")
    media = await synthesize_speech(store, "jawaban", synthesizer, session_id="s")
    assert media is not None
    bot = VoiceBot()
    sender = TelegramMediaSender(bot, store)
    answer = f"[voice:{media.id}]"
    cleaned = await deliver_voice_responses(10, answer, store, sender)
    assert cleaned == "Voice response delivered."
    assert [method for method, _ in bot.calls] == ["send_audio"]


async def test_deliver_voice_responses_without_markers_is_noop(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import TelegramMediaSender

    store = make_store(tmp_path)
    bot = VoiceBot()
    sender = TelegramMediaSender(bot, store)
    cleaned = await deliver_voice_responses(10, "plain answer", store, sender)
    assert cleaned == "plain answer"
    assert bot.calls == []


async def test_deliver_voice_responses_ignores_missing_assets(tmp_path: Path) -> None:
    from eurus.gateway.telegram.media import TelegramMediaSender

    store = make_store(tmp_path)
    bot = VoiceBot()
    sender = TelegramMediaSender(bot, store)
    cleaned = await deliver_voice_responses(10, "text [voice:" + "a" * 32 + "]", store, sender)
    assert cleaned == "text"
    assert bot.calls == []


async def test_transport_post_binary_and_multipart() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=b"audio-data")
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "transcribed"})
        return httpx.Response(404)

    transport = HttpxTransport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    binary = await transport.post_binary(
        "https://api.openai.com/v1/audio/speech", {"Authorization": "Bearer k"}, {"model": "tts-1"}
    )
    assert binary == b"audio-data"
    multipart = await transport.post_multipart(
        "https://api.openai.com/v1/audio/transcriptions",
        {"Authorization": "Bearer k"},
        {"model": "whisper-1"},
        {"file": ("audio.ogg", OGG_BYTES, "audio/ogg")},
    )
    assert multipart == {"text": "transcribed"}
    await transport.aclose()


async def test_transport_binary_error_mapping() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    transport = HttpxTransport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(HttpError) as excinfo:
        await transport.post_binary("https://api.openai.com/v1/x", {}, {})
    assert excinfo.value.status == 500
    await transport.aclose()


async def test_audio_blocks_carry_transcript_for_agents(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s", "note.ogg", OGG_BYTES, declared_mime="audio/ogg")
    store.update_status(media.id, "ready", metadata={"transcript": "halo", "duration_seconds": 4.2})
    fresh = store.get(media.id)
    assert fresh is not None
    blocks = store_blocks(fresh)
    assert blocks[0].transcript == "halo"
    assert blocks[0].duration_seconds == 4.2


def store_blocks(media: Any) -> tuple[Any, ...]:
    from eurus.media.store import attachment_content_blocks

    return attachment_content_blocks([media])
