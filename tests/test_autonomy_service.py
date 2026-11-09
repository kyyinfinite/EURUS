from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from eurus.autonomy_service import AUTONOMY_SUBSYSTEMS, AutonomyService, RuntimeLock
from eurus.config import build_config
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def make_service(home: Path, **overrides: object) -> AutonomyService:
    raw: dict[str, object] = {
        "agent": {"model": ""},
        "autonomy": {"poll_seconds": 0.05, **overrides},
    }
    config = build_config(raw)
    return AutonomyService(config, home=home)


def test_owner_mode_runs_workers_and_supervisor(home: Path) -> None:
    service = make_service(home)

    async def scenario() -> None:
        status = await service.start()
        try:
            assert status["mode"] == "owner"
            assert status["pid"] is not None
            assert status["metrics"]["jobs_submitted"] == 0
            assert "jobs_by_status" not in status
            health = service.health()
            assert health.get("status") in {"healthy", "degraded"}
        finally:
            await service.stop()
        assert service.status()["mode"] == "stopped"

    asyncio.run(scenario())


def test_second_service_becomes_client(home: Path) -> None:
    first = make_service(home)
    second = make_service(home)

    async def scenario() -> None:
        await first.start()
        status = await second.start()
        try:
            assert status["mode"] == "client"
            assert status["pid"] is None
            assert "jobs_by_status" in status["metrics"]
            await second.stop()
            assert second.status()["mode"] == "stopped"
            assert first.status()["mode"] == "owner"
        finally:
            await first.stop()

    asyncio.run(scenario())


def test_disabled_config_reports_disabled(home: Path) -> None:
    service = make_service(home, enabled=False)

    async def scenario() -> None:
        status = await service.start()
        try:
            assert status["mode"] == "disabled"
            assert service.health()["running"] is False
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_runtime_lock_is_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "autonomy.lock"
    first = RuntimeLock(lock_path)
    second = RuntimeLock(lock_path)
    assert first.acquire() is True
    assert first.held is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
    assert second.held is False


def test_toggle_subsystem_without_restart(home: Path) -> None:
    service = make_service(home)

    async def scenario() -> None:
        await service.start()
        try:
            assert service.set_subsystem("goals", False) is True
            assert service.status()["subsystems"]["goals"] is False
            state = json.loads((home / "data" / "autonomy_state.json").read_text())
            assert state["toggles"]["goals"] is False
            restarted = AutonomyService(service.config, home=home)
            assert restarted.status()["subsystems"]["goals"] is False
            with pytest.raises(ValueError):
                service.set_subsystem("unknown", False)
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_daily_dispatch_limiter_blocks_after_limit(home: Path) -> None:
    from eurus.autonomy_service import DailyDispatchLimiter
    from eurus.scheduler import ScheduleStore

    database_path = home / "data" / "eurus.sqlite3"
    store = JobStore(Database(database_path))
    schedule_store = ScheduleStore(database_path)
    limiter = DailyDispatchLimiter(schedule_store, store, limit=2, owner_id="eurus")
    for _ in range(2):
        store.enqueue("eurus", "queued work", source="schedule")
    assert limiter._dispatched_today() == 2
    schedule_store.create(
        "eurus",
        "run me",
        kind="interval",
        interval_seconds=60,
    )
    assert limiter.dispatch_due() == []
    assert len(store.list_jobs(statuses=("queued",), limit=100)) == 2


def test_owner_migration_rewrites_legacy_owners_and_keeps_rows(home: Path) -> None:
    database_path = home / "data" / "eurus.sqlite3"
    database = Database(database_path)
    with database.connection() as connection:
        connection.execute("DELETE FROM eurus_schema_migrations WHERE version=10")
    store = JobStore(database)
    store.enqueue("web", "web job")
    store.enqueue("local", "local job")
    with database.connection() as connection:
        before = connection.execute("SELECT COUNT(*) AS total FROM eurus_jobs").fetchone()
    database.close()

    reopened = Database(database_path)
    with reopened.connection() as connection:
        rows = connection.execute("SELECT owner_id FROM eurus_jobs").fetchall()
        version = connection.execute(
            "SELECT MAX(version) AS version FROM eurus_schema_migrations"
        ).fetchone()
    assert int(before["total"]) == len(rows)
    assert {str(row["owner_id"]) for row in rows} == {"eurus"}
    assert int(version["version"]) >= 10
    reopened.close()


def test_schedule_owner_migration_via_store_marker(home: Path) -> None:
    from eurus.scheduler import ScheduleStore
    from eurus.storage.migrations import apply_migrations

    database_path = home / "data" / "eurus.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    apply_migrations(connection)
    connection.execute("DELETE FROM eurus_schema_migrations WHERE version=10")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS eurus_schedules ("
        "schedule_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, objective TEXT NOT NULL, "
        "kind TEXT NOT NULL, timezone TEXT NOT NULL, next_run_at TEXT NOT NULL, "
        "interval_seconds INTEGER, enabled INTEGER NOT NULL, profile_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO eurus_schedules (schedule_id, owner_id, objective, kind, timezone, "
        "next_run_at, interval_seconds, enabled, profile_json, created_at, updated_at) "
        "VALUES ('s1', 'telegram:42', 'legacy', 'once', 'UTC', '2030-01-01T00:00:00+00:00', "
        "NULL, 1, '{}', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    store = ScheduleStore(database_path)
    schedule = store.get("s1")
    assert schedule is not None
    assert schedule.owner_id == "eurus"
    assert store.list_owner("eurus")[0].schedule_id == "s1"


def test_service_status_shape(home: Path) -> None:
    service = make_service(home)
    assert service.status()["mode"] == "stopped"
    assert set(service.status()["subsystems"]) == set(AUTONOMY_SUBSYSTEMS)


def test_web_gateway_lifespan_starts_autonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from eurus.config import load_config
    from eurus.gateway.web.app import build_context, create_app
    from eurus.gateway.web.auth import GatewaySecurity

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    with TestClient(app):
        status = ctx.autonomy.status()
        assert status["mode"] in {"owner", "client"}
        assert status["config"]["enabled"] is True
    assert ctx.autonomy.status()["mode"] == "stopped"
