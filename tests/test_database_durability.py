"""B4 regression tests: SQLite durability, connection reuse and async writes.

Before the fix, every storage operation opened a brand-new connection with
default pragmas (rollback journal, FULL synchronous sync) and every write ran
synchronously on the event loop thread.
"""

from __future__ import annotations

import asyncio

import pytest

from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore


def test_database_enables_wal_and_normal_synchronous(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    with database.connection() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    assert str(mode).lower() == "wal"
    assert synchronous == 1, "synchronous must be NORMAL (1) under WAL"
    assert busy_timeout == 10000


def test_connections_are_reused_from_the_pool(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    with database.connection() as first:
        first_id = id(first)
    with database.connection() as second:
        second_id = id(second)
    assert first_id == second_id, "released connections must be reused, not reopened"


def test_nested_checkouts_stay_isolated(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    with database.connection() as outer:
        outer.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER)")
        with database.connection() as inner:
            assert inner is not outer, "nested checkouts must get their own connection"
            inner.execute("INSERT INTO probe (id) VALUES (1)")
        outer.execute("INSERT INTO probe (id) VALUES (2)")


def test_connection_returns_to_pool_after_failure(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    with database.connection() as connection:
        connection.execute("CREATE TABLE probe (id INTEGER)")
    with pytest.raises(RuntimeError), database.connection() as connection:
        connection.execute("INSERT INTO probe (id) VALUES (1)")
        raise RuntimeError("abort")
    with database.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert count == 0, "failed transaction must roll back"
    with database.connection() as connection:
        connection.execute("INSERT INTO probe (id) VALUES (2)")
    with database.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert count == 1, "pooled connection must stay usable after a rollback"


def test_close_releases_pooled_connections(tmp_path):
    database = Database(tmp_path / "runtime.sqlite3")
    with database.connection() as connection:
        connection.execute("CREATE TABLE probe (id INTEGER)")
    database.close()
    with database.connection() as connection:
        connection.execute("CREATE TABLE probe2 (id INTEGER)")
    database.close()


@pytest.mark.asyncio
async def test_attempt_store_async_writes(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts.sqlite3")
    await store.record_pending_async(
        run_id="run-b4",
        tool_name="write_file",
        call_key="write_file:{}",
        attempt_id="attempt-b4",
        risk_level="high",
    )
    await store.record_async(
        ToolExecutionOutcome(
            call_id="call-b4",
            tool_name="write_file",
            status=ToolExecutionStatus.SUCCESS,
            call_key="write_file:{}",
            attempt_id="attempt-b4",
            run_id="run-b4",
            risk_level="high",
        )
    )
    rows = store.list_for_run("run-b4")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"


@pytest.mark.asyncio
async def test_event_store_async_append(tmp_path):
    from eurus.runtime.models import RuntimeEvent

    store = EventStore(tmp_path / "events.sqlite3")
    run_store = RunStore(store.database)
    from eurus.runtime.models import Run

    run = Run(owner_id="op", objective="async append")
    run_store.create(run)
    event = RuntimeEvent(run_id=run.run_id, event_type="probe.event", sequence_no=1)
    appended = await store.append_async(event)
    assert appended is event
    listed = store.list_for_run(run.run_id)
    assert [item.event_type for item in listed] == ["probe.event"]


@pytest.mark.asyncio
async def test_concurrent_async_writes_are_serialized(tmp_path):
    store = ToolAttemptStore(tmp_path / "concurrent.sqlite3")
    outcomes = [
        ToolExecutionOutcome(
            call_id=f"call-{index}",
            tool_name="write_file",
            status=ToolExecutionStatus.SUCCESS,
            call_key=f'write_file:{{"i": {index}}}',
            attempt_id=f"attempt-{index}",
            run_id="run-b4-concurrent",
            risk_level="high",
        )
        for index in range(12)
    ]
    await asyncio.gather(*(store.record_async(outcome) for outcome in outcomes))
    rows = store.list_for_run("run-b4-concurrent")
    assert len(rows) == 12
