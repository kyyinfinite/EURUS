from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from eurus.content import (
    ArtifactBlock,
    AudioBlock,
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    VideoBlock,
    VideoFrameBlock,
)
from eurus.errors import EurusError
from eurus.models import Message, ModelRef

Modality = Literal["text", "image", "audio", "video", "document", "artifact"]

INPUT_MODALITY_BY_BLOCK: dict[type[Any], Modality] = {
    ImageBlock: "image",
    AudioBlock: "audio",
    VideoBlock: "video",
    VideoFrameBlock: "image",
    DocumentBlock: "document",
    ArtifactBlock: "artifact",
}

CAPABILITY_KEYS: tuple[str, ...] = (
    "text_input",
    "image_input",
    "audio_input",
    "video_input",
    "document_input",
    "text_output",
    "audio_output",
    "image_generation",
    "video_generation",
    "tool_calling",
    "streaming",
    "realtime_audio",
)

VIEW_KEY_MAP: dict[str, str] = {
    "textInput": "text_input",
    "imageInput": "image_input",
    "audioInput": "audio_input",
    "videoInput": "video_input",
    "fileInput": "document_input",
    "textOutput": "text_output",
    "audioOutput": "audio_output",
    "imageOutput": "image_generation",
    "videoOutput": "video_generation",
    "toolCalling": "tool_calling",
    "streaming": "streaming",
    "realtimeAudio": "realtime_audio",
}


class UnsupportedModalityError(EurusError):
    def __init__(
        self,
        message: str,
        *,
        required: Sequence[Modality] = (),
        candidates: Sequence[tuple[ModelRef, Sequence[Modality]]] = (),
    ) -> None:
        super().__init__(message)
        self.required = tuple(required)
        self.candidates = tuple((ref, tuple(missing)) for ref, missing in candidates)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    text_input: bool = True
    image_input: bool = False
    audio_input: bool = False
    video_input: bool = False
    document_input: bool = False
    text_output: bool = True
    audio_output: bool = False
    image_generation: bool = False
    video_generation: bool = False
    tool_calling: bool = True
    streaming: bool = True
    realtime_audio: bool = False

    def to_view(self) -> dict[str, bool]:
        return {
            "textInput": self.text_input,
            "imageInput": self.image_input,
            "audioInput": self.audio_input,
            "videoInput": self.video_input,
            "fileInput": self.document_input,
            "textOutput": self.text_output,
            "audioOutput": self.audio_output,
            "imageOutput": self.image_generation,
            "videoOutput": self.video_generation,
            "toolCalling": self.tool_calling,
            "streaming": self.streaming,
        }

    @property
    def multimodal_input(self) -> bool:
        return self.image_input or self.audio_input or self.video_input

    @property
    def multimodal(self) -> bool:
        return (
            self.image_input
            or self.audio_input
            or self.video_input
            or self.audio_output
            or self.image_generation
            or self.video_generation
        )

    def accepts(self, modality: Modality) -> bool:
        if modality == "text":
            return self.text_input
        if modality == "image":
            return self.image_input
        if modality == "audio":
            return self.audio_input
        if modality == "video":
            return self.video_input
        if modality == "document":
            return self.document_input
        return self.tool_calling

    @classmethod
    def from_view(cls, view: Mapping[str, Any]) -> ModelCapabilities:
        if not isinstance(view, Mapping):
            raise ValueError("capability view must be a mapping")
        values: dict[str, bool] = {}
        for raw_key, raw_value in view.items():
            key = VIEW_KEY_MAP.get(str(raw_key), str(raw_key))
            if key not in CAPABILITY_KEYS:
                raise ValueError(f"unknown capability: {raw_key!r}")
            if not isinstance(raw_value, bool):
                raise ValueError(f"capability {raw_key!r} must be a boolean")
            values[key] = raw_value
        return cls(**values)


DEFAULT_CAPABILITIES = ModelCapabilities()

PROTOCOL_DEFAULTS: dict[str, ModelCapabilities] = {
    protocol: ModelCapabilities()
    for protocol in ("openai", "anthropic", "gemini", "groq-sdk", "genai-sdk")
}

