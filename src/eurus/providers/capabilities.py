from __future__ import annotations

from typing import Any

from eurus.providers.model_capabilities import (
    DEFAULT_CAPABILITIES,
    ModelCapabilities,
    default_registry,
)

TEXT_ONLY_PROFILE: dict[str, bool] = DEFAULT_CAPABILITIES.to_view()

PROTOCOL_CAPABILITIES: dict[str, dict[str, bool]] = {
    protocol: default_registry().protocol_default(protocol).to_view()
    for protocol in ("openai", "anthropic", "gemini", "groq-sdk", "genai-sdk")
}


def capabilities_for_protocol(protocol: str) -> dict[str, bool]:
    return default_registry().protocol_default(protocol).to_view()


def capability_view(protocol: str) -> dict[str, Any]:
    capabilities = capabilities_for_protocol(protocol)
    return {
        "capabilities": capabilities,
        "multimodal": bool(
            capabilities["imageInput"] or capabilities["audioInput"] or capabilities["videoInput"]
        ),
    }


def model_capability_view(provider: str, model: str, *, protocol: str = "") -> dict[str, Any]:
    capabilities = default_registry().lookup(provider, model, protocol=protocol)
    return {
        "capabilities": capabilities.to_view(),
        "multimodal": capabilities.multimodal,
    }


def model_capabilities(
    provider: str,
    model: str,
    *,
    protocol: str = "",
    registry: Any = None,
) -> ModelCapabilities:
    resolved = registry if registry is not None else default_registry()
    return resolved.lookup(provider, model, protocol=protocol)
