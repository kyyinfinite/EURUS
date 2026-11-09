from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from eurus.agent.events import AgentEvent


@dataclass(frozen=True, slots=True)
class SessionEvent:
    session_id: str
    sequence: int
    event: AgentEvent
    task_id: str | None = None


RuntimeEvent = SessionEvent


@dataclass(slots=True)
class _Subscription:
    queue: asyncio.Queue[SessionEvent]
    task_id: str | None
    dropped: int = 0

    def accepts(self, envelope: SessionEvent) -> bool:
        return self.task_id is None or envelope.task_id is None or self.task_id == envelope.task_id


class AgentEventBridge:
    def __init__(self, *, default_capacity: int = 128, max_subscribers: int = 256) -> None:
        if default_capacity < 1:
            raise ValueError("event bridge capacity must be positive")
        if max_subscribers < 1:
            raise ValueError("max_subscribers must be positive")
        self.default_capacity = default_capacity
        self.max_subscribers = max_subscribers
        self._sequence = 0
        self._dropped_total = 0
        self._subscribers: dict[str, dict[str, _Subscription]] = {}
        self.closed = False

    @property
    def subscriber_count(self) -> int:
        return sum(len(items) for items in self._subscribers.values())

    @property
    def dropped_events(self) -> int:
        return self._dropped_total

    def subscribe(
        self,
        session_id: str,
        *,
        capacity: int | None = None,
        task_id: str | None = None,
    ) -> tuple[str, asyncio.Queue[SessionEvent]]:
        if self.closed:
            raise RuntimeError("event bridge is closed")
        if not session_id.strip():
            raise ValueError("session id must not be empty")
        if task_id is not None and not task_id.strip():
            raise ValueError("task id must not be empty")
        queue_capacity = self.default_capacity if capacity is None else capacity
        if queue_capacity < 1:
            raise ValueError("subscriber capacity must be positive")
        if self.subscriber_count >= self.max_subscribers:
            raise RuntimeError("event bridge subscriber limit reached")
        subscriber_id = uuid.uuid4().hex
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=queue_capacity)
        self._subscribers.setdefault(session_id, {})[subscriber_id] = _Subscription(queue, task_id)
        return subscriber_id, queue

    def unsubscribe(self, session_id: str, subscriber_id: str) -> bool:
        subscribers = self._subscribers.get(session_id)
        if subscribers is None or subscriber_id not in subscribers:
            return False
        del subscribers[subscriber_id]
        if not subscribers:
            del self._subscribers[session_id]
        return True

    def dropped_for(self, session_id: str, subscriber_id: str) -> int:
        subscription = self._subscribers.get(session_id, {}).get(subscriber_id)
        return 0 if subscription is None else subscription.dropped

    async def publish(
        self,
        session_id: str,
        event: AgentEvent,
        *,
        task_id: str | None = None,
    ) -> None:
        if self.closed:
            return
        self._sequence += 1
        envelope = SessionEvent(session_id, self._sequence, event, task_id)
        for subscription in tuple(self._subscribers.get(session_id, {}).values()):
            if not subscription.accepts(envelope):
                continue
            queue = subscription.queue
            if queue.full():
                queue.get_nowait()
                queue.task_done()
                subscription.dropped += 1
                self._dropped_total += 1
            queue.put_nowait(envelope)

    def close(self) -> None:
        self.closed = True
        self._subscribers.clear()