_VISION_DOCUMENT = ModelCapabilities(image_input=True, document_input=True)
_GEMINI_MULTIMODAL = ModelCapabilities(
    image_input=True,
    audio_input=True,
    video_input=True,
    document_input=True,
)
_AUDIO_CHAT = ModelCapabilities(audio_input=True, audio_output=True)
_TRANSCRIBE = ModelCapabilities(
    text_output=True,
    tool_calling=False,
    streaming=False,
    audio_input=True,
)
_TTS = ModelCapabilities(
    text_input=True,
    text_output=False,
    tool_calling=False,
    streaming=False,
    audio_output=True,
)
_IMAGE_GEN = ModelCapabilities(
    text_input=True, text_output=False, tool_calling=False, streaming=False, image_generation=True
)
_VIDEO_GEN = ModelCapabilities(
    text_input=True, text_output=False, tool_calling=False, streaming=False, video_generation=True
)
_REALTIME = ModelCapabilities(audio_input=True, audio_output=True, realtime_audio=True)
_TEXT_ONLY_NO_TOOLS = ModelCapabilities(tool_calling=False)

MODEL_DECLARATIONS: dict[tuple[str, str], ModelCapabilities] = {
    ("openai", "gpt-4o"): _VISION_DOCUMENT,
    ("openai", "gpt-4o-mini"): _VISION_DOCUMENT,
    ("openai", "gpt-4.1"): _VISION_DOCUMENT,
    ("openai", "gpt-4.1-mini"): _VISION_DOCUMENT,
    ("openai", "gpt-4.1-nano"): _VISION_DOCUMENT,
    ("openai", "gpt-4-turbo"): _VISION_DOCUMENT,
    ("openai", "gpt-4o-audio-preview"): _AUDIO_CHAT,
    ("openai", "gpt-4o-mini-audio-preview"): _AUDIO_CHAT,
    ("openai", "gpt-4o-realtime"): _REALTIME,
    ("openai", "gpt-realtime"): _REALTIME,
    ("openai", "o1"): _VISION_DOCUMENT,
    ("openai", "o1-pro"): _VISION_DOCUMENT,
    ("openai", "o3"): _VISION_DOCUMENT,
    ("openai", "o4-mini"): _VISION_DOCUMENT,
    ("openai", "o3-mini"): ModelCapabilities(),
    ("openai", "o1-mini"): _TEXT_ONLY_NO_TOOLS,
    ("openai", "gpt-4"): ModelCapabilities(),
    ("openai", "gpt-3.5-turbo"): ModelCapabilities(),
    ("openai", "gpt-image-1"): replace(_IMAGE_GEN, image_input=True),
    ("openai", "dall-e-3"): _IMAGE_GEN,
    ("openai", "dall-e-2"): _IMAGE_GEN,
    ("openai", "whisper-1"): _TRANSCRIBE,
    ("openai", "gpt-4o-transcribe"): _TRANSCRIBE,
    ("openai", "gpt-4o-mini-transcribe"): _TRANSCRIBE,
    ("openai", "tts-1"): _TTS,
    ("openai", "tts-1-hd"): _TTS,
    ("openai", "gpt-4o-mini-tts"): _TTS,
    ("anthropic", "claude-3"): _VISION_DOCUMENT,
    ("anthropic", "claude-sonnet-4"): _VISION_DOCUMENT,
    ("anthropic", "claude-opus-4"): _VISION_DOCUMENT,
    ("anthropic", "claude-haiku-4"): _VISION_DOCUMENT,
    ("gemini", "gemini-1.5"): _GEMINI_MULTIMODAL,
    ("gemini", "gemini-2.0"): _GEMINI_MULTIMODAL,
    ("gemini", "gemini-2.5"): _GEMINI_MULTIMODAL,
    ("gemini-sdk", "gemini-1.5"): _GEMINI_MULTIMODAL,
    ("gemini-sdk", "gemini-2.0"): _GEMINI_MULTIMODAL,
    ("gemini-sdk", "gemini-2.5"): _GEMINI_MULTIMODAL,
    ("gemini", "imagen"): _IMAGE_GEN,
    ("gemini-sdk", "imagen"): _IMAGE_GEN,
    ("gemini", "veo"): _VIDEO_GEN,
    ("gemini-sdk", "veo"): _VIDEO_GEN,
    ("groq", "whisper-large-v3"): _TRANSCRIBE,
    ("groq-sdk", "whisper-large-v3"): _TRANSCRIBE,
    ("mistral", "pixtral"): _VISION_DOCUMENT,
    ("xai", "grok-2-vision"): _VISION_DOCUMENT,
    ("xai", "grok-4"): _VISION_DOCUMENT,
    ("openrouter", "openai/gpt-4o"): _VISION_DOCUMENT,
    ("openrouter", "openai/gpt-4.1"): _VISION_DOCUMENT,
    ("openrouter", "anthropic/claude-3"): _VISION_DOCUMENT,
    ("openrouter", "anthropic/claude-sonnet-4"): _VISION_DOCUMENT,
    ("openrouter", "anthropic/claude-opus-4"): _VISION_DOCUMENT,
    ("openrouter", "google/gemini-1.5"): _GEMINI_MULTIMODAL,
    ("openrouter", "google/gemini-2.0"): _GEMINI_MULTIMODAL,
    ("openrouter", "google/gemini-2.5"): _GEMINI_MULTIMODAL,
    ("openrouter", "meta-llama/llama-3.2-11b-vision"): _VISION_DOCUMENT,
    ("openrouter", "meta-llama/llama-3.2-90b-vision"): _VISION_DOCUMENT,
    ("openrouter", "qwen/qwen2-vl"): _VISION_DOCUMENT,
    ("openrouter", "qwen/qwen2.5-vl"): _VISION_DOCUMENT,
    ("ollama", "llama3.2-vision"): _VISION_DOCUMENT,
    ("ollama", "llava"): _VISION_DOCUMENT,
    ("ollama", "moondream"): _VISION_DOCUMENT,
    ("ollama", "minicpm-v"): _VISION_DOCUMENT,
    ("ollama", "qwen2.5vl"): _VISION_DOCUMENT,
    ("ollama", "qwen2-vl"): _VISION_DOCUMENT,
    ("ollama", "gemma3"): _VISION_DOCUMENT,
}


