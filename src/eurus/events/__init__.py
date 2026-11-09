from typing import Any

__all__ = [
    "EventBus",
    "DomainEventStore",
    "DomainEvent",
    "EventSubscription",
    "MULTIMODAL_EVENT_TYPES",
    "INBOUND_EVENT_TYPES",
    "MEDIA_PROCESSING_EVENT_TYPES",
]


def __getattr__(name: str) -> Any:
    if name == "EventBus":
        from eurus.events.bus import EventBus

        return EventBus
    if name == "DomainEventStore":
        from eurus.events.store import DomainEventStore

        return DomainEventStore
    if name == "DomainEvent":
        from eurus.events.store import DomainEvent

        return DomainEvent
    if name == "EventSubscription":
        from eurus.events.store import EventSubscription

        return EventSubscription
    if name in {"MULTIMODAL_EVENT_TYPES", "INBOUND_EVENT_TYPES", "MEDIA_PROCESSING_EVENT_TYPES"}:
        from eurus.events import types

        return getattr(types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
