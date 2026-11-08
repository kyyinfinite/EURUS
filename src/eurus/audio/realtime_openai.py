from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from eurus.catalog import ProviderSpec, resolve_api_key
from eurus.errors import ConfigError, ModelRefError, ProviderError, ProviderUnavailableError
from eurus.models import ModelRef
from eurus.providers.model_capabilities import CapabilityRegistry, default_registry

log = logging.getLogger(__name__)

REALTIME_SAMPLE_RATE = 24_000
REALTIME_AUDIO_FORMAT = "pcm16"
REALTIME_INPUT_AUDIO_MIME = "audio/pcm;rate=24000"
MAX_INSTRUCTION_CHARS = 24_000
MAX_TOOL_RESULT_CHARS = 20_000

OPENAI_REALTIME_PROTOCOL = "openai-realtime"


class RealtimeSessionError(ProviderError):
    pass


def _import_websockets() -> Any:
    try:
        import websockets
    except ImportError as exc:
        raise RealtimeSessionError(
            "realtime voice requires the optional dependency: pip install 'websockets>=12'"
        ) from exc
    return websockets


class OpenAIRealtimeSession:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        *,
        model: str,
        instructions: str = "",
        voice: str = "alloy",
        tools: Sequence[dict[str, Any]] = (),
        connect_timeout: float = 30.0,
        base_url: str = "",
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.model = model
        self.instructions = instructions[:MAX_INSTRUCTION_CHARS]
        self.voice = voice
        self.tools = list(tools)
        self.connect_timeout = connect_timeout
        self.base_url = base_url.rstrip("/")
        self._websocket: Any | None = None
        self._state = "idle"
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def websocket(self) -> Any:
        return self._websocket

    def realtime_url(self) -> str:
        base = self.base_url or self.spec.base_url.rstrip("/")
        if base.startswith("http://"):
            base = "ws://" + base.removeprefix("http://")
        elif base.startswith("https://"):
            base = "wss://" + base.removeprefix("https://")
        elif not base.startswith(("ws://", "wss://")):
            base = f"wss://{base}"
        return f"{base}/v1/realtime?model={self.model}"

    async def connect(self) -> None:
        if self._closed:
            raise RealtimeSessionError("realtime session is already closed")
        websockets = _import_websockets()
        headers = {"Authorization": f"Bearer {self._key()}"}
        headers.update(dict(self.spec.extra_headers))
        self._state = "connecting"
        try:
            self._websocket = await asyncio.wait_for(
                websockets.connect(self.realtime_url(), additional_headers=headers),
                timeout=self.connect_timeout,
            )
        except TimeoutError as exc:
            self._state = "closed"
            raise RealtimeSessionError(
                f"{self.spec.name}: realtime connection timed out", provider=self.spec.name
            ) from exc
        except Exception as exc:
            self._state = "closed"
            raise RealtimeSessionError(
                f"{self.spec.name}: realtime connection failed: {type(exc).__name__}",
                provider=self.spec.name,
            ) from exc
        self._reader = asyncio.create_task(self._read_loop())
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": self.instructions,
                    "voice": self.voice,
                    "input_audio_format": REALTIME_AUDIO_FORMAT,
                    "output_audio_format": REALTIME_AUDIO_FORMAT,
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {"type": "server_vad"},
                    "tools": self.tools,
                    "tool_choice": "auto",
                },
            }
        )
        self._state = "listening"

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16).decode("ascii"),
            }
        )

    async def interrupt(self) -> None:
        self._state = "interrupted"
        await self._send({"type": "response.cancel"})
        await self._send({"type": "input_audio_buffer.clear"})
        self._state = "listening"

    async def reconnect(self) -> None:
        if self._closed:
            return
        await self._teardown()
        self._closed = False
        await self.connect()

    async def send_tool_result(self, call_id: str, output: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output[:MAX_TOOL_RESULT_CHARS],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._events.get()
            if event.get("__closed__"):
                return
            yield event

    async def close(self) -> None:
        await self._teardown()
        self._closed = True

    async def _teardown(self) -> None:
        reader = self._reader
        self._reader = None
        websocket = self._websocket
        self._websocket = None
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                log.debug("realtime websocket close failed", exc_info=True)
        self._state = "closed"
        self._events.put_nowait({"__closed__": True})

    async def _read_loop(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    log.debug("realtime event is not json: %s", raw[:200])
                    continue
                if not isinstance(event, dict):
                    continue
                self._track_state(event)
                self._events.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning(
                    "realtime stream ended unexpectedly provider=%s error_type=%s",
                    self.spec.name,
                    type(exc).__name__,
                )
                self._events.put_nowait(
                    {
                        "type": "error",
                        "error": {"message": f"realtime stream ended: {type(exc).__name__}"},
                    }
                )
        finally:
            self._events.put_nowait({"__closed__": True})

    def _track_state(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", ""))
        if kind in {"response.audio.delta", "response.audio_transcript.delta"}:
            self._state = "speaking"
        elif kind in {
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "response.done",
        }:
            self._state = "listening"

    async def _send(self, event: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None:
            raise RealtimeSessionError(
                f"{self.spec.name}: realtime session is not connected", provider=self.spec.name
            )
        await websocket.send(json.dumps(event, ensure_ascii=False))

    def _key(self) -> str:
        if self.spec.api_key_env and not self._api_key:
            from eurus.errors import AuthenticationError

            raise AuthenticationError(
                f"{self.spec.name}: environment variable {self.spec.api_key_env} is not set; "
                "run eurus setup provider to configure the API key",
                provider=self.spec.name,
            )
        return self._api_key or ""


class OpenAIRealtimeTransport:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        *,
        model: str,
        voice: str = "alloy",
        connect_timeout: float = 30.0,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.model = model
        self.voice = voice
        self.connect_timeout = connect_timeout

    async def open_session(
        self,
        *,
        instructions: str = "",
        tools: Sequence[dict[str, Any]] = (),
    ) -> OpenAIRealtimeSession:
        session = OpenAIRealtimeSession(
            self.spec,
            self._api_key,
            model=self.model,
            instructions=instructions,
            voice=self.voice,
            tools=tools,
            connect_timeout=self.connect_timeout,
        )
        await session.connect()
        return session


def build_realtime_transport(
    config: Any,
    *,
    registry: CapabilityRegistry | None = None,
    environ: Any = None,
) -> OpenAIRealtimeTransport | None:
    model_text = config.audio.realtime_model.strip()
    if not model_text:
        return None
    try:
        ref = ModelRef.parse(model_text)
    except ModelRefError as exc:
        raise ConfigError(f"audio realtime model must look like 'provider/model': {exc}") from exc
    spec = config.providers.get(ref.provider)
    if spec is None:
        raise ConfigError(f"unknown provider '{ref.provider}' in audio realtime model")
    if spec.protocol != "openai":
        raise ConfigError(
            f"realtime voice requires an OpenAI-compatible provider, "
            f"'{ref.provider}' uses protocol '{spec.protocol}'"
        )
    resolved = registry if registry is not None else default_registry()
    capabilities = resolved.lookup_ref(ref, protocol=spec.protocol)
    if not capabilities.realtime_audio:
        raise ConfigError(
            f"model '{ref.model}' does not declare realtime audio support; "
            "declare capabilities or pick a realtime model"
        )
    return OpenAIRealtimeTransport(
        spec,
        resolve_api_key(spec, environ),
        model=ref.model,
        voice=config.audio.realtime_voice,
        connect_timeout=config.audio.realtime_timeout_seconds,
    )


__all__ = [
    "OPENAI_REALTIME_PROTOCOL",
    "OpenAIRealtimeSession",
    "OpenAIRealtimeTransport",
    "ProviderUnavailableError",
    "RealtimeSessionError",
    "REALTIME_AUDIO_FORMAT",
    "REALTIME_INPUT_AUDIO_MIME",
    "REALTIME_SAMPLE_RATE",
    "build_realtime_transport",
]
