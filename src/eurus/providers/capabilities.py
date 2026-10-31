"""Model input/output capability metadata for EURUS providers.

The EURUS agent loop currently builds **text-only** prompts for every
supported protocol (``openai``, ``anthropic``, ``gemini``, ``groq-sdk``,
``genai-sdk``). Attachments uploaded through the canonical media store are
delivered to the model as inlined text (for text-like formats) or as
structured metadata references — binary payloads are *not* decoded by any
provider adapter yet.

This module is the single honest source of that truth. Interfaces (web
gateway, TUI, CLI) use it to tell users what a provider can actually do
instead of implying multimodal support that does not exist. When a provider
adapter gains real image/audio/video input or output, update the profile for
that protocol here — and only here.
"""

from __future__ import annotations

from typing import Any

# Capability profile per protocol. Today every protocol shares the same
# conservative, text-only profile. Keys follow the web gateway contract.
TEXT_ONLY_PROFILE: dict[str, bool] = {
    "textInput": True,
    "imageInput": False,
    "audioInput": False,
    "videoInput": False,
    "fileInput": True,  # via canonical media references / text inlining
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
    """Honest capability map for a provider protocol (text-only today)."""
    return dict(PROTOCOL_CAPABILITIES.get(protocol, TEXT_ONLY_PROFILE))


def capability_view(protocol: str) -> dict[str, Any]:
    """JSON-friendly view with a human note explaining the profile."""
    capabilities = capabilities_for_protocol(protocol)
    return {
        "capabilities": capabilities,
        "multimodal": bool(
            capabilities["imageInput"]
            or capabilities["audioInput"]
            or capabilities["videoInput"]
        ),
    }
