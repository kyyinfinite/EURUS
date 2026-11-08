from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from eurus.automation.engine import AutomationEngine
from eurus.automation.notifications import Notification, NotificationSink
from eurus.events.store import (
    ADMIN_SUBSCRIPTION_OWNER,
    DEFAULT_EVENT_OWNER,
    STALE_DELIVERING_SECONDS,
    DomainEvent,
    DomainEventStore,
    EventSubscription,
)
from eurus.workflows.definition import resolve_template

log = logging.getLogger(__name__)

DELIVERY_INFLIGHT = "__delivery_inflight__"


class ScheduleTriggerPort(Protocol):
    def trigger_event_schedules(
        self,
        event: DomainEvent,
        *,
        owner_id: str | None = None,
    ) -> list[str]: ...


@dataclass(slots=True)
class DispatchReport:
    events_fanned_out: int = 0
    deliveries_created: int = 0
    deliveries_processed: int = 0
    deliveries_delivered: int = 0
    deliveries_retried: int = 0
    deliveries_dead: int = 0
    jobs_submitted: int = 0
    jobs_suppressed: int = 0
    schedules_triggered: int = 0


class EventBus:
    def __init__(
        self,
        database: Any,
        *,
        automation: AutomationEngine | None = None,
        schedule_triggers: ScheduleTriggerPort | None = None,
        notifications: NotificationSink | None = None,
        max_delivery_attempts: int = 5,
        delivery_batch: int = 50,
    ) -> None:
        if not 1 <= max_delivery_attempts <= 20:
            raise ValueError("max_delivery_attempts must be between 1 and 20")
        if not 1 <= delivery_batch <= 500:
            raise ValueError("delivery_batch must be between 1 and 500")
        self.store = (
            database if isinstance(database, DomainEventStore) else DomainEventStore(database)
        )
        self.automation = automation
        self.schedule_triggers = schedule_triggers
        self.notifications = notifications
        self.max_delivery_attempts = max_delivery_attempts
        self.delivery_batch = delivery_batch
        self._async_deliveries: dict[str, tuple[asyncio.Task[Any], float, str]] = {}

    def set_automation(self, automation: AutomationEngine) -> None:
        if self.automation is not None and self.automation is not automation:
            raise RuntimeError("event bus is already bound to another automation engine")
        self.automation = automation

    def publish(
        self,
        event_type: str,
        *,
        subject_type: str = "",
        subject_id: str = "",
        payload: Mapping[str, Any] | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        dedup_key: str | None = None,
        owner_id: str | None = None,
        schema_version: int | None = None,
    ) -> DomainEvent | None:
        return self.store.publish(
            event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            dedup_key=dedup_key,
            owner_id=owner_id,
            schema_version=schema_version,
        )

    def subscribe(
        self,
        name: str,
        *,
        event_types: Sequence[str] = (),
        subject_type: str = "",
        subject_id: str = "",
        handler_kind: str = "submit_job",
        handler_config: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> EventSubscription:
        config = dict(handler_config or {})
        effective_owner = owner_id
        if effective_owner is None:
            configured = config.get("owner_id")
            effective_owner = (
                str(configured) if isinstance(configured, str) and configured.strip() else ""
            )
        return self.store.create_subscription(
            name,
            event_types=event_types,
            subject_type=subject_type,
            subject_id=subject_id,
            handler_kind=handler_kind,
            handler_config=config,
            owner_id=effective_owner,
        )

    def unsubscribe(self, subscription_id: str, *, owner_id: str | None = None) -> bool:
        return self.store.remove_subscription(subscription_id, owner_id=owner_id)

    def set_subscription_enabled(
        self, subscription_id: str, enabled: bool, *, owner_id: str | None = None
    ) -> bool:
        return self.store.set_subscription_enabled(subscription_id, enabled, owner_id=owner_id)

    def list_subscriptions(
        self, *, enabled_only: bool = False, owner_id: str | None = None
    ) -> list[EventSubscription]:
        return self.store.list_subscriptions(enabled_only=enabled_only, owner_id=owner_id)

    def dispatch_tick(
        self, *, now: datetime | None = None, owner_id: str | None = None
    ) -> DispatchReport:
        report = DispatchReport()
        self._settle_async_deliveries(report)
        self.store.recover_stale_delivering(
            exclude=self._active_async_delivery_ids(), now=now, owner_id=owner_id
        )
        self._drain_outbox(report, now=now, owner_id=owner_id)
        self._process_deliveries(report, now=now, owner_id=owner_id)
        return report

    def _active_async_delivery_ids(self) -> frozenset[str]:
        current = time.monotonic()
        active = {
            delivery_id
            for delivery_id, (_, created_at, _) in self._async_deliveries.items()
            if current - created_at <= STALE_DELIVERING_SECONDS
        }
        for delivery_id, (task, _, _) in list(self._async_deliveries.items()):
            if delivery_id not in active or task.done():
                self._async_deliveries.pop(delivery_id, None)
        return frozenset(active)

    def _settle_async_deliveries(self, report: DispatchReport) -> None:
        for delivery_id, (task, _, token) in list(self._async_deliveries.items()):
            if not task.done():
                continue
            self._async_deliveries.pop(delivery_id, None)
            if task.cancelled():
                self._fail_async_delivery(delivery_id, "delivery cancelled", report, token)
                continue
            exc = task.exception()
            if exc is not None:
                self._fail_async_delivery(
                    delivery_id, f"notification delivery failed: {exc}", report, token
                )
                continue
            outcome = task.result()
            if outcome is False:
                self._fail_async_delivery(
                    delivery_id, "notification sink rejected the delivery", report, token
                )
                continue
            if self.store.ack_delivery(delivery_id, claim_token=token):
                report.deliveries_delivered += 1

    def _fail_async_delivery(
        self, delivery_id: str, error: str, report: DispatchReport, token: str = ""
    ) -> None:
        delivery = self.store.get_delivery(delivery_id)
        if delivery is None or delivery.status != "delivering":
            return
        result = self.store.fail_delivery(
            delivery_id,
            error=error,
            max_attempts=self.max_delivery_attempts,
            claim_token=token or None,
        )
        if result == "dead":
            report.deliveries_dead += 1
        else:
            report.deliveries_retried += 1

    def _drain_outbox(
        self, report: DispatchReport, *, now: datetime | None, owner_id: str | None
    ) -> None:
        for event in self.store.pending_outbox(owner_id=owner_id, limit=self.delivery_batch):
            report.events_fanned_out += 1
            report.deliveries_created += self.store.fan_out(event, now=now)
            self.store.mark_outbox_dispatched(event.event_id, owner_id=owner_id)

    def _process_deliveries(
        self, report: DispatchReport, *, now: datetime | None, owner_id: str | None
    ) -> None:
        due = self.store.due_deliveries(now=now, owner_id=owner_id, limit=self.delivery_batch)
        for delivery in due:
            claimed = self.store.mark_delivering(delivery.delivery_id, owner_id=owner_id)
            if claimed is None:
                continue
            report.deliveries_processed += 1
            try:
                outcome = self._deliver(claimed, report, owner_id)
            except Exception as exc:
                log.exception(
                    "delivery %s for event %s raised unexpectedly",
                    delivery.delivery_id,
                    delivery.event_id,
                )
                outcome = f"delivery crashed: {type(exc).__name__}: {exc}"
            if outcome == DELIVERY_INFLIGHT:
                continue
            if outcome == "delivered":
                self.store.ack_delivery(
                    delivery.delivery_id, owner_id=owner_id, claim_token=claimed.claim_token
                )
                report.deliveries_delivered += 1
            elif outcome == "dead":
                result = self.store.fail_delivery(
                    delivery.delivery_id,
                    error="delivery exhausted attempts",
                    max_attempts=1,
                    now=now,
                    owner_id=owner_id,
                    claim_token=claimed.claim_token,
                )
                report.deliveries_dead += 1
                _ = result
            else:
                log.warning(
                    "delivery %s (event %s, subscription %s) not delivered: %s",
                    delivery.delivery_id,
                    delivery.event_id,
                    delivery.subscription_id,
                    outcome,
                )
                result = self.store.fail_delivery(
                    delivery.delivery_id,
                    error=outcome,
                    max_attempts=self.max_delivery_attempts,
                    now=now,
                    owner_id=owner_id,
                    claim_token=claimed.claim_token,
                )
                if result == "dead":
                    report.deliveries_dead += 1
                    log.error(
                        "delivery %s (event %s, subscription %s) reached dead letter: %s",
                        delivery.delivery_id,
                        delivery.event_id,
                        delivery.subscription_id,
                        outcome,
                    )
                else:
                    report.deliveries_retried += 1

    def _deliver(self, delivery: Any, report: DispatchReport, owner_id: str | None) -> str:
        event = self.store.get_event(delivery.event_id, owner_id=owner_id)
        if event is None:
            return "event record missing"
        subscription = self.store.get_subscription(delivery.subscription_id)
        if subscription is None or not subscription.enabled:
            return "delivered"
        if subscription.handler_kind == "submit_job":
            return self._submit_job(event, subscription, report)
        if subscription.handler_kind == "trigger_schedules":
            return self._trigger_schedules(event, subscription, report)
        if subscription.handler_kind == "notify":
            return self._notify(
                event,
                subscription,
                delivery.delivery_id,
                claim_token=str(getattr(delivery, "claim_token", "") or ""),
            )
        return f"unknown handler kind: {subscription.handler_kind}"

    def _submit_job(
        self, event: DomainEvent, subscription: EventSubscription, report: DispatchReport
    ) -> str:
        if self.automation is None:
            return "automation engine unavailable"
        config = subscription.handler_config
        if event.event_type.startswith("TASK_") and not bool(
            config.get("allow_task_events", False)
        ):
            log.debug(
                "subscription %s does not accept automation lifecycle events",
                subscription.subscription_id,
            )
            report.jobs_suppressed += 1
            return "delivered"
        context = {
            "event_type": event.event_type,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "payload": event.payload,
        }
        objective_template = str(config.get("objective", "Handle event {{event_type}}"))
        try:
            objective = str(resolve_template(objective_template, context))
        except Exception as exc:
            return f"objective template failed: {exc}"
        owner_id = event.owner_id or str(config.get("owner_id") or DEFAULT_EVENT_OWNER)
        extra_payload = config.get("payload", {})
        merged_payload: dict[str, Any] = {
            "kind": "event",
            "event_id": event.event_id,
            "event_type": event.event_type,
            "subscription_id": subscription.subscription_id,
            "event_owner": event.owner_id,
            "mode": "event",
        }
        if isinstance(extra_payload, Mapping):
            merged_payload.update(resolve_template(dict(extra_payload), context))
        job_id = f"evt-{event.event_id[:20]}-{subscription.subscription_id[:16]}"
        existing = self.automation.jobs.get(job_id)
        if existing is not None and existing.owner_id == owner_id:
            return "delivered"
        try:
            self.automation.submit(
                owner_id,
                objective,
                payload=merged_payload,
                source="event",
                priority=int(config.get("priority", 5)),
                max_attempts=int(config.get("max_attempts", 2)),
                correlation_id=event.correlation_id or event.event_id,
                job_id=job_id,
            )
        except sqlite3.IntegrityError:
            return "delivered"
        except Exception as exc:
            log.exception(
                "job submission failed for event %s subscription %s",
                event.event_id,
                subscription.subscription_id,
            )
            return f"job submission failed: {exc}"
        report.jobs_submitted += 1
        return "delivered"

    def _trigger_schedules(
        self, event: DomainEvent, subscription: EventSubscription, report: DispatchReport
    ) -> str:
        if self.schedule_triggers is None:
            return "schedule trigger port unavailable"
        config = subscription.handler_config
        configured = config.get("owner_id")
        owner_id = event.owner_id or (
            str(configured) if isinstance(configured, str) and configured.strip() else None
        )
        try:
            triggered = self.schedule_triggers.trigger_event_schedules(
                event,
                owner_id=owner_id if owner_id else None,
            )
        except Exception as exc:
            return f"schedule trigger failed: {exc}"
        report.schedules_triggered += len(triggered)
        return "delivered"

    def _notify(
        self,
        event: DomainEvent,
        subscription: EventSubscription,
        delivery_id: str,
        claim_token: str = "",
    ) -> str:
        if self.notifications is None:
            return "notification sink unavailable"
        config = subscription.handler_config
        message = str(config.get("message", "event {{event_type}} on {{subject_id}}"))
        context = {
            "event_type": event.event_type,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "payload": event.payload,
        }
        try:
            body = str(resolve_template(message, context))
        except Exception as exc:
            return f"notification template failed: {exc}"
        configured = config.get("owner_id")
        owner_id = event.owner_id or (
            str(configured) if isinstance(configured, str) and configured.strip() else ""
        )
        notification = Notification(
            kind="workflow_notification",
            title=f"event:{event.event_type}",
            body=body[:2000],
            owner_id=owner_id,
            correlation_id=event.correlation_id or event.event_id,
        )
        try:
            delivered = self.notifications.deliver(notification)
        except Exception as exc:
            log.exception(
                "notification delivery failed for event %s subscription %s",
                event.event_id,
                subscription.subscription_id,
            )
            return f"notification delivery failed: {exc}"
        if asyncio.iscoroutine(delivered):
            return self._schedule_async_notification(
                delivered, event, subscription, delivery_id, claim_token=claim_token
            )
        if delivered is False:
            return "notification sink rejected the delivery"
        return "delivered"

    def _schedule_async_notification(
        self,
        delivered: Any,
        event: DomainEvent,
        subscription: EventSubscription,
        delivery_id: str,
        claim_token: str = "",
    ) -> str:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            delivered.close()
            log.error(
                "no running event loop for the asynchronous notification sink; "
                "delivery for event %s subscription %s is not acknowledged",
                event.event_id,
                subscription.subscription_id,
            )
            return "no running event loop for asynchronous notification sink"
        task = loop.create_task(delivered)
        task.add_done_callback(self._observe_notification_task)
        self._async_deliveries[delivery_id] = (task, time.monotonic(), claim_token)
        return DELIVERY_INFLIGHT

    @staticmethod
    def _observe_notification_task(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("asynchronous notification delivery failed: %s", exc)


__all__ = [
    "ADMIN_SUBSCRIPTION_OWNER",
    "DEFAULT_EVENT_OWNER",
    "DispatchReport",
    "EventBus",
    "ScheduleTriggerPort",
]
