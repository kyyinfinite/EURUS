from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.automation.supervision import RuntimeSupervisor, SupervisionReport
from eurus.runtime.liveness import reap_stale_runs
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.state_machine import transition_run
from eurus.storage.database import Database
from eurus.storage.runs import RunStore


def _store(tmp_path: Path) -> RunStore:
    return RunStore(Database(tmp_path / "runs.sqlite3"))


def _seed_run(store: RunStore, *, status: RunStatus = RunStatus.RUNNING) -> Run:
    run = Run(owner_id="local", objective="stay alive")
    store.create(run)
    if status is not run.status:
        transition_run(run, status)
        store.save(run)
    return run


def test_migration_adds_heartbeat_column(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.database.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(eurus_runs)").fetchall()
        }
    assert "heartbeat_at" in columns


def test_heartbeat_updates_only_running_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    running = _seed_run(store)
    queued = _seed_run(store, status=RunStatus.QUEUED)
    assert store.heartbeat(running.run_id) is True
    assert store.heartbeat(queued.run_id) is False
    assert store.heartbeat("missing") is False


def test_stale_running_uses_heartbeat_and_updated_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fresh = _seed_run(store)
    stale = _seed_run(store)
    old = datetime.now(UTC) - timedelta(seconds=2000)
    with store.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_runs SET heartbeat_at=? WHERE run_id=?",
            (old.isoformat(), fresh.run_id),
        )
        connection.execute(
            "UPDATE eurus_runs SET updated_at=? WHERE run_id=?",
            (old.isoformat(), stale.run_id),
        )
    found = store.stale_running(older_than_seconds=1000)
    assert {run.run_id for run in found} == {fresh.run_id, stale.run_id}
    assert store.stale_running(older_than_seconds=3000) == []


def test_reap_stale_runs_interrupts_only_stale(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stale = _seed_run(store)
    live = _seed_run(store)
    old = datetime.now(UTC) - timedelta(seconds=2000)
    with store.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_runs SET updated_at=? WHERE run_id=?",
            (old.isoformat(), stale.run_id),
        )
    reaped = reap_stale_runs(store, older_than_seconds=1000)
    assert reaped == [stale.run_id]
    assert store.get(stale.run_id).status is RunStatus.INTERRUPTED
    assert store.get(stale.run_id).stop_reason == "heartbeat_expired"
    assert store.get(live.run_id).status is RunStatus.RUNNING


def test_reap_skips_concurrently_changed_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    stale = _seed_run(store)
    old = datetime.now(UTC) - timedelta(seconds=2000)
    with store.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_runs SET updated_at=? WHERE run_id=?",
            (old.isoformat(), stale.run_id),
        )
    snapshot = store.get(stale.run_id)
    assert snapshot is not None
    raced = store.get(stale.run_id)
    assert raced is not None
    transition_run(raced, RunStatus.INTERRUPTED, stop_reason="operator_cancelled")
    store.save(raced, expected_status_version=raced.status_version - 1)
    monkeypatch.setattr(store, "stale_running", lambda **_: [snapshot], raising=True)
    reaped = reap_stale_runs(store, older_than_seconds=1000)
    assert reaped == []
    assert store.get(stale.run_id).stop_reason == "operator_cancelled"


def test_durable_cancel_accepts_queued_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    queued = _seed_run(store, status=RunStatus.QUEUED)
    assert store.request_cancel(queued.run_id) is True
    assert store.cancellation_requested(queued.run_id) is True


def test_supervisor_reports_reaped_runs(tmp_path: Path) -> None:
    from eurus.automation.engine import AutomationEngine

    store = _store(tmp_path)
    database = store.database
    stale = _seed_run(store)
    old = datetime.now(UTC) - timedelta(seconds=2000)
    with database.connection() as connection:
        connection.execute(
            "UPDATE eurus_runs SET updated_at=? WHERE run_id=?",
            (old.isoformat(), stale.run_id),
        )

    class NoopExecutor:
        async def execute(self, job: object, context: object) -> str:
            return "noop"

    automation = AutomationEngine(database, NoopExecutor())
    supervisor = RuntimeSupervisor(automation, runs=store)
    report = supervisor.supervise()
    assert isinstance(report, SupervisionReport)
    assert report.runs_reaped == [stale.run_id]
    assert store.get(stale.run_id).status is RunStatus.INTERRUPTED
    database.close()
