from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from eurus.automation.worker import AutomationWorkerPool
from eurus.events.bus import EventBus
from eurus.events.store import DomainEventStore
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "jobs.sqlite3")


async def test_outer_cancellation_cancels_handler_and_finishes_job(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = JobStore(database)
    store.enqueue("owner", "objective")
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    finished: list[tuple[str, str, str | None]] = []

    async def handler(job: object) -> str:
        handler_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        return "done"

    def on_finish(job: object, status: str, result: str, error: str | None) -> None:
        finished.append((status, result, error))

    pool = AutomationWorkerPool(store, handler, on_finish=on_finish)

    async def drive() -> None:
        worker_task = asyncio.create_task(pool.run_once("w1"))
        await handler_started.wait()
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    await asyncio.wait_for(drive(), timeout=20)
    assert handler_cancelled.is_set()
    assert finished == [("cancelled", "", "worker_cancelled")]
    job = store.list_jobs(owner_id="owner")[0]
    assert job.status == "cancelled"
    assert job.result == ""
    assert job.error == "worker_cancelled"


async def test_outer_cancellation_abandons_unresponsive_handler_but_finishes(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = JobStore(database)
    store.enqueue("owner", "objective")
    handler_started = asyncio.Event()

    async def handler(job: object) -> str:
        handler_started.set()
        while True:
            await asyncio.sleep(0.05)

    pool = AutomationWorkerPool(store, handler)

    async def drive() -> None:
        worker_task = asyncio.create_task(pool.run_once("w2"))
        await handler_started.wait()
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    await asyncio.wait_for(drive(), timeout=60)
    job = store.list_jobs(owner_id="owner")[0]
    assert job.status == "cancelled"


async def test_handler_task_failure_unchanged(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = JobStore(database)
    store.enqueue("owner", "objective")

    async def handler(job: object) -> str:
        raise RuntimeError("boom")

    pool = AutomationWorkerPool(store, handler)
    assert await pool.run_once("w3") is True
    job = store.list_jobs(owner_id="owner")[0]
    assert job.status == "failed"
    assert job.error == "RuntimeError"
    assert pool.jobs_failed == 1


def _seed_event_with_notification(store: DomainEventStore) -> str:
    store.create_subscription(
        "test.sub",
        subject_type="task",
        subject_id="",
        event_types=["TASK_COMPLETED"],
        handler_kind="notify",
        handler_config={"message": "event {{event_type}}"},
        owner_id="",
    )
    event = store.publish(
        event_type="TASK_COMPLETED",
        subject_type="task",
        subject_id="task-1",
        payload={"outcome": "ok"},
        owner_id="",
    )
    assert event is not None
    assert store.pending_outbox(owner_id=None)
    store.fan_out(event)
    store.mark_outbox_dispatched(event.event_id)
    return event.event_id


class AsyncSink:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls = 0

    async def deliver(self, notification: object) -> bool:
        self.calls += 1
        if self.outcome == "fail":
            raise RuntimeError("sink down")
        if self.outcome == "reject":
            return False
        await asyncio.sleep(0.05)
        return True


async def test_async_notification_acked_only_after_completion(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = DomainEventStore(database)
    event_id = _seed_event_with_notification(store)
    sink = AsyncSink("ok")
    bus = EventBus(database, notifications=sink)
    report = bus.dispatch_tick()
    assert report.deliveries_processed == 1
    assert store.due_deliveries(owner_id=None) == []
    pending = [row for row in store.deliveries_for_event(event_id) if row.status == "delivering"]
    assert len(pending) == 1
    await asyncio.sleep(0.15)
    report = bus.dispatch_tick()
    assert report.deliveries_delivered == 1
    delivered = [row for row in store.deliveries_for_event(event_id) if row.status == "delivered"]
    assert len(delivered) == 1
    assert sink.calls == 1


async def test_async_notification_failure_retries(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = DomainEventStore(database)
    event_id = _seed_event_with_notification(store)
    sink = AsyncSink("fail")
    bus = EventBus(database, notifications=sink)
    bus.dispatch_tick()
    await asyncio.sleep(0.15)
    report = bus.dispatch_tick()
    assert report.deliveries_retried == 1
    rows = store.deliveries_for_event(event_id)
    assert rows[0].status == "pending"
    assert "sink down" in (rows[0].last_error or "")


async def test_async_notification_rejection_retries(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = DomainEventStore(database)
    event_id = _seed_event_with_notification(store)
    sink = AsyncSink("reject")
    bus = EventBus(database, notifications=sink)
    bus.dispatch_tick()
    await asyncio.sleep(0.15)
    report = bus.dispatch_tick()
    assert report.deliveries_retried == 1
    rows = store.deliveries_for_event(event_id)
    assert rows[0].status == "pending"


def test_stale_delivering_recovered_after_threshold(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = DomainEventStore(database)
    store.create_subscription(
        "test.sub2",
        subject_type="task",
        subject_id="",
        event_types=["TASK_COMPLETED"],
        handler_kind="notify",
        handler_config={"message": "m"},
        owner_id="",
    )
    event = store.publish(
        event_type="TASK_COMPLETED",
        subject_type="task",
        subject_id="task-2",
        payload={},
        owner_id="",
    )
    assert event is not None
    store.fan_out(event)
    store.mark_outbox_dispatched(event.event_id)
    delivery = store.due_deliveries(owner_id=None)[0]
    claimed = store.mark_delivering(delivery.delivery_id)
    assert claimed is not None
    assert store.recover_stale_delivering(older_than_seconds=300) == 0
    time.sleep(0.01)
    assert store.recover_stale_delivering(older_than_seconds=0.001) == 1
    recovered = store.due_deliveries(owner_id=None)
    assert recovered[0].delivery_id == delivery.delivery_id


def test_stale_delivering_excludes_active_ids(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = DomainEventStore(database)
    event = store.publish(
        event_type="TASK_COMPLETED",
        subject_type="task",
        subject_id="task-3",
        payload={},
        owner_id="",
    )
    assert event is not None
    store.create_subscription(
        "test.sub3",
        subject_type="task",
        subject_id="",
        event_types=["TASK_COMPLETED"],
        handler_kind="notify",
        handler_config={"message": "m"},
        owner_id="",
    )
    store.fan_out(event)
    store.mark_outbox_dispatched(event.event_id)
    delivery = store.due_deliveries(owner_id=None)[0]
    store.mark_delivering(delivery.delivery_id)
    assert (
        store.recover_stale_delivering(older_than_seconds=0.001, exclude={delivery.delivery_id})
        == 0
    )


def test_job_claim_skips_busy_owner(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = JobStore(database)
    busy_job = store.enqueue("busy-owner", "busy objective")
    free_job = store.enqueue("free-owner", "free objective")
    claimed_busy = store.claim("w1", lease_seconds=60)
    assert claimed_busy is not None
    assert claimed_busy.job_id == busy_job.job_id
    stolen = store.claim("w2", lease_seconds=60, owner_limit=1)
    assert stolen is not None
    assert stolen.job_id == free_job.job_id
    assert stolen.owner_id == "free-owner"
    assert store.claim("w3", lease_seconds=60, owner_limit=1) is None
