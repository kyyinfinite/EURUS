from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from eurus.audio.config import MAX_TTS_INPUT_CHARS, TTS_FORMATS
from eurus.catalog import ProviderSpec, resolve_api_key
from eurus.errors import ConfigError, ProviderError, ProviderUnavailableError
from eurus.providers.httpx_transport import HttpxTransport
from eurus.providers.model_capabilities import CapabilityRegistry
from eurus.providers.transport import HttpError, TransportError, error_from_http

if TYPE_CHECKING:
    from eurus.config import Config

log = logging.getLogger(__name__)

TTS_MIMES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
}
TTS_RETRIES = 2
MAX_SPEECH_BYTES = 25 * 1024 * 1024


class SpeechSynthesisError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechResult:
    data: bytes
    mime: str
    provider: str = ""
    model: str = ""
    voice: str = ""
    audio_format: str = ""


class SpeechSynthesizer(Protocol):
    async def synthesize(
        self, text: str, *, voice: str | None = None, audio_format: str | None = None
    ) -> SpeechResult: ...


class SpeechTransport(Protocol):
    async def post_binary(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
    ) -> bytes: ...


class OpenAICompatSpeechSynthesizer:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        transport: SpeechTransport,
        *,
        model: str,
        default_voice: str = "alloy",
        default_format: str = "mp3",
        max_chars: int = MAX_TTS_INPUT_CHARS,
        retries: int = TTS_RETRIES,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.transport = transport
        self.model = model
        self.default_voice = default_voice
        self.default_format = default_format
        self.max_chars = max_chars
        self.retries = max(0, retries)

    async def synthesize(
        self, text: str, *, voice: str | None = None, audio_format: str | None = None
    ) -> SpeechResult:
        cleaned = text.strip()
        if not cleaned:
            raise SpeechSynthesisError("speech text must not be empty", provider=self.spec.name)
        if len(cleaned) > self.max_chars:
            raise SpeechSynthesisError(
                f"speech text exceeds the {self.max_chars} character limit",
                provider=self.spec.name,
            )
        chosen_voice = (voice or "").strip() or self.default_voice
        chosen_format = (audio_format or "").strip() or self.default_format
        if chosen_format not in TTS_FORMATS:
            raise SpeechSynthesisError(
                f"speech format must be one of {TTS_FORMATS}",
                provider=self.spec.name,
            )
        attempt = 0
        while True:
            try:
                return await self._request(cleaned, chosen_voice, chosen_format)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
                await asyncio.sleep(min(0.5 * attempt, 4.0))

    async def _request(self, text: str, voice: str, audio_format: str) -> SpeechResult:
        url = f"{self.spec.base_url.rstrip('/')}/audio/speech"
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": audio_format,
        }
        headers = {"Content-Type": "application/json"}
        headers.update(self._extra_headers())
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            data = await self.transport.post_binary(url, headers, payload)
        except HttpError as exc:
            raise error_from_http(self.spec.name, exc) from exc
        except TransportError as exc:
            raise ProviderUnavailableError(
                f"{self.spec.name}: {exc}", provider=self.spec.name, retryable=True
            ) from exc
        if not data:
            raise SpeechSynthesisError(
                f"{self.spec.name}: speech response is empty", provider=self.spec.name
            )
        if len(data) > MAX_SPEECH_BYTES:
            raise SpeechSynthesisError(
                f"speech output exceeds the {MAX_SPEECH_BYTES} byte limit",
                provider=self.spec.name,
            )
        return SpeechResult(
            data=data,
            mime=TTS_MIMES[audio_format],
            provider=self.spec.name,
            model=self.model,
            voice=voice,
            audio_format=audio_format,
        )

    def _extra_headers(self) -> dict[str, str]:
        headers = dict(self.spec.extra_headers)
        if self.spec.user_agent:
            headers["User-Agent"] = self.spec.user_agent
        return headers

    def _key(self) -> str | None:
        if self.spec.api_key_env and not self._api_key:
            from eurus.errors import AuthenticationError

            raise AuthenticationError(
                f"{self.spec.name}: environment variable {self.spec.api_key_env} is not set; "
                "run eurus setup provider to configure the API key",
                provider=self.spec.name,
            )
        return self._api_key


def build_speech_synthesizer(
    config: Config,
    *,
    transport: SpeechTransport | None = None,
    environ: Mapping[str, str] | None = None,
    registry: CapabilityRegistry | None = None,
) -> OpenAICompatSpeechSynthesizer | None:
    model_text = config.audio.tts_model.strip()
    if not model_text:
        return None
    from eurus.audio.stt import _audio_registry, _resolve_audio_ref

    ref, spec = _resolve_audio_ref(config, model_text, purpose="tts")
    if spec.protocol != "openai":
        raise ConfigError(
            f"audio tts requires an OpenAI-compatible provider, "
            f"'{ref.provider}' uses protocol '{spec.protocol}'"
        )
    resolved = registry if registry is not None else _audio_registry(config)
    capabilities = resolved.lookup_ref(ref, protocol=spec.protocol)
    if not capabilities.audio_output:
        raise ConfigError(
            f"model '{ref.model}' does not declare speech synthesis support "
            "(audio_output); declare capabilities or pick a TTS model"
        )
    resolved_transport: SpeechTransport
    if transport is None:
        resolved_transport = HttpxTransport(timeout=config.audio.tts_timeout_seconds)
    else:
        resolved_transport = transport
    return OpenAICompatSpeechSynthesizer(
        spec,
        resolve_api_key(spec, environ),
        resolved_transport,
        model=ref.model,
        default_voice=config.audio.tts_voice,
        default_format=config.audio.tts_format,
    )


def speech_filename(audio_format: str) -> str:
    from eurus.media.signatures import EXT_BY_MIME

    ext = EXT_BY_MIME.get(TTS_MIMES.get(audio_format, ""))
    return f"speech.{ext or audio_format}"
