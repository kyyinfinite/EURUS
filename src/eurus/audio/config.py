from __future__ import annotations

from dataclasses import dataclass

TTS_FORMATS = ("mp3", "opus", "aac", "flac", "wav")
DEFAULT_TTS_VOICE = "alloy"
DEFAULT_TTS_FORMAT = "mp3"
MAX_TTS_INPUT_CHARS = 4096
DEFAULT_STT_TIMEOUT_SECONDS = 180.0
DEFAULT_TTS_TIMEOUT_SECONDS = 120.0
DEFAULT_REALTIME_VOICE = "alloy"
DEFAULT_REALTIME_SESSION_SECONDS = 300.0
DEFAULT_REALTIME_TIMEOUT_SECONDS = 30.0
MAX_REALTIME_SESSION_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class AudioConfig:
    stt_model: str = ""
    stt_language: str = ""
    stt_timeout_seconds: float = DEFAULT_STT_TIMEOUT_SECONDS
    tts_model: str = ""
    tts_voice: str = DEFAULT_TTS_VOICE
    tts_format: str = DEFAULT_TTS_FORMAT
    tts_timeout_seconds: float = DEFAULT_TTS_TIMEOUT_SECONDS
    realtime_model: str = ""
    realtime_voice: str = DEFAULT_REALTIME_VOICE
    realtime_session_seconds: float = DEFAULT_REALTIME_SESSION_SECONDS
    realtime_timeout_seconds: float = DEFAULT_REALTIME_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.stt_timeout_seconds <= 0:
            raise ValueError("stt_timeout_seconds must be positive")
        if self.tts_timeout_seconds <= 0:
            raise ValueError("tts_timeout_seconds must be positive")
        if self.tts_format not in TTS_FORMATS:
            raise ValueError(f"tts_format must be one of {TTS_FORMATS}")
        if not self.tts_voice.strip():
            raise ValueError("tts_voice must not be empty")
        if self.stt_language and len(self.stt_language) > 32:
            raise ValueError("stt_language must be at most 32 characters")
        if not self.realtime_voice.strip():
            raise ValueError("realtime_voice must not be empty")
        if not 30.0 <= self.realtime_session_seconds <= MAX_REALTIME_SESSION_SECONDS:
            raise ValueError(
                f"realtime_session_seconds must be between 30 and {MAX_REALTIME_SESSION_SECONDS}"
            )
        if self.realtime_timeout_seconds <= 0:
            raise ValueError("realtime_timeout_seconds must be positive")
