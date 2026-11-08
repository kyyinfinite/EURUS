from __future__ import annotations

from eurus.audio.config import AudioConfig
from eurus.audio.pipeline import synthesize_speech, transcribe_media
from eurus.audio.speak_tool import SPEAK_TOOL_NAME, speak_tool
from eurus.audio.stt import (
    OpenAICompatTranscriber,
    Transcriber,
    TranscriptionError,
    TranscriptionResult,
    build_transcriber,
)
from eurus.audio.tts import (
    OpenAICompatSpeechSynthesizer,
    SpeechResult,
    SpeechSynthesisError,
    SpeechSynthesizer,
    build_speech_synthesizer,
)

__all__ = [
    "AudioConfig",
    "OpenAICompatSpeechSynthesizer",
    "OpenAICompatTranscriber",
    "SPEAK_TOOL_NAME",
    "SpeechResult",
    "SpeechSynthesisError",
    "SpeechSynthesizer",
    "Transcriber",
    "TranscriptionError",
    "TranscriptionResult",
    "build_speech_synthesizer",
    "build_transcriber",
    "speak_tool",
    "synthesize_speech",
    "transcribe_media",
]
