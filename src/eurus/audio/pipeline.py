from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from eurus.audio.stt import Transcriber, TranscriptionResult
from eurus.audio.tts import SpeechSynthesizer, speech_filename
from eurus.events.multimodal import MultimodalEventPublisher, safe_publish
from eurus.events.types import (
    MEDIA_PROCESSING_FAILED,
    MEDIA_TRANSCRIPTION_COMPLETED,
    MEDIA_TRANSCRIPTION_STARTED,
)
from eurus.media.store import MediaFile, MediaStore

log = logging.getLogger(__name__)

TRANSCRIPT_METADATA_KEY = "transcript"
TRANSCRIPT_LANGUAGE_METADATA_KEY = "transcript_language"
MAX_TRANSCRIPT_METADATA_CHARS = 20_000


async def transcribe_media(
    store: MediaStore,
    media: MediaFile,
    transcriber: Transcriber,
    *,
    events: MultimodalEventPublisher | None = None,
    language: str | None = None,
) -> TranscriptionResult | None:
    if media.kind != "audio":
        return None
    await _publish(
        events,
        MEDIA_TRANSCRIPTION_STARTED,
        media,
        {"language": (language or "").strip()},
    )
    try:
        data = await _read_media_payload(store, media)
        result = await transcriber.transcribe(data, media.mime, language=language)
    except Exception as exc:
        log.warning(
            "audio transcription failed media_id=%s error_type=%s",
            media.id,
            type(exc).__name__,
            exc_info=True,
        )
        await _publish(
            events,
            MEDIA_PROCESSING_FAILED,
            media,
            {
                "stage": "transcription",
                "error_code": "transcription_failed",
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        return None
    metadata = {TRANSCRIPT_METADATA_KEY: result.text[:MAX_TRANSCRIPT_METADATA_CHARS]}
    if result.language:
        metadata[TRANSCRIPT_LANGUAGE_METADATA_KEY] = result.language
    await asyncio.to_thread(store.update_status, media.id, "ready", metadata=metadata)
    await _publish(
        events,
        MEDIA_TRANSCRIPTION_COMPLETED,
        media,
        {
            "text": result.text[:4000],
            "language": result.language,
            "duration_seconds": result.duration_seconds,
        },
    )
    return result


async def transcribe_video_audio(
    store: MediaStore,
    media: MediaFile,
    transcriber: Transcriber,
    *,
    events: MultimodalEventPublisher | None = None,
    language: str | None = None,
) -> TranscriptionResult | None:
    if media.kind != "video":
        return None
    audio_media = await _resolve_video_audio_asset(store, media)
    if audio_media is None:
        return None
    await _publish(
        events,
        MEDIA_TRANSCRIPTION_STARTED,
        media,
        {"language": (language or "").strip()},
    )
    try:
        audio_data = await _read_media_payload(store, audio_media)
        result = await transcriber.transcribe(audio_data, audio_media.mime, language=language)
    except Exception as exc:
        log.warning(
            "video audio transcription failed media_id=%s error_type=%s",
            media.id,
            type(exc).__name__,
            exc_info=True,
        )
        await _publish(
            events,
            MEDIA_PROCESSING_FAILED,
            media,
            {
                "stage": "transcription",
                "error_code": "transcription_failed",
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        return None
    metadata = {TRANSCRIPT_METADATA_KEY: result.text[:MAX_TRANSCRIPT_METADATA_CHARS]}
    if result.language:
        metadata[TRANSCRIPT_LANGUAGE_METADATA_KEY] = result.language
    await asyncio.to_thread(store.update_status, media.id, "ready", metadata=metadata)
    await _publish(
        events,
        MEDIA_TRANSCRIPTION_COMPLETED,
        media,
        {
            "text": result.text[:4000],
            "language": result.language,
            "duration_seconds": result.duration_seconds,
        },
    )
    return result


async def _resolve_video_audio_asset(store: MediaStore, media: MediaFile) -> MediaFile | None:
    audio_asset_id = media.metadata.get("audio_asset_id")
    if isinstance(audio_asset_id, str) and audio_asset_id:
        existing = await asyncio.to_thread(store.get, audio_asset_id)
        if existing is not None:
            return existing
    from eurus.media.video import extract_audio_track

    try:
        data = await _read_media_payload(store, media)
    except (FileNotFoundError, OSError, ValueError):
        log.warning("video payload unreadable media_id=%s", media.id, exc_info=True)
        return None
    duration = media.metadata.get("duration_seconds")
    extracted = await asyncio.to_thread(
        extract_audio_track,
        data,
        duration_seconds=(
            float(duration)
            if isinstance(duration, int | float) and not isinstance(duration, bool)
            else None
        ),
    )
    if extracted is None:
        return None
    audio_data, _truncated = extracted
    try:
        audio_media = await asyncio.to_thread(
            store.save,
            media.session_id,
            _audio_track_name(media.filename),
            audio_data,
            declared_mime="audio/wav",
            owner_id=media.owner_id,
        )
    except Exception:
        log.warning("video audio asset storage failed media_id=%s", media.id, exc_info=True)
        return None
    await asyncio.to_thread(
        store.update_status,
        media.id,
        "ready",
        metadata={"audio_asset_id": audio_media.id},
    )
    return audio_media


def _audio_track_name(source: str) -> str:
    base = source.rsplit(".", 1)[0] if "." in source else source
    return f"{base or 'video'}-audio.wav"


async def synthesize_speech(
    store: MediaStore,
    text: str,
    synthesizer: SpeechSynthesizer,
    *,
    session_id: str,
    owner_id: str = "",
    voice: str | None = None,
    audio_format: str | None = None,
) -> MediaFile | None:
    try:
        result = await synthesizer.synthesize(text, voice=voice, audio_format=audio_format)
    except Exception as exc:
        log.warning("speech synthesis failed error_type=%s", type(exc).__name__, exc_info=True)
        return None
    try:
        media = await asyncio.to_thread(
            store.save,
            session_id,
            speech_filename(result.audio_format),
            result.data,
            declared_mime=result.mime,
            owner_id=owner_id,
            deduplicate=False,
        )
    except Exception:
        log.warning("speech asset storage failed", exc_info=True)
        return None
    return media


async def _read_media_payload(store: MediaStore, media: MediaFile) -> bytes:
    path = await asyncio.to_thread(store.storage_path_for, media.id)
    if path is None:
        raise FileNotFoundError(f"media payload missing for {media.id}")
    data = await asyncio.to_thread(path.read_bytes)
    if not data:
        raise ValueError(f"media payload is empty for {media.id}")
    return data


async def _publish(
    events: MultimodalEventPublisher | None,
    event_type: str,
    media: MediaFile,
    payload: dict[str, Any],
) -> None:
    if events is None:
        return
    enriched = dict(payload)
    enriched["media_id"] = media.id
    if media.session_id:
        enriched.setdefault("session_id", media.session_id)
    await asyncio.to_thread(
        safe_publish,
        lambda: events.media_event(
            event_type,
            media.id,
            enriched,
            session_id=media.session_id,
        ),
        context=f"audio-pipeline:{media.id}:{event_type}",
    )


def media_session_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
