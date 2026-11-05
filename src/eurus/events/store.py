from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from eurus.storage.database import Database, DatabasePath
from eurus.storage.leases import fenced_transition, new_claim_token

DELIVERY_STATUSES = frozenset({"pending", "delivering", "delivered", "dead"})
HANDLER_KINDS = frozenset({"submit_job", "trigger_schedules", "notify"})
MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_BACKOFF_BASE_SECONDS = 1.0
DELIVERY_BACKOFF_MAX_SECONDS = 300.0
STALE_DELIVERING_SECONDS = 300.0
DEFAULT_EVENT_OWNER = "local"
GLOBAL_EVENT_OWNER = ""
ADMIN_SUBSCRIPTION_OWNER = "*"
MAX_OWNER_LENGTH = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_owner(owner_id: str | None) -> str:
    value = (owner_id or "").strip()
    if len(value) > MAX_OWNER_LENGTH:
        raise ValueError("owner_id exceeds 200 characters")
    return value


def delivery_scope_sql(
    owner_id: str | None, *, deliveries_alias: str = ""
) -> tuple[str, list[Any]]:
    if owner_id is None:
        return "", []
    column = f"{deliveries_alias}.event_id" if deliveries_alias else "event_id"
    clause = (
        f" AND {column} IN (SELECT event_id FROM eurus_domain_events "
        "WHERE owner_id=? OR owner_id='')"
    )
    return clause, [owner_id]


@dataclass(slots=True)
class DomainEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    created_at: datetime
    subject_type: str = ""
    subject_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    causation_id: str = ""
    dedup_key: str | None = None
    owner_id: str = ""


@dataclass(slots=True)
class EventSubscription:
    subscription_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    event_types: tuple[str, ...] = ()
    subject_type: str = ""
    subject_id: str = ""
    handler_kind: str = "submit_job"
    handler_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    owner_id: str = ""


@dataclass(slots=True)
class EventDelivery:
    delivery_id: str
    event_id: str
    subscription_id: str
    status: str
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    delivered_at: datetime | None = None
    claim_token: str = ""


class DomainEventStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

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
    ) -> DomainEvent | None:
        if not event_type.strip():
            raise ValueError("event_type is required")
        owner = normalize_owner(owner_id)
        current = _now()
        event = DomainEvent(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            occurred_at=current,
            created_at=current,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=dict(payload or {}),
            correlation_id=correlation_id,
            causation_id=causation_id,
            dedup_key=dedup_key,
            owner_id=owner,
        )
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO eurus_domain_events(
                        event_id, event_type, subject_type, subject_id, payload_json,
                        correlation_id, causation_id, dedup_key, occurred_at, created_at,
                        owner_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.event_type,
                        event.subject_type,
                        event.subject_id,
                        json.dumps(event.payload, ensure_ascii=False),
                        event.correlation_id,
                        event.causation_id,
                        event.dedup_key,
                        _time(event.occurred_at),
                        _time(event.created_at),
                        event.owner_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO eurus_outbox(outbox_id, event_id, status, created_at)
                    VALUES (?, ?, 'pending', ?)""",
                    (uuid.uuid4().hex, event.event_id, _time(current)),
                )
        except sqlite3.IntegrityError:
            return None
        return event

    def get_event(self, event_id: str, *, owner_id: str | None = None) -> DomainEvent | None:
        scope, scope_values = _event_scope(owner_id)
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM eurus_domain_events WHERE event_id=?{scope}",
                (event_id, *scope_values),
            ).fetchone()
        return self._event(row) if row is not None else None

    def list_events(
        self,
        *,
        event_type: str | None = None,
        subject_type: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        values: list[Any] = []
        if event_type is not None:
            clauses.append("event_type=?")
            values.append(event_type)
        if subject_type is not None:
            clauses.append("subject_type=?")
            values.append(subject_type)
        if owner_id is not None:
            clauses.append("(owner_id=? OR owner_id='')")
            values.append(owner_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_domain_events{where} ORDER BY occurred_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def create_subscription(
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
        if not name.strip() or len(name) > 200:
            raise ValueError("subscription name must be 1-200 characters")
        if handler_kind not in HANDLER_KINDS:
            raise ValueError(f"unknown handler kind: {handler_kind}")
        owner = normalize_owner(owner_id)
        current = _now()
        subscription = EventSubscription(
            subscription_id=uuid.uuid4().hex,
            name=name,
            created_at=current,
            updated_at=current,
            event_types=tuple(event_types),
            subject_type=subject_type,
            subject_id=subject_id,
            handler_kind=handler_kind,
            handler_config=dict(handler_config or {}),
            owner_id=owner,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_event_subscriptions(
                    subscription_id, name, event_types_json, subject_type, subject_id,
                    handler_kind, handler_config_json, enabled, created_at, updated_at,
                    owner_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    subscription.subscription_id,
                    subscription.name,
                    json.dumps(list(subscription.event_types)),
                    subscription.subject_type,
                    subscription.subject_id,
                    subscription.handler_kind,
                    json.dumps(subscription.handler_config, ensure_ascii=False),
                    _time(current),
                    _time(current),
                    subscription.owner_id,
                ),
            )
        return subscription

    def get_subscription(
        self, subscription_id: str, *, owner_id: str | None = None
    ) -> EventSubscription | None:
        scope, scope_values = _subscription_scope(owner_id)
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM eurus_event_subscriptions WHERE subscription_id=?{scope}",
                (subscription_id, *scope_values),
            ).fetchone()
        return self._subscription(row) if row is not None else None

    def list_subscriptions(
        self,
        *,
        enabled_only: bool = False,
        owner_id: str | None = None,
        limit: int = 200,
    ) -> list[EventSubscription]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT * FROM eurus_event_subscriptions"
        clauses: list[str] = []
        values: list[Any] = []
        if enabled_only:
            clauses.append("enabled=1")
        if owner_id is not None:
            clauses.append("(owner_id=? OR owner_id='')")
            values.append(owner_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at LIMIT ?"
        with self.database.connection() as connection:
            rows = connection.execute(query, (*values, limit)).fetchall()
        return [self._subscription(row) for row in rows]

    def set_subscription_enabled(
        self, subscription_id: str, enabled: bool, *, owner_id: str | None = None
    ) -> bool:
        scope, scope_values = _subscription_scope(owner_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_event_subscriptions SET enabled=?, updated_at=? "
                f"WHERE subscription_id=?{scope}",
                (int(enabled), _time(_now()), subscription_id, *scope_values),
            )
            return cursor.rowcount == 1

    def remove_subscription(self, subscription_id: str, *, owner_id: str | None = None) -> bool:
        scope, scope_values = _subscription_scope(owner_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM eurus_event_subscriptions WHERE subscription_id=?{scope}",
                (subscription_id, *scope_values),
            )
            return cursor.rowcount == 1

    def subscriptions_matching(self, event: DomainEvent) -> list[EventSubscription]:
        matches: list[EventSubscription] = []
        for subscription in self.list_subscriptions(enabled_only=True, limit=500):
            if not subscription_accepts(subscription, event):
                continue
            if (
                subscription.event_types
                and "*" not in subscription.event_types
                and event.event_type not in subscription.event_types
            ):
                continue
            if subscription.subject_type and subscription.subject_type != event.subject_type:
                continue
            if subscription.subject_id and subscription.subject_id != event.subject_id:
                continue
            matches.append(subscription)
        return matches

    def fan_out(self, event: DomainEvent, *, now: datetime | None = None) -> int:
        created = 0
        current = _time(now or _now())
        for subscription in self.subscriptions_matching(event):
            try:
                with self.database.connection() as connection:
                    connection.execute(
                        """INSERT INTO eurus_event_deliveries(
                            delivery_id, event_id, subscription_id, status, attempts,
                            next_attempt_at, created_at, updated_at
                        ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)""",
                        (
                            uuid.uuid4().hex,
                            event.event_id,
                            subscription.subscription_id,
                            current,
                            current,
                            current,
                        ),
                    )
                created += 1
            except sqlite3.IntegrityError:
                continue
        return created

    def pending_outbox(self, *, owner_id: str | None = None, limit: int = 100) -> list[DomainEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        scope, scope_values = _event_scope(owner_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT e.* FROM eurus_domain_events AS e
                JOIN eurus_outbox AS o ON o.event_id=e.event_id
                WHERE o.status='pending'{scope} ORDER BY o.created_at LIMIT ?""",
                (*scope_values, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def mark_outbox_dispatched(self, event_id: str, *, owner_id: str | None = None) -> bool:
        scope, scope_values = _event_scope(owner_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE eurus_outbox SET status='dispatched', dispatched_at=?
                WHERE event_id=? AND status='pending'
                AND event_id IN (SELECT event_id FROM eurus_domain_events WHERE 1=1{scope})""",
                (_time(_now()), event_id, *scope_values),
            )
            return cursor.rowcount == 1

    def due_deliveries(
        self, *, now: datetime | None = None, owner_id: str | None = None, limit: int = 50
    ) -> list[EventDelivery]:
        current = _time(now or _now())
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        scope, scope_values = delivery_scope_sql(owner_id, deliveries_alias="d")
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT d.* FROM eurus_event_deliveries AS d
                JOIN eurus_domain_events AS e ON e.event_id=d.event_id
                WHERE d.status='pending' AND d.next_attempt_at<=?{scope}
                ORDER BY d.next_attempt_at LIMIT ?""",
                (current, *scope_values, limit),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def mark_delivering(
        self, delivery_id: str, *, owner_id: str | None = None
    ) -> EventDelivery | None:
        scope, scope_values = delivery_scope_sql(owner_id)
        token = new_claim_token()
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE eurus_event_deliveries SET status='delivering',
                attempts=attempts+1, updated_at=?, claim_token=? WHERE delivery_id=?
                AND status='pending' {scope}""",
                (_time(_now()), token, delivery_id, *scope_values),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                f"SELECT * FROM eurus_event_deliveries WHERE delivery_id=?{scope}",
                (delivery_id, *scope_values),
            ).fetchone()
            if row is None:
                return None
        return self._delivery(row)

    def ack_delivery(
        self,
        delivery_id: str,
        *,
        owner_id: str | None = None,
        claim_token: str | None = None,
    ) -> bool:
        scope, scope_values = delivery_scope_sql(owner_id)
        expect: dict[str, Any] = {"status": "delivering"}
        if claim_token is not None:
            expect["claim_token"] = claim_token
        with self.database.connection() as connection:
            return fenced_transition(
                connection,
                table="eurus_event_deliveries",
                key_column="delivery_id",
                key_value=delivery_id,
                assignments={
                    "status": "delivered",
                    "delivered_at": _time(_now()),
                    "updated_at": _time(_now()),
                    "last_error": None,
                },
                expect=expect,
                extra_where=scope,
                extra_params=scope_values,
            )

    def fail_delivery(
        self,
        delivery_id: str,
        *,
        error: str,
        max_attempts: int = MAX_DELIVERY_ATTEMPTS,
        backoff_base_seconds: float = DELIVERY_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DELIVERY_BACKOFF_MAX_SECONDS,
        now: datetime | None = None,
        owner_id: str | None = None,
        claim_token: str | None = None,
    ) -> str:
        current = now or _now()
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return "missing"
        if claim_token is not None and delivery.claim_token != claim_token:
            return "fenced"
        scope, scope_values = delivery_scope_sql(owner_id)
        if delivery.attempts >= max_attempts:
            with self.database.connection() as connection:
                cursor = connection.execute(
                    f"""UPDATE eurus_event_deliveries SET status='dead', last_error=?,
                    updated_at=? WHERE delivery_id=?{scope}""",
                    (error[:2000], _time(current), delivery_id, *scope_values),
                )
                if cursor.rowcount != 1:
                    return "missing"
            return "dead"
        delay = min(backoff_base_seconds * (2 ** (delivery.attempts - 1)), backoff_max_seconds)
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE eurus_event_deliveries SET status='pending', last_error=?,
                next_attempt_at=?, updated_at=?, claim_token='' WHERE delivery_id=?{scope}""",
                (
                    error[:2000],
                    _time(current + timedelta(seconds=delay)),
                    _time(current),
                    delivery_id,
                    *scope_values,
                ),
            )
            if cursor.rowcount != 1:
                return "missing"
        return "retry"

    def retry_dead_delivery(self, delivery_id: str, *, owner_id: str | None = None) -> bool:
        scope, scope_values = delivery_scope_sql(owner_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE eurus_event_deliveries SET status='pending', attempts=0,
                next_attempt_at=?, updated_at=? WHERE delivery_id=? AND status='dead'
                {scope}""",
                (_time(_now()), _time(_now()), delivery_id, *scope_values),
            )
            return cursor.rowcount == 1

    def recover_stale_delivering(
        self,
        *,
        older_than_seconds: float = STALE_DELIVERING_SECONDS,
        exclude: Container[str] = frozenset(),
        now: datetime | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> int:
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds must be positive")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        current = now or _now()
        threshold = _time(current - timedelta(seconds=older_than_seconds))
        scope, scope_values = delivery_scope_sql(owner_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT delivery_id FROM eurus_event_deliveries
                WHERE status='delivering' AND updated_at<?{scope} LIMIT ?""",
                (threshold, *scope_values, limit),
            ).fetchall()
            recovered = 0
            for row in rows:
                delivery_id = str(row["delivery_id"])
                if delivery_id in exclude:
                    continue
                cursor = connection.execute(
                    """UPDATE eurus_event_deliveries SET status='pending',
                    next_attempt_at=?, updated_at=?, claim_token='' WHERE delivery_id=?
                    AND status='delivering'""",
                    (_time(current), _time(current), delivery_id),
                )
                if cursor.rowcount == 1:
                    recovered += 1
            return recovered

    def dead_deliveries(
        self, *, owner_id: str | None = None, limit: int = 100
    ) -> list[EventDelivery]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        scope, scope_values = delivery_scope_sql(owner_id, deliveries_alias="d")
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT d.* FROM eurus_event_deliveries AS d
                JOIN eurus_domain_events AS e ON e.event_id=d.event_id
                WHERE d.status='dead'{scope}
                ORDER BY d.updated_at DESC LIMIT ?""",
                (*scope_values, limit),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def get_delivery(self, delivery_id: str) -> EventDelivery | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_event_deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        return self._delivery(row) if row is not None else None

    def deliveries_for_event(
        self, event_id: str, *, owner_id: str | None = None
    ) -> list[EventDelivery]:
        scope, scope_values = delivery_scope_sql(owner_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT * FROM eurus_event_deliveries WHERE event_id=?{scope}
                ORDER BY created_at""",
                (event_id, *scope_values),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def purge(
        self,
        *,
        older_than_days: int = 30,
        now: datetime | None = None,
    ) -> dict[str, int]:
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")
        current = now or _now()
        cutoff = _time(current - timedelta(days=older_than_days))
        purged: dict[str, int] = {"events": 0, "outbox": 0}
        with self.database.connection() as connection:
            stale_outbox = connection.execute(
                "SELECT COUNT(*) FROM eurus_outbox WHERE created_at<? AND status='dispatched'",
                (cutoff,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM eurus_outbox WHERE created_at<? AND status='dispatched'",
                (cutoff,),
            )
            purged["outbox"] = int(stale_outbox)
            stale_events = connection.execute(
                """SELECT COUNT(*) FROM eurus_domain_events AS e WHERE e.occurred_at<?
                AND NOT EXISTS (
                    SELECT 1 FROM eurus_event_deliveries AS d
                    WHERE d.event_id=e.event_id AND d.status IN ('pending','delivering')
                )""",
                (cutoff,),
            ).fetchone()[0]
            connection.execute(
                """DELETE FROM eurus_domain_events WHERE occurred_at<? AND event_id NOT IN (
                    SELECT event_id FROM eurus_event_deliveries
                    WHERE status IN ('pending','delivering')
                )""",
                (cutoff,),
            )
            purged["events"] = int(stale_events)
        return purged

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]:
        scope, scope_values = _event_scope(owner_id)
        delivery_scope, delivery_values = delivery_scope_sql(owner_id, deliveries_alias="d")
        with self.database.connection() as connection:
            events = connection.execute(
                f"SELECT COUNT(*) FROM eurus_domain_events WHERE 1=1{scope}",
                (*scope_values,),
            ).fetchone()[0]
            pending_outbox = connection.execute(
                f"""SELECT COUNT(*) FROM eurus_outbox AS o
                JOIN eurus_domain_events AS e ON e.event_id=o.event_id
                WHERE o.status='pending'{scope}""",
                (*scope_values,),
            ).fetchone()[0]
            pending_deliveries = connection.execute(
                f"""SELECT COUNT(*) FROM eurus_event_deliveries AS d
                JOIN eurus_domain_events AS e ON e.event_id=d.event_id
                WHERE d.status='pending'{delivery_scope}""",
                (*delivery_values,),
            ).fetchone()[0]
            dead = connection.execute(
                f"""SELECT COUNT(*) FROM eurus_event_deliveries AS d
                JOIN eurus_domain_events AS e ON e.event_id=d.event_id
                WHERE d.status='dead'{delivery_scope}""",
                (*delivery_values,),
            ).fetchone()[0]
            subscriptions = connection.execute(
                "SELECT COUNT(*) FROM eurus_event_subscriptions WHERE enabled=1"
            ).fetchone()[0]
        return {
            "events": int(events),
            "pending_outbox": int(pending_outbox),
            "pending_deliveries": int(pending_deliveries),
            "dead_deliveries": int(dead),
            "active_subscriptions": int(subscriptions),
        }

    def _event(self, row: sqlite3.Row) -> DomainEvent:
        return DomainEvent(
            event_id=row["event_id"],
            event_type=row["event_type"],
            subject_type=row["subject_type"] or "",
            subject_id=row["subject_id"] or "",
            payload=json.loads(row["payload_json"] or "{}"),
            correlation_id=row["correlation_id"] or "",
            causation_id=row["causation_id"] or "",
            dedup_key=row["dedup_key"],
            occurred_at=_parse(row["occurred_at"]) or datetime.now(UTC),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            owner_id=row["owner_id"] or "",
        )

    def _subscription(self, row: sqlite3.Row) -> EventSubscription:
        return EventSubscription(
            subscription_id=row["subscription_id"],
            name=row["name"],
            event_types=tuple(json.loads(row["event_types_json"] or "[]")),
            subject_type=row["subject_type"] or "",
            subject_id=row["subject_id"] or "",
            handler_kind=row["handler_kind"],
            handler_config=json.loads(row["handler_config_json"] or "{}"),
            enabled=bool(row["enabled"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            owner_id=row["owner_id"] or "",
        )

    def _delivery(self, row: sqlite3.Row) -> EventDelivery:
        return EventDelivery(
            delivery_id=row["delivery_id"],
            event_id=row["event_id"],
            subscription_id=row["subscription_id"],
            status=row["status"],
            attempts=int(row["attempts"]),
            next_attempt_at=_parse(row["next_attempt_at"]) or datetime.now(UTC),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            last_error=row["last_error"],
            delivered_at=_parse(row["delivered_at"]),
            claim_token=str(row["claim_token"] or ""),
        )


def subscription_accepts(subscription: EventSubscription, event: DomainEvent) -> bool:
    if subscription.owner_id in (ADMIN_SUBSCRIPTION_OWNER, GLOBAL_EVENT_OWNER):
        return True
    return event.owner_id in (subscription.owner_id, GLOBAL_EVENT_OWNER)


def _event_scope(owner_id: str | None) -> tuple[str, list[Any]]:
    if owner_id is None:
        return "", []
    return " AND (owner_id=? OR owner_id='')", [owner_id]


def _subscription_scope(owner_id: str | None) -> tuple[str, list[Any]]:
    if owner_id is None:
        return "", []
    return " AND (owner_id=? OR owner_id='')", [owner_id]
