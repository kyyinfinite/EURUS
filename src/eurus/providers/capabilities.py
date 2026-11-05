from __future__ import annotations

from typing import Any

TEXT_ONLY_PROFILE: dict[str, bool] = {
    "textInput": True,
    "imageInput": False,
    "audioInput": False,
    "videoInput": False,
    "fileInput": True,
    "textOutput": True,
    "imageOutput": False,
    "audioOutput": False,
    "videoOutput": False,
    "toolCalling": True,
    "streaming": True,
}

PROTOCOL_CAPABILITIES: dict[str, dict[str, bool]] = {
    protocol: dict(TEXT_ONLY_PROFILE)
    for protocol in ("openai", "anthropic", "gemini", "groq-sdk", "genai-sdk")
}


def capabilities_for_protocol(protocol: str) -> dict[str, bool]:
    return dict(PROTOCOL_CAPABILITIES.get(protocol, TEXT_ONLY_PROFILE))


def capability_view(protocol: str) -> dict[str, Any]:
    capabilities = capabilities_for_protocol(protocol)
    return {
        "capabilities": capabilities,
        "multimodal": bool(
            capabilities["imageInput"] or capabilities["audioInput"] or capabilities["videoInput"]
        ),
    }
