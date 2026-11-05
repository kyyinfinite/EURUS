from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.scheduler import SILENT_PREFIX
from eurus.storage.leases import new_claim_token
from eurus.storage.sqlite import connect

log = logging.getLogger(__name__)

DELIVERY_STATUSES = frozenset({"queued", "sending", "delivered", "failed", "suppressed"})
CHANNELS = frozenset({"local", "web", "telegram", "tui"})
DEFAULT_MAX_ATTEMPTS = 3
STALE_SENDING_SECONDS = 120.0

DeliverySender = Callable[[str, str, str], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Delivery:
    delivery_id: str
    job_id: str
    execution_key: str
    channel: str
    target: str
    subject: str
    body: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    delivered_at: str | None
    claim_token: str = ""


def _delivery(row: sqlite3.Row) -> Delivery:
    keys = set(row.keys())
    return Delivery(
        delivery_id=str(row["delivery_id"]),
        job_id=str(row["job_id"]),
        execution_key=str(row["execution_key"]),
        channel=str(row["channel"]),
        target=str(row["target"]),
        subject=str(row["subject"]),
        body=str(row["body"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        last_error=row["last_error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        delivered_at=row["delivered_at"],
        claim_token=str(row["claim_token"]) if "claim_token" in keys else "",
    )


class DeliveryStore:
    def __init__(self, path: Path, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        self.max_attempts = max_attempts
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS eurus_deliveries (
                    delivery_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    execution_key TEXT NOT NULL, channel TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                    last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS eurus_deliveries_status
                    ON eurus_deliveries(status, channel, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS eurus_deliveries_idempotent
                    ON eurus_deliveries(execution_key, channel);
                """
            )
            columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(eurus_deliveries)")
            }
            if "claim_token" not in columns:
                db.execute(
                    "ALTER TABLE eurus_deliveries ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path, rows=True, foreign_keys=False, autocommit=True)

    def enqueue(
        self,
        *,
        job_id: str,
        attempt: int,
        channel: str,
        target: str = "",
        subject: str = "",
        body: str = "",
        status: str = "queued",
        now: datetime | None = None,
    ) -> Delivery:
        if channel not in CHANNELS:
            raise ValueError(f"unknown delivery channel: {channel}")
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"unknown delivery status: {status}")
        current = _iso(now or _utcnow())
        execution_key = f"{job_id}:{int(attempt)}"
        delivery_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO eurus_deliveries ("
                "delivery_id, job_id, execution_key, channel, target, subject, body, "
                "status, attempts, max_attempts, last_error, created_at, updated_at, "
                "delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)",
                (
                    delivery_id,
                    job_id,
                    execution_key,
                    channel,
                    target[:300],
                    subject[:500],
                    body[:8000],
                    status,
                    self.max_attempts,
                    current,
                    current,
                    current if status == "delivered" else None,
                ),
            )
            row = db.execute(
                "SELECT * FROM eurus_deliveries WHERE execution_key=? AND channel=?",
                (execution_key, channel),
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery row could not be read back")
        return _delivery(row)

    def claim_due(self, channel: str, *, limit: int = 10) -> list[Delivery]:
        if channel not in CHANNELS:
            raise ValueError(f"unknown delivery channel: {channel}")
        current = _iso(_utcnow())
        claimed: list[Delivery] = []
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM eurus_deliveries WHERE channel=? AND status='queued' "
                "ORDER BY created_at LIMIT ?",
                (channel, max(1, min(limit, 100))),
            ).fetchall()
            for row in rows:
                token = new_claim_token()
                cursor = db.execute(
                    "UPDATE eurus_deliveries SET status='sending', attempts=attempts+1, "
                    "updated_at=?, claim_token=? WHERE delivery_id=? AND status='queued'",
                    (current, token, row["delivery_id"]),
                )
                if cursor.rowcount == 1:
                    refreshed = db.execute(
                        "SELECT * FROM eurus_deliveries WHERE delivery_id=?",
                        (row["delivery_id"],),
                    ).fetchone()
                    if refreshed is not None:
                        claimed.append(_delivery(refreshed))
            db.commit()
        return claimed

    def recover_sending(self) -> int:
        current = _iso(_utcnow())
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE eurus_deliveries SET status='queued', claim_token='', updated_at=? "
                "WHERE status='sending'",
                (current,),
            )
            return cursor.rowcount

    def recover_stale_sending(self, *, older_than_seconds: float = STALE_SENDING_SECONDS) -> int:
        threshold = _iso(_utcnow() - timedelta(seconds=older_than_seconds))
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE eurus_deliveries SET status='queued', claim_token='', updated_at=? "
                "WHERE status='sending' AND updated_at<?",
                (threshold, threshold),
            )
            return cursor.rowcount

    def mark(
        self,
        delivery_id: str,
        *,
        status: str,
        error: str | None = None,
        now: datetime | None = None,
        claim_token: str | None = None,
    ) -> bool:
        if status not in ("delivered", "failed", "suppressed", "queued"):
            raise ValueError(f"invalid delivery status: {status}")
        current = now or _utcnow()
        fence = ""
        params: list[Any] = []
        if claim_token is not None:
            fence = " AND claim_token=?"
            params.append(claim_token)
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE eurus_deliveries SET status=?, last_error=?, updated_at=?, "
                "delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END, "
                "claim_token=CASE WHEN ? IN ('delivered','failed','suppressed') THEN '' "
                "ELSE claim_token END "
                "WHERE delivery_id=? AND status IN ('queued','sending')" + fence,
                (
                    status,
                    error[:300] if error else None,
                    _iso(current),
                    status,
                    _iso(current),
                    status,
                    delivery_id,
                    *params,
                ),
            )
            return cursor.rowcount == 1

    def pending_for_execution(self, execution_key: str) -> list[Delivery]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM eurus_deliveries WHERE execution_key=?",
                (execution_key,),
            ).fetchall()
            return [_delivery(row) for row in rows]

    def list_deliveries(
        self, *, statuses: Sequence[str] = (), channel: str | None = None, limit: int = 100
    ) -> list[Delivery]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        values: list[Any] = []
        if statuses:
            invalid = set(statuses) - DELIVERY_STATUSES
            if invalid:
                raise ValueError("invalid delivery status")
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
        if channel is not None:
            clauses.append("channel=?")
            values.append(channel)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT * FROM eurus_deliveries{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
            return [_delivery(row) for row in rows]

    def finalize_execution(self, execution_key: str) -> str | None:
        deliveries = self.pending_for_execution(execution_key)
        if not deliveries:
            return None
        if all(item.status == "delivered" for item in deliveries):
            final = "ok"
        elif all(item.status in ("delivered", "failed") for item in deliveries):
            final = "delivery_failed"
        elif all(item.status == "suppressed" for item in deliveries):
            final = "suppressed"
        else:
            final = "delivery_queued"
        job_id = deliveries[0].job_id
        with closing(self._connect()) as db, suppress(sqlite3.OperationalError):
            db.execute(
                "UPDATE eurus_jobs SET delivery_status=?, updated_at=? WHERE job_id=?",
                (final, _iso(_utcnow()), job_id),
            )
        return final


def resolve_channels(
    deliver: str, origin_gateway: str, origin_conversation: str
) -> list[tuple[str, str]]:
    text = str(deliver or "").strip()
    if not text or text == "local":
        return [("local", "")]
    if text == "origin":
        gateway = origin_gateway.strip()
        conversation = origin_conversation.strip()
        if gateway == "telegram" and conversation:
            return [("telegram", conversation)]
        if gateway in ("web", "tui"):
            return [(gateway, conversation)]
        return [("local", "")]
    if text == "web":
        return [("web", origin_conversation.strip())]
    if text == "tui":
        return [("tui", "")]
    if text.startswith("telegram:"):
        return [("telegram", text.split(":", 1)[1])]
    return [("local", "")]


class RoutingNotificationSink:
    def __init__(self, store: DeliveryStore) -> None:
        self.store = store

    async def deliver(self, notification: Any) -> None:
        with suppress(Exception):
            notification.validate()
        job_id = str(getattr(notification, "job_id", "") or "")
        deliver = str(getattr(notification, "deliver", "") or "")
        if not job_id or not deliver:
            log.info(
                "notification kind=%s has no delivery target; logged only",
                getattr(notification, "kind", ""),
            )
            return
        attempt = int(getattr(notification, "attempt", 0) or 0)
        body = str(getattr(notification, "result", "") or "") or str(
            getattr(notification, "body", "")
        )
        silent = bool(getattr(notification, "silent", False)) or body.startswith(SILENT_PREFIX)
        channels = resolve_channels(
            deliver,
            str(getattr(notification, "origin_gateway", "") or ""),
            str(getattr(notification, "origin_conversation", "") or ""),
        )
        subject = str(getattr(notification, "title", "") or "")
        for channel, target in channels:
            self.store.enqueue(
                job_id=job_id,
                attempt=attempt,
                channel=channel,
                target=target,
                subject=subject,
                body=body,
                status="suppressed" if silent else "queued",
            )
        self.store.finalize_execution(f"{job_id}:{attempt}")


class LocalFileSink:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else paths.home_dir() / "inbox"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(self.root, 0o700)

    async def __call__(self, target: str, subject: str, body: str) -> None:
        stamp = _utcnow().strftime("%Y%m%dT%H%M%S")
        safe_subject = "".join(char if char.isalnum() else "-" for char in subject)[:60]
        path = self.root / f"{stamp}-{safe_subject or 'delivery'}-{uuid.uuid4().hex[:8]}.txt"
        path.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
        with suppress(OSError):
            os.chmod(path, 0o600)


class TuiInboxSink:
    async def __call__(self, target: str, subject: str, body: str) -> None:
        return None


class DeliveryDispatcher:
    def __init__(
        self,
        store: DeliveryStore,
        channel: str,
        sender: Any,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        if channel not in CHANNELS:
            raise ValueError(f"unknown delivery channel: {channel}")
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        self.store = store
        self.channel = channel
        self.sender = sender
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self.delivered = 0
        self.failed = 0

    async def run(self) -> None:
        self.store.recover_stale_sending()
        while not self._stop.is_set():
            claimed = self.store.claim_due(self.channel, limit=10)
            if not claimed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                continue
            for delivery in claimed:
                await self._deliver(delivery)

    async def _deliver(self, delivery: Delivery) -> None:
        token = delivery.claim_token
        try:
            await self.sender(delivery.target, delivery.subject, delivery.body)
        except asyncio.CancelledError:
            self.store.mark(
                delivery.delivery_id,
                status="queued",
                error="dispatcher_cancelled",
                claim_token=token,
            )
            raise
        except Exception as exc:
            log.warning(
                "delivery %s via %s failed: %s",
                delivery.delivery_id,
                self.channel,
                type(exc).__name__,
            )
            if delivery.attempts >= delivery.max_attempts:
                self.store.mark(
                    delivery.delivery_id,
                    status="failed",
                    error=type(exc).__name__,
                    claim_token=token,
                )
                self.failed += 1
            else:
                self.store.mark(
                    delivery.delivery_id,
                    status="queued",
                    error=type(exc).__name__,
                    claim_token=token,
                )
            self.store.finalize_execution(delivery.execution_key)
            return
        self.store.mark(delivery.delivery_id, status="delivered", claim_token=token)
        self.delivered += 1
        self.store.finalize_execution(delivery.execution_key)

    async def stop(self) -> None:
        self._stop.set()


__all__ = [
    "CHANNELS",
    "DELIVERY_STATUSES",
    "Delivery",
    "DeliveryDispatcher",
    "DeliveryStore",
    "LocalFileSink",
    "RoutingNotificationSink",
    "TuiInboxSink",
    "resolve_channels",
]
