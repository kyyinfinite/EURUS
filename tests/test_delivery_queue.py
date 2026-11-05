from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.automation.deliveries import (
    DeliveryDispatcher,
    DeliveryStore,
    RoutingNotificationSink,
    TuiInboxSink,
    resolve_channels,
)
from eurus.automation.notifications import Notification
from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def notification(**overrides: object) -> Notification:
    values: dict[str, object] = {
        "kind": "job_completed",
        "title": "job_completed:abcd1234 completed",
        "body": "done",
        "result": "the actual result text",
        "job_id": "job-1",
        "attempt": 0,
        "deliver": "origin",
        "origin_gateway": "telegram",
        "origin_conversation": "4242",
    }
    values.update(overrides)
    return Notification(**values)


def test_resolve_channels_matrix() -> None:
    assert resolve_channels("origin", "telegram", "77") == [("telegram", "77")]
    assert resolve_channels("origin", "web", "s1") == [("web", "s1")]
    assert resolve_channels("origin", "tui", "") == [("tui", "")]
    assert resolve_channels("origin", "", "") == [("local", "")]
    assert resolve_channels("telegram:55", "", "") == [("telegram", "55")]
    assert resolve_channels("web", "", "s2") == [("web", "s2")]
    assert resolve_channels("local", "", "") == [("local", "")]
    assert resolve_channels("", "", "") == [("local", "")]


