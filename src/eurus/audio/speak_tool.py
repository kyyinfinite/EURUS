from __future__ import annotations

import logging
from typing import Any

from eurus.audio.config import TTS_FORMATS
from eurus.audio.pipeline import synthesize_speech
from eurus.audio.tts import SpeechSynthesizer
from eurus.media.store import MediaStore
from eurus.tools.base import Tool, ToolContext

log = logging.getLogger(__name__)

SPEAK_TOOL_NAME = "speak"
VOICE_MARKER_TEMPLATE = "[voice:{media_id}]"

SPEAK_DESCRIPTION = (
    "Convert text into a spoken audio reply using the configured text-to-speech provider. "
    "Use it when the user asks for a voice or audio answer (for example 'jawab pakai suara', "
    "'read this out loud', 'make an audio of this'). "
    "After the tool succeeds, include the returned [voice:...] marker EXACTLY as given inside "
    "your final answer so the gateway can deliver the audio file; also write a short normal "
    "text answer for chats that cannot play audio. "
    "Use audio_format 'opus' to produce a Telegram-compatible voice message."
)

SPEAK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The text to speak aloud, in the same language as the reply.",
        },
        "voice": {
            "type": "string",
            "description": "Optional voice name override (provider-dependent).",
        },
        "audio_format": {
            "type": "string",
            "enum": list(TTS_FORMATS),
            "description": "Output audio format; 'opus' sends as a Telegram voice message.",
        },
    },
    "required": ["text"],
}


def speak_tool(
    store: MediaStore,
    synthesizer: SpeechSynthesizer,
    *,
    default_voice: str | None = None,
    default_format: str | None = None,
) -> Tool:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return "speak failed: text is required"
        voice = str(arguments.get("voice") or "") or (default_voice or "")
        audio_format = str(arguments.get("audio_format") or "") or (default_format or "")
        session_id = (context.session_id or "voice-output").strip() or "voice-output"
        owner_id = (context.owner_id or "local").strip() or "local"
        media = await synthesize_speech(
            store,
            text,
            synthesizer,
            session_id=session_id,
            owner_id=owner_id,
            voice=voice or None,
            audio_format=audio_format or None,
        )
        if media is None:
            return "speech synthesis failed; answer in text only"
        sink = context.event_sink
        if sink is not None:
            try:
                sink(
                    "audio.output.ready",
                    {
                        "asset_id": media.id,
                        "mime": media.mime,
                        "size": media.size,
                        "session_id": session_id,
                    },
                )
            except Exception:
                log.debug("audio output event emission failed", exc_info=True)
        marker = VOICE_MARKER_TEMPLATE.format(media_id=media.id)
        return (
            f"{marker}\nAudio reply saved ({media.mime}, {media.size} bytes). "
            "Include the marker above verbatim in your final answer so the audio is delivered."
        )

    return Tool(
        name=SPEAK_TOOL_NAME,
        description=SPEAK_DESCRIPTION,
        parameters=SPEAK_SCHEMA,
        handler=handler,
        mutates=False,
    )
