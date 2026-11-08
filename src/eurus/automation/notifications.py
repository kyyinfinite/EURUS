from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

NOTIFICATION_KINDS = frozenset(
    {
        "job_completed",
        "job_failed",
        "job_retrying",
        "job_cancelled",
        "job_unknown_outcome",
        "goal_completed",
        "goal_failed",
        "workflow_completed",
        "workflow_failed",
        "workflow_notification",
        "approval_requested",
        "dead_letter",
    }
)


@dataclass(frozen=True, slots=True)
class Notification:
    kind: str
    title: str
    body: str = ""
    owner_id: str = ""
    correlation_id: str = ""
    job_id: str = ""
    result: str = ""
    deliver: str = ""
    origin_gateway: str = ""
    origin_conversation: str = ""
    silent: bool = False
    attempt: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.kind not in NOTIFICATION_KINDS:
            raise ValueError(f"unknown notification kind: {self.kind}")
        if not self.title.strip():
            raise ValueError("notification title is required")


class NotificationSink(Protocol):
    async def deliver(self, notification: Notification) -> None: ...


class LoggingNotificationSink:
    async def deliver(self, notification: Notification) -> None:
        notification.validate()
        log.info(
            "notification kind=%s owner=%s title=%s",
            notification.kind,
            notification.owner_id,
            notification.title,
        )


class CollectingNotificationSink:
    def __init__(self) -> None:
        self.delivered: list[Notification] = []

    async def deliver(self, notification: Notification) -> None:
        notification.validate()
        self.delivered.append(notification)

    def clear(self) -> None:
        self.delivered.clear()


class CompositeNotificationSink:
    def __init__(self, sinks: Sequence[NotificationSink]) -> None:
        if not sinks:
            raise ValueError("at least one notification sink is required")
        self.sinks = tuple(sinks)

    async def deliver(self, notification: Notification) -> None:
        for sink in self.sinks:
            await sink.deliver(notification)


class ThrottledNotificationSink:
    def __init__(self, inner: NotificationSink, *, max_per_kind: int = 100) -> None:
        if max_per_kind < 1:
            raise ValueError("max_per_kind must be positive")
        self.inner = inner
        self.max_per_kind = max_per_kind
        self.counts: dict[str, int] = {}

    async def deliver(self, notification: Notification) -> None:
        count = self.counts.get(notification.kind, 0)
        if count >= self.max_per_kind:
            return
        self.counts[notification.kind] = count + 1
        await self.inner.deliver(notification)
