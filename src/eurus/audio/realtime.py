from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

RealtimeSessionState = Literal[
    "idle",
    "connecting",
    "listening",
    "speaking",
    "interrupted",
    "reconnecting",
    "closed",
]


@dataclass(frozen=True, slots=True)
class RealtimeAudioChunk:
    data: bytes
    mime: str
    sequence: int
    is_final: bool = False


class RealtimeAudioInputStream(Protocol):
    def chunks(self) -> AsyncIterator[RealtimeAudioChunk]: ...

    async def close(self) -> None: ...


class RealtimeAudioOutputStream(Protocol):
    async def send(self, chunk: RealtimeAudioChunk) -> None: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...


class RealtimeVoiceSession(Protocol):
    @property
    def state(self) -> RealtimeSessionState: ...

    async def connect(self) -> None: ...

    async def interrupt(self) -> None: ...

    async def reconnect(self) -> None: ...

    async def close(self) -> None: ...


class RealtimeVoiceTransport(Protocol):
    async def open_session(self) -> RealtimeVoiceSession: ...


SUPPORTED_REALTIME_PROTOCOLS: tuple[str, ...] = ("openai-realtime",)

REALTIME_SCOPE_NOTE = (
    "Realtime voice sessions are a Batch 3 scope item: the abstraction above defines the "
    "stream, session lifecycle, interruption, and reconnection surface, but no transport is "
    "implemented in this batch. Providers must declare the realtime_audio capability before "
    "a session can be opened."
)
