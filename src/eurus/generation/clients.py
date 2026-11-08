from __future__ import annotations

import asyncio
import base64
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

MAX_PROMPT_CHARS = 4000
MAX_OUTPUTS_PER_REQUEST = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
GENERATION_RETRIES = 1


class GenerationError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    mime: str
    revised_prompt: str = ""


@dataclass(frozen=True, slots=True)
class VideoJob:
    job_id: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class VideoJobStatus:
    done: bool
    video_uri: str = ""
    error: str = ""


class GenerationTransport(Protocol):
    async def post_json(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def post_multipart(
        self,
        url: str,
        headers: Mapping[str, str],
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]: ...

    async def get_json(self, url: str, headers: Mapping[str, str]) -> dict[str, Any]: ...

    async def get_bytes(self, url: str, headers: Mapping[str, str]) -> bytes: ...


class ImageGenerator(Protocol):
    supports_edit: bool

    async def generate(
        self, prompt: str, *, size: str = "", count: int = 1
    ) -> list[GeneratedImage]: ...

    async def edit(
        self, prompt: str, image: bytes, image_mime: str, *, size: str = ""
    ) -> list[GeneratedImage]: ...


class VideoGenerator(Protocol):
    async def submit(
        self,
        prompt: str,
        *,
        image: bytes | None = None,
        image_mime: str = "",
        aspect_ratio: str = "",
    ) -> VideoJob: ...

    async def status(self, job: VideoJob) -> VideoJobStatus: ...

    async def download(self, job: VideoJob, status: VideoJobStatus) -> bytes: ...

    async def cancel(self, job: VideoJob) -> bool: ...


def _clip_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        raise GenerationError("generation prompt is required")
    return text[:MAX_PROMPT_CHARS]


def _clip_count(count: int, cap: int = MAX_OUTPUTS_PER_REQUEST) -> int:
    return max(1, min(int(count or 1), cap))


def _sniff_image_mime(data: bytes) -> str:
    from eurus.media.signatures import detect_signature

    detected = detect_signature(data)
    if isinstance(detected, str) and detected.startswith("image/"):
        return detected
    return "image/png"


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return bool(exc.retryable)
    return isinstance(exc, (TransportError, TimeoutError))


async def _with_retry(work: Any, *args: Any, retries: int = GENERATION_RETRIES) -> Any:
    attempt = 0
    while True:
        try:
            return await work(*args)
        except Exception as exc:
            if not _retryable(exc) or attempt >= retries:
                raise
            attempt += 1
            await asyncio.sleep(min(0.5 * attempt, 4.0))


class OpenAICompatImageClient:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        transport: GenerationTransport,
        *,
        model: str,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.transport = transport
        self.model = model
        self.supports_edit = True

    async def generate(
        self, prompt: str, *, size: str = "", count: int = 1
    ) -> list[GeneratedImage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _clip_prompt(prompt),
            "n": _clip_count(count),
        }
        requested = (size or "").strip()
        if requested and requested != "auto":
            payload["size"] = requested
        if self.model.startswith("dall-e"):
            payload["response_format"] = "b64_json"
        data = await _with_retry(self._post_generations, payload)
        return await self._parse_images(data)

    async def edit(
        self, prompt: str, image: bytes, image_mime: str, *, size: str = ""
    ) -> list[GeneratedImage]:
        if not image:
            raise GenerationError("image edit requires the source image payload")
        url = f"{self.spec.base_url.rstrip('/')}/images/edits"
        form: dict[str, str] = {
            "model": self.model,
            "prompt": _clip_prompt(prompt),
        }
        requested = (size or "").strip()
        if requested and requested != "auto":
            form["size"] = requested
        filename = "image.png" if image_mime == "image/png" else "image.webp"
        files = {"image": (filename, image, image_mime)}
        response = await _with_retry(self._post_multipart, url, self._headers(), form, files)
        return await self._parse_images(response)

    async def _post_generations(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.spec.base_url.rstrip('/')}/images/generations"
        return await self.transport.post_json(url, self._headers(), payload)

    async def _post_multipart(
        self,
        url: str,
        headers: Mapping[str, str],
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        return await self.transport.post_multipart(url, headers, data, files)

    async def _download(self, url: str) -> bytes:
        return await self.transport.get_bytes(url, self._headers())

    async def _parse_images(self, data: Mapping[str, Any]) -> list[GeneratedImage]:
        entries = data.get("data")
        if not isinstance(entries, list) or not entries:
            raise GenerationError(
                f"{self.spec.name}: image generation response has no data", provider=self.spec.name
            )
        images: list[GeneratedImage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            encoded = entry.get("b64_json")
            if isinstance(encoded, str) and encoded:
                payload = base64.b64decode(encoded)
            else:
                url = entry.get("url")
                if not isinstance(url, str) or not url:
                    continue
                payload = await self._download(url)
            if not payload or len(payload) > MAX_IMAGE_BYTES:
                continue
            revised = entry.get("revised_prompt")
            images.append(
                GeneratedImage(
                    data=payload,
                    mime=_sniff_image_mime(payload),
                    revised_prompt=revised if isinstance(revised, str) else "",
                )
            )
        if not images:
            raise GenerationError(
                f"{self.spec.name}: image generation produced no usable images",
                provider=self.spec.name,
            )
        return images

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(dict(self.spec.extra_headers))
        if self.spec.user_agent:
            headers["User-Agent"] = self.spec.user_agent
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
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


class GeminiImagenClient:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        transport: GenerationTransport,
        *,
        model: str,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.transport = transport
        self.model = model
        self.supports_edit = False

    async def generate(
        self, prompt: str, *, size: str = "", count: int = 1
    ) -> list[GeneratedImage]:
        payload: dict[str, Any] = {
            "instances": [{"prompt": _clip_prompt(prompt)}],
            "parameters": {"sampleCount": _clip_count(count)},
        }
        url = f"{self.spec.base_url.rstrip('/')}/models/{self.model}:predict"
        data = await _with_retry(self._post_json, url, self._headers(), payload)
        predictions = data.get("predictions")
        if not isinstance(predictions, list) or not predictions:
            raise GenerationError(
                f"{self.spec.name}: imagen response has no predictions", provider=self.spec.name
            )
        images: list[GeneratedImage] = []
        for entry in predictions:
            if not isinstance(entry, dict):
                continue
            encoded = entry.get("bytesBase64Encoded")
            if not isinstance(encoded, str) or not encoded:
                continue
            payload_bytes = base64.b64decode(encoded)
            if not payload_bytes or len(payload_bytes) > MAX_IMAGE_BYTES:
                continue
            mime = entry.get("mimeType")
            images.append(
                GeneratedImage(
                    data=payload_bytes,
                    mime=mime
                    if isinstance(mime, str) and mime.startswith("image/")
                    else "image/png",
                )
            )
        if not images:
            raise GenerationError(
                f"{self.spec.name}: imagen produced no usable images", provider=self.spec.name
            )
        return images

    async def edit(
        self, prompt: str, image: bytes, image_mime: str, *, size: str = ""
    ) -> list[GeneratedImage]:
        raise GenerationError(
            f"{self.spec.name}: image editing is not supported by imagen models; "
            "configure an OpenAI-compatible image model (for example gpt-image-1) to edit images",
            provider=self.spec.name,
        )

    async def _post_json(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self.transport.post_json(url, headers, payload)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(dict(self.spec.extra_headers))
        if self.spec.user_agent:
            headers["User-Agent"] = self.spec.user_agent
        key = self._key()
        if key:
            headers["x-goog-api-key"] = key
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


class GeminiVeoClient:
    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        transport: GenerationTransport,
        *,
        model: str,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self.transport = transport
        self.model = model

    async def submit(
        self,
        prompt: str,
        *,
        image: bytes | None = None,
        image_mime: str = "",
        aspect_ratio: str = "",
    ) -> VideoJob:
        instance: dict[str, Any] = {"prompt": _clip_prompt(prompt)}
        if image is not None and image_mime:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(image).decode("ascii"),
                "mimeType": image_mime,
            }
        parameters: dict[str, Any] = {}
        if aspect_ratio:
            parameters["aspectRatio"] = aspect_ratio
        payload = {"instances": [instance], "parameters": parameters}
        url = f"{self.spec.base_url.rstrip('/')}/models/{self.model}:predictLongRunning"
        data = await self.transport.post_json(url, self._headers(), payload)
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GenerationError(
                f"{self.spec.name}: video generation did not return an operation name",
                provider=self.spec.name,
            )
        return VideoJob(job_id=name.strip(), provider=self.spec.name, model=self.model)

    async def status(self, job: VideoJob) -> VideoJobStatus:
        url = f"{self.spec.base_url.rstrip('/')}/{job.job_id.lstrip('/')}"
        try:
            data = await self.transport.get_json(url, self._headers())
        except HttpError as exc:
            raise error_from_http(self.spec.name, exc) from exc
        except TransportError as exc:
            raise ProviderUnavailableError(
                f"{self.spec.name}: {exc}", provider=self.spec.name, retryable=True
            ) from exc
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            return VideoJobStatus(done=True, error=str(message or "video generation failed"))
        if not data.get("done"):
            return VideoJobStatus(done=False)
        uri = self._extract_video_uri(data.get("response"))
        if not uri:
            return VideoJobStatus(done=True, error="video generation finished without a video")
        return VideoJobStatus(done=True, video_uri=uri)

    async def download(self, job: VideoJob, status: VideoJobStatus) -> bytes:
        if not status.video_uri:
            raise GenerationError(
                f"{self.spec.name}: no downloadable video for job {job.job_id}",
                provider=self.spec.name,
            )
        separator = "&" if "?" in status.video_uri else "?"
        url = f"{status.video_uri}{separator}alt=media"
        data = await self.transport.get_bytes(url, self._headers())
        if not data:
            raise GenerationError(
                f"{self.spec.name}: downloaded video is empty", provider=self.spec.name
            )
        if len(data) > MAX_VIDEO_BYTES:
            raise GenerationError(
                f"{self.spec.name}: generated video exceeds the {MAX_VIDEO_BYTES} byte limit",
                provider=self.spec.name,
            )
        return data

    async def cancel(self, job: VideoJob) -> bool:
        url = f"{self.spec.base_url.rstrip('/')}/{job.job_id.lstrip('/')}:cancel"
        try:
            await self.transport.post_json(url, self._headers(), {})
        except (HttpError, TransportError, ProviderError) as exc:
            log.warning(
                "video job cancellation failed job_id=%s error_type=%s",
                job.job_id,
                type(exc).__name__,
            )
            return False
        return True

    def _extract_video_uri(self, response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        video_response = response.get("generateVideoResponse")
        if isinstance(video_response, dict):
            samples = video_response.get("generatedSamples")
            if isinstance(samples, list) and samples:
                sample = samples[0]
                if isinstance(sample, dict):
                    video = sample.get("video")
                    if isinstance(video, dict) and isinstance(video.get("uri"), str):
                        return str(video["uri"])
        videos = response.get("videos")
        if isinstance(videos, list) and videos:
            video = videos[0]
            if isinstance(video, dict):
                for key in ("uri", "gcsUri", "videoUri"):
                    value = video.get(key)
                    if isinstance(value, str) and value:
                        return value
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(dict(self.spec.extra_headers))
        if self.spec.user_agent:
            headers["User-Agent"] = self.spec.user_agent
        key = self._key()
        if key:
            headers["x-goog-api-key"] = key
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


def _resolve_generation_ref(
    config: Config, model_text: str, *, purpose: str
) -> tuple[ModelRef, ProviderSpec]:
    try:
        ref = ModelRef.parse(model_text)
    except ModelRefError as exc:
        raise ConfigError(
            f"generation {purpose} model must look like 'provider/model': {exc}"
        ) from exc
    spec = config.providers.get(ref.provider)
    if spec is None:
        raise ConfigError(f"unknown provider '{ref.provider}' in generation {purpose} model")
    return ref, spec


def _generation_registry(config: Config) -> CapabilityRegistry:
    from eurus.providers.factory import _registry_from_config

    return _registry_from_config(config) or default_registry()


def build_image_client(
    config: Config,
    *,
    transport: GenerationTransport | None = None,
    environ: Mapping[str, str] | None = None,
    registry: CapabilityRegistry | None = None,
) -> ImageGenerator | None:
    model_text = config.generation.image_model.strip()
    if not model_text:
        return None
    ref, spec = _resolve_generation_ref(config, model_text, purpose="image")
    resolved = registry if registry is not None else _generation_registry(config)
    capabilities = resolved.lookup_ref(ref, protocol=spec.protocol)
    if not capabilities.image_generation:
        raise ConfigError(
            f"model '{ref.model}' does not declare image generation support; "
            "declare capabilities or pick an image generation model"
        )
    resolved_transport: GenerationTransport
    if transport is None:
        resolved_transport = HttpxTransport(timeout=config.generation.image_timeout_seconds)
    else:
        resolved_transport = transport
    if spec.protocol == "openai":
        return OpenAICompatImageClient(
            spec, resolve_api_key(spec, environ), resolved_transport, model=ref.model
        )
    if spec.protocol == "gemini":
        return GeminiImagenClient(
            spec, resolve_api_key(spec, environ), resolved_transport, model=ref.model
        )
    raise ConfigError(
        f"image generation requires an OpenAI-compatible or Gemini provider, "
        f"'{ref.provider}' uses protocol '{spec.protocol}'"
    )


def build_video_client(
    config: Config,
    *,
    transport: GenerationTransport | None = None,
    environ: Mapping[str, str] | None = None,
    registry: CapabilityRegistry | None = None,
) -> VideoGenerator | None:
    model_text = config.generation.video_model.strip()
    if not model_text:
        return None
    ref, spec = _resolve_generation_ref(config, model_text, purpose="video")
    resolved = registry if registry is not None else _generation_registry(config)
    capabilities = resolved.lookup_ref(ref, protocol=spec.protocol)
    if not capabilities.video_generation:
        raise ConfigError(
            f"model '{ref.model}' does not declare video generation support; "
            "declare capabilities or pick a video generation model"
        )
    if spec.protocol != "gemini":
        raise ConfigError(
            f"video generation requires a Gemini provider with Veo models, "
            f"'{ref.provider}' uses protocol '{spec.protocol}'"
        )
    resolved_transport: GenerationTransport
    if transport is None:
        resolved_transport = HttpxTransport(
            timeout=config.generation.video_poll_interval_seconds * 3
        )
    else:
        resolved_transport = transport
    return GeminiVeoClient(
        spec, resolve_api_key(spec, environ), resolved_transport, model=ref.model
    )