def _family_matches(declared_model: str, model: str) -> bool:
    if model == declared_model:
        return True
    if not model.startswith(declared_model):
        return False
    boundary = model[len(declared_model)]
    return boundary in {"-", ".", "/", "_"}


class CapabilityRegistry:
    def __init__(
        self,
        *,
        model_declarations: Mapping[tuple[str, str], ModelCapabilities] | None = None,
        protocol_defaults: Mapping[str, ModelCapabilities] | None = None,
    ) -> None:
        self._models: dict[tuple[str, str], ModelCapabilities] = (
            dict(model_declarations) if model_declarations is not None else dict(MODEL_DECLARATIONS)
        )
        self._protocols: dict[str, ModelCapabilities] = (
            dict(protocol_defaults) if protocol_defaults is not None else dict(PROTOCOL_DEFAULTS)
        )
        self._providers: dict[str, ModelCapabilities] = {}

    def declare_model(self, provider: str, model: str, capabilities: ModelCapabilities) -> None:
        if not provider.strip() or not model.strip():
            raise ValueError("provider and model are required")
        self._models[(provider, model)] = capabilities

    def declare_provider(self, provider: str, capabilities: ModelCapabilities) -> None:
        if not provider.strip():
            raise ValueError("provider is required")
        self._providers[provider] = capabilities

    def protocol_default(self, protocol: str) -> ModelCapabilities:
        return self._protocols.get(protocol, DEFAULT_CAPABILITIES)

    def lookup(self, provider: str, model: str, *, protocol: str = "") -> ModelCapabilities:
        exact = self._models.get((provider, model))
        if exact is not None:
            return exact
        family: tuple[str, ModelCapabilities] | None = None
        for (declared_provider, declared_model), capabilities in self._models.items():
            if declared_provider != provider:
                continue
            if _family_matches(declared_model, model) and (
                family is None or len(declared_model) > len(family[0])
            ):
                family = (declared_model, capabilities)
        if family is not None:
            return family[1]
        provider_level = self._providers.get(provider)
        if provider_level is not None:
            return provider_level
        return self.protocol_default(protocol)

    def lookup_ref(self, ref: ModelRef, *, protocol: str = "") -> ModelCapabilities:
        return self.lookup(ref.provider, ref.model, protocol=protocol)

    def declared_models(self, provider: str) -> tuple[str, ...]:
        models = [model for (name, model) in self._models if name == provider]
        return tuple(sorted(models))


