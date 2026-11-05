from typing import Any

__all__ = ["EventBus", "DomainEventStore", "DomainEvent", "EventSubscription"]


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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