def test_sink_routes_by_deliver_and_respects_silent_prefix(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    sink = RoutingNotificationSink(store)
    asyncio.run(sink.deliver(notification()))
    rows = store.list_deliveries()
    assert len(rows) == 1
    assert rows[0].channel == "telegram"
    assert rows[0].target == "4242"
    assert rows[0].status == "queued"
    assert rows[0].body == "the actual result text"

    asyncio.run(sink.deliver(notification(job_id="job-2", result="[SILENT] internal")))
    suppressed = store.list_deliveries(statuses=("suppressed",))
    assert [row.job_id for row in suppressed] == ["job-2"]


def test_sink_silent_flag_and_local_fallback(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    sink = RoutingNotificationSink(store)
    asyncio.run(sink.deliver(notification(job_id="job-3", silent=True, deliver="local")))
    rows = store.list_deliveries()
    assert rows[0].status == "suppressed"
    assert rows[0].channel == "local"
    asyncio.run(sink.deliver(notification(job_id="job-4", deliver="unknown-mode")))
    assert store.list_deliveries()[-1].channel == "local"


def test_dispatcher_delivers_and_writes_job_status_ok(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    database = Database(home / "data" / "eurus.sqlite3")
    jobs = JobStore(database)
    job = jobs.enqueue("eurus", "objective", payload={"deliver": "origin"})
    jobs.finish(job.job_id, "w1", job.lease_version, status="completed", result="done")
    sink = RoutingNotificationSink(store)
    sent: list[tuple[str, str, str]] = []

    async def sender(target: str, subject: str, body: str) -> None:
        sent.append((target, subject, body))

    asyncio.run(sink.deliver(notification(job_id=job.job_id)))
    dispatcher = DeliveryDispatcher(store, "telegram", sender, poll_seconds=0.1)

    async def scenario() -> None:
        task = asyncio.create_task(dispatcher.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sent:
                break
        await dispatcher.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(sent) == 1
    assert sent[0][0] == "4242"
    refreshed = jobs.get(job.job_id)
    assert refreshed is not None
    assert refreshed.delivery_status == "ok"


def test_channel_failure_is_isolated_and_marks_delivery_failed(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    database = Database(home / "data" / "eurus.sqlite3")
    jobs = JobStore(database)
    job = jobs.enqueue("eurus", "objective")
    jobs.finish(job.job_id, "w1", job.lease_version, status="completed", result="done")
    delivered: list[str] = []

    async def good_sender(target: str, subject: str, body: str) -> None:
        delivered.append(target)

    async def broken_sender(target: str, subject: str, body: str) -> None:
        raise RuntimeError("telegram down")

    async def scenario() -> None:
        sink2 = RoutingNotificationSink(store)
        await sink2.deliver(
            notification(
                job_id=job.job_id,
                deliver="web",
                origin_gateway="web",
                origin_conversation="s1",
            )
        )
        await sink2.deliver(notification(job_id=job.job_id, attempt=0, deliver="tui"))
        good = DeliveryDispatcher(store, "web", good_sender, poll_seconds=0.1)
        bad = DeliveryDispatcher(store, "tui", broken_sender, poll_seconds=0.1)
        tasks = [asyncio.create_task(good.run()), asyncio.create_task(bad.run())]
        for _ in range(200):
            await asyncio.sleep(0.02)
            rows = store.pending_for_execution(f"{job.job_id}:0")
            if all(row.status in ("delivered", "failed") for row in rows):
                break
        for dispatcher, task in ((good, tasks[0]), (bad, tasks[1])):
            await dispatcher.stop()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert delivered == ["s1"]
    rows = store.pending_for_execution(f"{job.job_id}:0")
    statuses = {row.channel: row.status for row in rows}
    assert statuses["web"] == "delivered"
    assert statuses["tui"] in ("failed", "queued", "sending")
    refreshed = jobs.get(job.job_id)
    assert refreshed is not None
    assert refreshed.delivery_status == "delivery_failed"


def test_restart_continues_queue_without_duplicates(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    sink = RoutingNotificationSink(store)
    sent: list[str] = []

    async def sender(target: str, subject: str, body: str) -> None:
        sent.append(target)

    async def scenario() -> None:
        await sink.deliver(notification(job_id="j-a", deliver="local"))
        await sink.deliver(notification(job_id="j-b", deliver="local"))
        first = DeliveryDispatcher(store, "local", sender, poll_seconds=0.1)
        task = asyncio.create_task(first.run())
        await asyncio.sleep(0.06)
        await first.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert len(sent) >= 1
        second = DeliveryDispatcher(store, "local", sender, poll_seconds=0.1)
        task2 = asyncio.create_task(second.run())
        for _ in range(200):
            await asyncio.sleep(0.02)
            if not store.list_deliveries(statuses=("queued", "sending")):
                break
        await second.stop()
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)
        assert not store.list_deliveries(statuses=("queued", "sending"))
        assert len(sent) == 2

    asyncio.run(scenario())


def test_recover_sending_after_crash(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    sink = RoutingNotificationSink(store)
    asyncio.run(sink.deliver(notification(job_id="j-crash")))
    claimed = store.claim_due("telegram")
    assert len(claimed) == 1
    assert store.list_deliveries(statuses=("sending",))
    assert store.recover_sending() == 1
    assert not store.list_deliveries(statuses=("sending",))
    assert len(store.list_deliveries(statuses=("queued",))) == 1


def test_enqueue_is_idempotent_per_execution(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    sink = RoutingNotificationSink(store)
    asyncio.run(sink.deliver(notification(job_id="job-1")))
    asyncio.run(sink.deliver(notification(job_id="job-1")))
    rows = store.list_deliveries()
    assert len(rows) == 1


def test_scheduled_origin_delivery_reaches_channel_on_time(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    database = Database(home / "data" / "eurus.sqlite3")
    jobs = JobStore(database)
    schedules = ScheduleStore(home / "data" / "eurus.sqlite3")
    fake_now = datetime.now(UTC).replace(microsecond=0)
    schedule = schedules.create(
        "eurus",
        "ping the chat",
        kind="once",
        run_at=(fake_now + timedelta(seconds=1)).isoformat(),
        deliver="origin",
        origin_gateway="telegram",
        origin_conversation="999",
    )
    dispatched = schedules.dispatch_due(now=fake_now + timedelta(seconds=2))
    assert schedule.schedule_id in dispatched or dispatched
    job = jobs.list_jobs(owner_id="eurus", limit=1)[0]
    assert job.payload["origin_conversation"] == "999"
    jobs.finish(job.job_id, "w1", job.lease_version, status="completed", result="ping done")
    sink = RoutingNotificationSink(store)
    sent: list[tuple[str, str]] = []

    async def sender(target: str, subject: str, body: str) -> None:
        sent.append((target, body))

    async def scenario() -> None:
        await sink.deliver(
            Notification(
                kind="job_completed",
                title="job_completed completed",
                body="",
                result="ping done",
                job_id=job.job_id,
                attempt=job.attempts,
                deliver=str(job.payload.get("deliver", "")),
                origin_gateway=str(job.payload.get("origin_gateway", "")),
                origin_conversation=str(job.payload.get("origin_conversation", "")),
            )
        )
        dispatcher = DeliveryDispatcher(store, "telegram", sender, poll_seconds=0.1)
        task = asyncio.create_task(dispatcher.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sent:
                break
        await dispatcher.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert sent == [("999", "ping done")]
    refreshed = jobs.get(job.job_id)
    assert refreshed is not None
    assert refreshed.delivery_status == "ok"


def test_tui_sink_marks_rows_delivered(home: Path) -> None:
    store = DeliveryStore(home / "data" / "eurus.sqlite3")
    database = Database(home / "data" / "eurus.sqlite3")
    jobs = JobStore(database)
    job = jobs.enqueue("eurus", "objective", payload={"deliver": "tui"})
    jobs.finish(job.job_id, "w1", job.lease_version, status="completed", result="done")
    sink = RoutingNotificationSink(store)
    asyncio.run(sink.deliver(notification(job_id=job.job_id, deliver="tui", origin_gateway="tui")))
    dispatcher = DeliveryDispatcher(store, "tui", TuiInboxSink(), poll_seconds=0.1)

    async def scenario() -> None:
        task = asyncio.create_task(dispatcher.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            rows = store.pending_for_execution(f"{job.job_id}:0")
            if rows and rows[0].status == "delivered":
                break
        await dispatcher.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    rows = store.pending_for_execution(f"{job.job_id}:0")
    assert rows[0].status == "delivered"
    refreshed = jobs.get(job.job_id)
    assert refreshed is not None
    assert refreshed.delivery_status == "ok"


def test_local_sink_writes_file(home: Path) -> None:
    from eurus.automation.deliveries import LocalFileSink

    sink = LocalFileSink(home / "inbox")
    asyncio.run(sink("ignored-target", "Daily digest", "all quiet"))
    files = list((home / "inbox").glob("*.txt"))
    assert len(files) == 1
    assert "all quiet" in files[0].read_text(encoding="utf-8")


def test_delivery_migration_is_additive_and_idempotent(home: Path) -> None:
    database_path = home / "data" / "eurus.sqlite3"
    database = Database(database_path)
    from eurus.storage.migrations import LATEST_SCHEMA_VERSION

    with database.connection() as connection:
        version = connection.execute(
            "SELECT MAX(version) AS version FROM eurus_schema_migrations"
        ).fetchone()
    assert int(version["version"]) == LATEST_SCHEMA_VERSION
    database.close()

    connection = sqlite3.connect(database_path)
    connection.execute("ALTER TABLE eurus_jobs DROP COLUMN delivery_status")
    connection.execute("DELETE FROM eurus_schema_migrations WHERE version=11")
    connection.commit()
    connection.close()

    reopened = Database(database_path)
    with reopened.connection() as db:
        columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(eurus_jobs)")}
        row_count = db.execute("SELECT COUNT(*) AS total FROM eurus_jobs").fetchone()
    assert "delivery_status" in columns
    assert int(row_count["total"]) == 0
    reopened.close()
