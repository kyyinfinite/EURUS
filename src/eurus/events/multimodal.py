from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from eurus.events.store import DomainEvent, DomainEventStore
from eurus.events.types import (
    MEDIA_RECEIVED,
    MEDIA_SUBJECT_TYPE,
    MESSAGE_RECEIVED,
    MESSAGE_SUBJECT_TYPE,
    EventPayloadError,
    media_received_event_type,
    validate_payload,
)

log = logging.getLogger(__name__)

MAX_TEXT_SNAPSHOT_CHARS = 2000


class MultimodalEventPublisher:
    def __init__(self, store: DomainEventStore, *, owner_id: str = "") -> None:
        self.store = store
        self.owner_id = owner_id

    def message_received(
        self,
        session_id: str,
        *,
        message_id: str = "",
        text: str = "",
        media_ids: Sequence[str] = (),
        source: str = "",
        correlation_id: str = "",
    ) -> DomainEvent | None:
        payload = validate_payload(
            MESSAGE_RECEIVED,
            {
                "session_id": session_id,
                "message_id": message_id,
                "text": text[:MAX_TEXT_SNAPSHOT_CHARS],
                "media_ids": list(media_ids),
                "source": source,
            },
        )
        dedup_key = f"message-received:{source}:{message_id}" if message_id else None
        return self.store.publish(
            MESSAGE_RECEIVED,
            subject_type=MESSAGE_SUBJECT_TYPE,
            subject_id=message_id or session_id,
            payload=payload,
            correlation_id=correlation_id,
            dedup_key=dedup_key,
            owner_id=self.owner_id,
        )

    def media_received(
        self,
        media: Mapping[str, Any],
        *,
        source: str = "",
        correlation_id: str = "",
    ) -> list[DomainEvent]:
        published: list[DomainEvent] = []
        generic = self._publish_media_event(
            MEDIA_RECEIVED,
            media,
            source=source,
            correlation_id=correlation_id,
            dedup_prefix="media-received",
        )
        if generic is not None:
            published.append(generic)
        kind = str(media.get("kind", ""))
        if kind:
            specific_type = media_received_event_type(kind)
            specific = self._publish_media_event(
                specific_type,
                media,
                source=source,
                correlation_id=correlation_id,
                dedup_prefix=f"{kind}-received",
            )
            if specific is not None:
                published.append(specific)
        return published

    def media_event(
        self,
        event_type: str,
        media_id: str,
        payload: Mapping[str, Any],
        *,
        session_id: str = "",
        dedup_key: str | None = None,
        correlation_id: str = "",
        causation_id: str = "",
    ) -> DomainEvent | None:
        enriched = dict(payload)
        enriched["media_id"] = media_id
        if session_id:
            enriched.setdefault("session_id", session_id)
        validated = validate_payload(event_type, enriched)
        return self.store.publish(
            event_type,
            subject_type=MEDIA_SUBJECT_TYPE,
            subject_id=media_id,
            payload=validated,
            correlation_id=correlation_id,
            causation_id=causation_id,
            dedup_key=dedup_key,
            owner_id=self.owner_id,
        )

    def _publish_media_event(
        self,
        event_type: str,
        media: Mapping[str, Any],
        *,
        source: str,
        correlation_id: str,
        dedup_prefix: str,
    ) -> DomainEvent | None:
        media_id = str(media.get("id") or media.get("media_id") or "")
        payload = validate_payload(
            event_type,
            {
                "media_id": media_id,
                "session_id": media.get("session_id", ""),
                "kind": media.get("kind", ""),
                "mime": media.get("mime", media.get("mime_type", "")),
                "size": media.get("size", 0),
                "sha256": media.get("sha256", ""),
                "filename": media.get("filename", media.get("name", "")),
                "source": source,
            },
        )
        return self.store.publish(
            event_type,
            subject_type=MEDIA_SUBJECT_TYPE,
            subject_id=media_id,
            payload=payload,
            correlation_id=correlation_id,
            dedup_key=f"{dedup_prefix}:{media_id}",
            owner_id=self.owner_id,
        )


def safe_publish(call: Any, *, context: str) -> None:
    try:
        call()
    except (EventPayloadError, ValueError) as exc:
        log.warning("multimodal event rejected context=%s error=%s", context, exc)
    except Exception:
        log.warning("multimodal event publish failed context=%s", context, exc_info=True)