_default_registry: CapabilityRegistry | None = None


def default_registry() -> CapabilityRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = CapabilityRegistry()
    return _default_registry


def reset_default_registry(
    registry: CapabilityRegistry | None = None,
) -> None:
    global _default_registry
    _default_registry = registry


def _frame_source_ids(blocks: Sequence[ContentBlock]) -> set[str]:
    return {block.source_asset_id for block in blocks if isinstance(block, VideoFrameBlock)}


def _block_modality(block: ContentBlock, frame_sources: set[str]) -> Modality | None:
    if isinstance(block, VideoBlock) and block.asset_id in frame_sources:
        return "image"
    return INPUT_MODALITY_BY_BLOCK.get(type(block))


def required_modalities(blocks: Sequence[ContentBlock]) -> tuple[Modality, ...]:
    modalities: list[Modality] = ["text"]
    frame_sources = _frame_source_ids(blocks)
    for block in blocks:
        modality = _block_modality(block, frame_sources)
        if modality is None:
            continue
        if modality not in modalities:
            modalities.append(modality)
    return tuple(modalities)


def required_modalities_for_messages(messages: Sequence[Message]) -> tuple[Modality, ...]:
    modalities: list[Modality] = ["text"]
    frame_sources: set[str] = set()
    for message in messages:
        frame_sources.update(_frame_source_ids(getattr(message, "blocks", ())))
    for message in messages:
        for block in getattr(message, "blocks", ()):
            modality = _block_modality(block, frame_sources)
            if modality is None:
                continue
            if modality not in modalities:
                modalities.append(modality)
    return tuple(modalities)


def missing_modalities(
    capabilities: ModelCapabilities, modalities: Sequence[Modality]
) -> tuple[Modality, ...]:
    return tuple(m for m in modalities if not capabilities.accepts(m))


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    selected: ModelRef | None
    required: tuple[Modality, ...]
    rejected: tuple[tuple[ModelRef, tuple[Modality, ...]], ...]

    @property
    def ok(self) -> bool:
        return self.selected is not None

    def error(self) -> UnsupportedModalityError:
        if self.selected is not None:
            raise ValueError("routing decision succeeded; no error to raise")
        return UnsupportedModalityError(
            "no model in the routing chain supports the required modalities: "
            + ", ".join(self.required),
            required=self.required,
            candidates=self.rejected,
        )


def plan_routing(
    chain: Sequence[ModelRef],
    modalities: Sequence[Modality],
    *,
    registry: CapabilityRegistry | None = None,
    protocols: Mapping[str, str] | None = None,
) -> RoutingDecision:
    resolved = registry if registry is not None else default_registry()
    required = tuple(modalities) or ("text",)
    rejected: list[tuple[ModelRef, tuple[Modality, ...]]] = []
    for ref in chain:
        protocol = (protocols or {}).get(ref.provider, "")
        capabilities = resolved.lookup_ref(ref, protocol=protocol)
        missing = missing_modalities(capabilities, required)
        if not missing:
            return RoutingDecision(selected=ref, required=required, rejected=tuple(rejected))
        rejected.append((ref, missing))
    return RoutingDecision(selected=None, required=required, rejected=tuple(rejected))


def capable_chain(
    chain: Sequence[ModelRef],
    modalities: Sequence[Modality],
    *,
    registry: CapabilityRegistry | None = None,
    protocols: Mapping[str, str] | None = None,
) -> tuple[ModelRef, ...]:
    resolved = registry if registry is not None else default_registry()
    required = tuple(modalities) or ("text",)
    protocol_map = protocols or {}
    return tuple(
        ref
        for ref in chain
        if not missing_modalities(
            resolved.lookup_ref(ref, protocol=protocol_map.get(ref.provider, "")), required
        )
    )
