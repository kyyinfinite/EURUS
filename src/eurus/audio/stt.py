from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from eurus.catalog import ProviderSpec, resolve_api_key
from eurus.errors import ConfigError, ModelRefError, ProviderError, ProviderUnavailableError
from eurus.models import ModelRef
from eurus.providers.httpx_transport import HttpxTransport
from eurus.providers.model_capabilities import CapabilityRegistry, default_registry
from eurus.providers.transport import HttpError, TransportError, error_from_http

if TYPE_CHECKING:
    from eurus.config import Config

log = logging.getLogger(__name__)

TRANSCRIBABLE_MIMES: dict[str, tuple[str, str]] = {
    "audio/mpeg": ("audio.mp3", "audio/mpeg"),
    "audio/mp3": ("audio.mp3", "audio/mpeg"),
    "audio/wav": ("audio.wav", "audio/wav"),
    "audio/x-wav": ("audio.wav", "audio/wav"),
    "audio/ogg": ("audio.ogg", "audio/ogg"),
    "audio/mp4": ("audio.m4a", "audio/mp4"),
    "audio/m4a": ("audio.m4a", "audio/mp4"),
    "audio/webm": ("audio.webm", "audio/webm"),
    "audio/flac": ("audio.flac", "audio/flac"),
    "video/mp4": ("audio.mp4", "video/mp4"),
    "video/webm": ("audio.webm", "video/webm"),
}
MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
STT_RETRIES = 2


class TranscriptionError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str = ""
    duration_seconds: float | None = None
    provider: str = ""
    model: str = ""


class Transcriber(Protocol):
    async def transcribe(
        self, data: bytes, mime: str, *, language: str | None = None
    ) -> TranscriptionResult: ...


class AudioTransport(Protocol):
    async def post_multipart(
        self,
        url: str,
        headers: Mapping[str, str],
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]: ...


class OpenAICompatTranscriber:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        transport: AudioTransport,
        *,
        model: str,
        language: str = "",
        max_bytes: int = MAX_TRANSCRIPTION_BYTES,
        retries: int = STT_RETRIES,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.transport = transport
        self.model = model
        self.language = language
        self.max_bytes = max_bytes
        self.retries = max(0, retries)

    async def transcribe(
        self, data: bytes, mime: str, *, language: str | None = None
    ) -> TranscriptionResult:
        if not data:
            raise TranscriptionError("audio payload is empty", provider=self.spec.name)
        if len(data) > self.max_bytes:
            raise TranscriptionError(
                f"audio exceeds the {self.max_bytes} byte transcription limit",
                provider=self.spec.name,
            )
        upload = TRANSCRIBABLE_MIMES.get(mime)
        if upload is None:
            raise TranscriptionError(
                f"unsupported audio format for transcription: {mime}",
                provider=self.spec.name,
            )
        filename, content_type = upload
        effective_language = (language or self.language or "").strip() or None
        attempt = 0
        while True:
            try:
                return await self._request(data, filename, content_type, effective_language)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
                await asyncio.sleep(min(0.5 * attempt, 4.0))

    async def _request(
        self, data: bytes, filename: str, content_type: str, language: str | None
    ) -> TranscriptionResult:
        url = f"{self.spec.base_url.rstrip('/')}/audio/transcriptions"
        form: dict[str, str] = {"model": self.model}
        if language:
            form["language"] = language
        headers = {"Content-Type": "application/json"}
        headers.update(self._extra_headers())
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        files = {"file": (filename, data, content_type)}
        try:
            response = await self.transport.post_multipart(url, headers, form, files)
        except HttpError as exc:
            raise error_from_http(self.spec.name, exc) from exc
        except TransportError as exc:
            raise ProviderUnavailableError(
                f"{self.spec.name}: {exc}", provider=self.spec.name, retryable=True
            ) from exc
        text = response.get("text")
        if not isinstance(text, str):
            raise TranscriptionError(
                f"{self.spec.name}: transcription response has no text",
                provider=self.spec.name,
            )
        raw_language = response.get("language")
        language_value = str(raw_language) if isinstance(raw_language, str) else ""
        raw_duration = response.get("duration")
        duration: float | None = None
        if isinstance(raw_duration, int | float) and not isinstance(raw_duration, bool):
            duration = float(raw_duration)
        return TranscriptionResult(
            text=text,
            language=language_value,
            duration_seconds=duration,
            provider=self.spec.name,
            model=str(response.get("model") or self.model),
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


def _audio_registry(config: Config) -> CapabilityRegistry:
    from eurus.providers.factory import _registry_from_config

    return _registry_from_config(config) or default_registry()


def _resolve_audio_ref(
    config: Config, model_text: str, *, purpose: str
) -> tuple[ModelRef, ProviderSpec]:
    try:
        ref = ModelRef.parse(model_text)
    except ModelRefError as exc:
        raise ConfigError(f"audio {purpose} model must look like 'provider/model': {exc}") from exc
    spec = config.providers.get(ref.provider)
    if spec is None:
        raise ConfigError(f"unknown provider '{ref.provider}' in audio {purpose} model")
    return ref, spec


def build_transcriber(
    config: Config,
    *,
    transport: AudioTransport | None = None,
    environ: Mapping[str, str] | None = None,
    registry: CapabilityRegistry | None = None,
) -> OpenAICompatTranscriber | None:
    model_text = config.audio.stt_model.strip()
    if not model_text:
        return None
    ref, spec = _resolve_audio_ref(config, model_text, purpose="stt")
    if spec.protocol != "openai":
        raise ConfigError(
            f"audio stt requires an OpenAI-compatible provider, "
            f"'{ref.provider}' uses protocol '{spec.protocol}'"
        )
    resolved = registry if registry is not None else _audio_registry(config)
    capabilities = resolved.lookup_ref(ref, protocol=spec.protocol)
    if not capabilities.audio_input or not capabilities.text_output:
        raise ConfigError(
            f"model '{ref.model}' does not declare transcription support "
            "(audio_input and text_output); declare capabilities or pick a transcription model"
        )
    resolved_transport: AudioTransport
    if transport is None:
        resolved_transport = HttpxTransport(timeout=config.audio.stt_timeout_seconds)
    else:
        resolved_transport = transport
    return OpenAICompatTranscriber(
        spec,
        resolve_api_key(spec, environ),
        resolved_transport,
        model=ref.model,
        language=config.audio.stt_language,
    )
