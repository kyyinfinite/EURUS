from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eurus.content import ContentBlock
from eurus.events.multimodal import MultimodalEventPublisher, safe_publish
from eurus.media.errors import MediaError
from eurus.media.processing import MediaProcessor, run_processing_pipeline
from eurus.media.store import (
    MediaFile,
    MediaStore,
    attachment_content_blocks,
    compose_attachment_block,
)

if TYPE_CHECKING:
    from eurus.audio.stt import Transcriber
    from eurus.memory.media_index import MediaMemoryIndexer

log = logging.getLogger(__name__)

TELEGRAM_DOWNLOAD_TIMEOUT_SECONDS = 90.0
TELEGRAM_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
MEDIA_GROUP_WINDOW_SECONDS = 1.5
MEDIA_GROUP_MAX_ITEMS = 10
MAX_MEDIA_PER_MESSAGE = 8
DEFAULT_MEDIA_OBJECTIVE = "media attachment"

Downloader = Callable[[str], Awaitable[bytes]]

PHOTO_MIME = "image/jpeg"
VOICE_MIME = "audio/ogg"
VIDEO_NOTE_MIME = "video/mp4"

IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
VOICE_MIMES = frozenset({"audio/ogg"})
VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime"})


@dataclass(frozen=True, slots=True)
class TelegramMediaRef:
    file_id: str
    kind_hint: str
    filename: str = ""
    mime_hint: str = ""
    file_size: int | None = None
    caption: str = ""
    media_group_id: str = ""


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    blocks: tuple[ContentBlock, ...] = ()
    files: tuple[MediaFile, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.blocks)


class TelegramMediaIngester:
    def __init__(
        self,
        store: MediaStore,
        downloader: Downloader,
        *,
        processor: MediaProcessor | None = None,
        events: MultimodalEventPublisher | None = None,
        download_timeout: float = TELEGRAM_DOWNLOAD_TIMEOUT_SECONDS,
        transcriber: Transcriber | None = None,
        media_memory: MediaMemoryIndexer | None = None,
    ) -> None:
        if download_timeout <= 0:
            raise ValueError("download_timeout must be positive")
        self.store = store
        self.downloader = downloader
        self.processor = processor if processor is not None else MediaProcessor()
        self.events = events
        self.download_timeout = download_timeout
        self.transcriber = transcriber
        self.media_memory = media_memory

    async def ingest(
        self,
        session_id: str,
        refs: Sequence[TelegramMediaRef],
        *,
        owner_id: str = "",
        source: str = "telegram",
    ) -> IngestOutcome:
        blocks: list[ContentBlock] = []
        files: list[MediaFile] = []
        errors: list[str] = []
        for ref in list(refs)[:MAX_MEDIA_PER_MESSAGE]:
            try:
                media = await self._ingest_one(session_id, ref, owner_id=owner_id, source=source)
            except MediaError as exc:
                errors.append(f"{ref.filename or ref.kind_hint}: {exc}")
                continue
            except TimeoutError:
                errors.append(f"{ref.filename or ref.kind_hint}: download timed out")
                continue
            except Exception as exc:
                log.warning(
                    "telegram media ingest failed file_id=%s error_type=%s",
                    ref.file_id,
                    type(exc).__name__,
                    exc_info=True,
                )
                errors.append(f"{ref.filename or ref.kind_hint}: download failed")
                continue
            if media is None:
                continue
            files.append(media)
            blocks.extend(attachment_content_blocks([media]))
        return IngestOutcome(blocks=tuple(blocks), files=tuple(files), errors=tuple(errors))

    async def _ingest_one(
        self,
        session_id: str,
        ref: TelegramMediaRef,
        *,
        owner_id: str,
        source: str,
    ) -> MediaFile | None:
        if ref.file_size is not None:
            self.store.limits.validate_size(ref.file_size)
        try:
            data = await asyncio.wait_for(self.downloader(ref.file_id), self.download_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except Exception as exc:
            raise MediaError(
                f"telegram download failed: {type(exc).__name__}",
            ) from exc
        media = await asyncio.to_thread(
            self.store.save,
            session_id,
            ref.filename or f"{ref.kind_hint}-{ref.file_id[-8:]}",
            data,
            declared_mime=(ref.mime_hint or None),
            owner_id=owner_id,
        )
        await run_processing_pipeline(
            self.store, media, self.processor, data=data, events=self.events
        )
        resolved = await asyncio.to_thread(self.store.get, media.id)
        current = resolved or media
        if self.transcriber is not None and current.kind == "audio":
            from eurus.audio.pipeline import transcribe_media

            await transcribe_media(
                self.store,
                current,
                self.transcriber,
                events=self.events,
            )
            refreshed = await asyncio.to_thread(self.store.get, media.id)
            current = refreshed or current
        elif self.transcriber is not None and current.kind == "video":
            from eurus.audio.pipeline import transcribe_video_audio

            await transcribe_video_audio(
                self.store,
                current,
                self.transcriber,
                events=self.events,
            )
            refreshed = await asyncio.to_thread(self.store.get, media.id)
            current = refreshed or current
        if self.media_memory is not None:
            await asyncio.to_thread(_index_media_memory, self.media_memory, current)
        if self.events is not None:
            view = current.view()
            await asyncio.to_thread(
                safe_publish,
                lambda: self.events.media_received(
                    view,
                    source=source,
                    correlation_id=media.id,
                ),
                context=f"telegram-media:{media.id}",
            )
        return current

    def objective_text(self, caption: str, outcome: IngestOutcome) -> str:
        marker = compose_attachment_block(list(outcome.files))
        text = (caption or "").strip()
        notes = ""
        if any(_video_without_frames(file) for file in outcome.files):
            notes = (
                "\nNote: keyframe extraction is unavailable for one or more attached "
                "videos; only metadata and any transcript can be used."
            )
        if text and marker:
            return f"{text}\n{marker}{notes}"
        if marker:
            return f"{marker}{notes}"
        return DEFAULT_MEDIA_OBJECTIVE


class TelegramMediaSender:
    def __init__(
        self,
        bot: Any,
        store: MediaStore | None = None,
        *,
        upload_limit_bytes: int = TELEGRAM_UPLOAD_LIMIT_BYTES,
        max_attempts: int = 3,
    ) -> None:
        if upload_limit_bytes < 1:
            raise ValueError("upload_limit_bytes must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.bot = bot
        self.store = store
        self.upload_limit_bytes = upload_limit_bytes
        self.max_attempts = max_attempts

    async def send_media(self, chat_id: int, media: MediaFile, caption: str = "") -> None:
        if media.size > self.upload_limit_bytes:
            raise MediaError(
                f"media exceeds the Telegram upload limit of {self.upload_limit_bytes} bytes"
            )
        if self.store is None:
            raise MediaError("media sender is not bound to a media store")
        path = await asyncio.to_thread(self.store.storage_path_for, media.id)
        if path is None:
            raise MediaError("media file is no longer available")
        data = await asyncio.to_thread(path.read_bytes)
        await self.send_bytes(chat_id, media.filename, media.mime, data, caption=caption)

    async def send_bytes(
        self, chat_id: int, filename: str, mime: str, data: bytes, caption: str = ""
    ) -> None:
        if len(data) > self.upload_limit_bytes:
            raise MediaError(
                f"media exceeds the Telegram upload limit of {self.upload_limit_bytes} bytes"
            )
        method, kwargs = self._dispatch(chat_id, filename, mime, data, caption)
        await self._send_with_retry(method, kwargs, filename)

    def _dispatch(
        self, chat_id: int, filename: str, mime: str, data: bytes, caption: str
    ) -> tuple[str, dict[str, Any]]:
        from aiogram.types import BufferedInputFile

        safe_name = filename.strip() or "file"
        file = BufferedInputFile(data, filename=safe_name)
        caption_text = caption.strip()[:MAX_CAPTION_CHARS] if caption.strip() else None
        if mime in IMAGE_MIMES:
            return "send_photo", {
                "chat_id": chat_id,
                "photo": file,
                "caption": caption_text,
            }
        if mime in VOICE_MIMES:
            return "send_voice", {"chat_id": chat_id, "voice": file, "caption": caption_text}
        if mime in VIDEO_MIMES:
            return "send_video", {
                "chat_id": chat_id,
                "video": file,
                "caption": caption_text,
            }
        if mime.startswith("audio/"):
            return "send_audio", {
                "chat_id": chat_id,
                "audio": file,
                "caption": caption_text,
            }
        return "send_document", {
            "chat_id": chat_id,
            "document": file,
            "caption": caption_text,
        }

    async def _send_with_retry(self, method: str, kwargs: dict[str, Any], label: str) -> None:
        send = getattr(self.bot, method)
        for attempt in range(self.max_attempts):
            try:
                await send(**kwargs)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                retryable = retry_after is not None or type(exc).__name__ in {
                    "TelegramNetworkError",
                    "TimeoutError",
                    "ConnectionError",
                    "OSError",
                }
                if not retryable or attempt + 1 >= self.max_attempts:
                    log.warning(
                        "telegram media delivery failed label=%s method=%s error_type=%s",
                        label,
                        method,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    raise
                delay = (
                    min(float(retry_after), 30.0)
                    if retry_after is not None
                    else min(0.5 * (2**attempt), 8.0)
                )
                await asyncio.sleep(delay)


MAX_CAPTION_CHARS = 1024
VOICE_MARKER_PATTERN = re.compile(r"\[voice:([a-f0-9]{32})\]")
MEDIA_MARKER_PATTERN = re.compile(r"\[(voice|image|video):([a-f0-9]{32})\]")


async def deliver_media_responses(
    chat_id: int,
    answer: str,
    store: MediaStore,
    sender: TelegramMediaSender,
) -> str:
    markers = MEDIA_MARKER_PATTERN.findall(answer)
    if not markers:
        return answer
    for kind, asset_id in markers:
        media = await asyncio.to_thread(store.get, asset_id)
        if media is None:
            log.warning("generated media asset missing asset_id=%s kind=%s", asset_id, kind)
            continue
        try:
            await sender.send_media(chat_id, media)
        except Exception:
            log.warning(
                "generated media delivery failed asset_id=%s kind=%s", asset_id, kind, exc_info=True
            )
    cleaned = MEDIA_MARKER_PATTERN.sub("", answer).strip()
    if cleaned:
        return cleaned
    kinds = {kind for kind, _asset in markers}
    if kinds == {"voice"}:
        return "Voice response delivered."
    if kinds == {"image"}:
        return "Image response delivered."
    if kinds == {"video"}:
        return "Video response delivered."
    return "Media response delivered."


async def deliver_voice_responses(
    chat_id: int,
    answer: str,
    store: MediaStore,
    sender: TelegramMediaSender,
) -> str:
    return await deliver_media_responses(chat_id, answer, store, sender)


class MediaGroupCollector:
    def __init__(
        self,
        flush: Callable[[int, str, list[TelegramMediaRef], str], Awaitable[None]],
        *,
        window_seconds: float = MEDIA_GROUP_WINDOW_SECONDS,
        max_items: int = MEDIA_GROUP_MAX_ITEMS,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._flush = flush
        self._window_seconds = window_seconds
        self._max_items = max_items
        self._pending: dict[str, list[TelegramMediaRef]] = {}
        self._captions: dict[str, str] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    async def add(self, chat_id: int, group_id: str, ref: TelegramMediaRef) -> None:
        key = f"{chat_id}:{group_id}"
        bucket = self._pending.setdefault(key, [])
        bucket.append(ref)
        if not self._captions.get(key):
            self._captions[key] = ref.caption
        timer = self._timers.get(key)
        if timer is not None and not timer.done():
            timer.cancel()
        if len(bucket) >= self._max_items:
            await self._flush_key(key)
            return
        self._timers[key] = asyncio.create_task(self._expire(key))

    async def _expire(self, key: str) -> None:
        await asyncio.sleep(self._window_seconds)
        await self._flush_key(key)

    async def _flush_key(self, key: str) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None and not timer.done():
            timer.cancel()
        refs = self._pending.pop(key, None) or []
        caption = self._captions.pop(key, "")
        if not refs:
            return
        chat_id_text, _, group_id = key.partition(":")
        try:
            chat_id = int(chat_id_text)
        except ValueError:
            log.warning("media group key malformed key=%s", key)
            return
        try:
            await self._flush(chat_id, group_id, refs, caption)
        except Exception:
            log.warning("media group flush failed key=%s", key, exc_info=True)

    async def close(self) -> None:
        keys = list(self._pending)
        for key in keys:
            await self._flush_key(key)
        for timer in self._timers.values():
            if not timer.done():
                timer.cancel()


def extract_telegram_media(message: Any) -> list[TelegramMediaRef]:
    refs: list[TelegramMediaRef] = []
    caption = str(getattr(message, "caption", "") or "")
    group_id = str(getattr(message, "media_group_id", "") or "")
    photo = getattr(message, "photo", None)
    if photo:
        largest = max(photo, key=lambda item: int(getattr(item, "file_size", 0) or 0))
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(largest, "file_id", "")),
                kind_hint="image",
                filename="photo.jpg",
                mime_hint=PHOTO_MIME,
                file_size=_optional_int(getattr(largest, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    voice = getattr(message, "voice", None)
    if voice is not None:
        duration = int(getattr(voice, "duration", 0) or 0)
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(voice, "file_id", "")),
                kind_hint="audio",
                filename=f"voice-{duration}s.ogg",
                mime_hint=VOICE_MIME,
                file_size=_optional_int(getattr(voice, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    audio = getattr(message, "audio", None)
    if audio is not None:
        mime = str(getattr(audio, "mime_type", "") or "")
        filename = str(getattr(audio, "file_name", "") or "") or "audio"
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(audio, "file_id", "")),
                kind_hint="audio",
                filename=filename,
                mime_hint=mime,
                file_size=_optional_int(getattr(audio, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    video = getattr(message, "video", None)
    if video is not None:
        mime = str(getattr(video, "mime_type", "") or "") or "video/mp4"
        filename = str(getattr(video, "file_name", "") or "") or "video.mp4"
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(video, "file_id", "")),
                kind_hint="video",
                filename=filename,
                mime_hint=mime,
                file_size=_optional_int(getattr(video, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(video_note, "file_id", "")),
                kind_hint="video",
                filename="video_note.mp4",
                mime_hint=VIDEO_NOTE_MIME,
                file_size=_optional_int(getattr(video_note, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "")
        kind_hint = _kind_for_mime(mime)
        filename = str(getattr(document, "file_name", "") or "") or "document"
        refs.append(
            TelegramMediaRef(
                file_id=str(getattr(document, "file_id", "")),
                kind_hint=kind_hint,
                filename=filename,
                mime_hint=mime,
                file_size=_optional_int(getattr(document, "file_size", None)),
                caption=caption,
                media_group_id=group_id,
            )
        )
    return [ref for ref in refs if ref.file_id]


def _kind_for_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "document"


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _video_without_frames(file: MediaFile) -> bool:
    if file.kind != "video":
        return False
    frames = file.metadata.get("frames")
    return not isinstance(frames, list) or not frames


def _index_media_memory(indexer: Any, media: MediaFile) -> None:
    try:
        indexer.index_media(media)
    except Exception:
        log.warning("media memory indexing failed media_id=%s", media.id, exc_info=True)
