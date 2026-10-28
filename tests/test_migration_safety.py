from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eurus.sessions import SessionStore
from eurus.storage.database import Database
from eurus.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_fresh_database_reaches_latest_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3")
    with database.connection() as connection:
        applied = {
            int(row[0]) for row in connection.execute("SELECT version FROM eurus_schema_migrations")
        }
        assert applied == set(range(1, LATEST_SCHEMA_VERSION + 1))
        tables = _table_names(connection)
    assert "eurus_runs" in tables
    assert "eurus_tool_attempts" in tables


def _build_v1_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE eurus_schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        INSERT INTO eurus_schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00');
        CREATE TABLE eurus_runs (
            run_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            parent_run_id TEXT,
            session_id TEXT,
            status TEXT NOT NULL,
            stop_reason TEXT,
            budget_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            schema_version INTEGER NOT NULL,
            checkpoint_version INTEGER NOT NULL,
            result_ref TEXT,
            status_version INTEGER NOT NULL
        );
        INSERT INTO eurus_runs VALUES (
            'legacy-run', 'legacy-owner', 'legacy objective', NULL, NULL,
            'completed', 'done', '{}', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:01:00+00:00', NULL, NULL, 1, 0, NULL, 1
        );
        CREATE TABLE eurus_run_checkpoints (
            run_id TEXT NOT NULL REFERENCES eurus_runs(run_id) ON DELETE CASCADE,
            checkpoint_version INTEGER NOT NULL,
            checkpoint_type TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, checkpoint_version)
        );
        CREATE TABLE eurus_runtime_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES eurus_runs(run_id) ON DELETE CASCADE,
            sequence_no INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            step_id TEXT,
            attempt_id TEXT,
            parent_run_id TEXT,
            UNIQUE (run_id, sequence_no)
        );
        INSERT INTO eurus_runtime_events VALUES (
            'evt-1', 'legacy-run', 1, 'run.started', '{}', 1,
            '2026-01-01T00:00:00+00:00', NULL, NULL, NULL
        );
        """
    )
    connection.commit()
    connection.close()


def test_migration_from_v1_preserves_existing_records(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _build_v1_database(legacy)
    database = Database(legacy)
    with database.connection() as connection:
        applied = {
            int(row[0]) for row in connection.execute("SELECT version FROM eurus_schema_migrations")
        }
        assert applied == set(range(1, LATEST_SCHEMA_VERSION + 1))
        run = connection.execute(
            "SELECT owner_id, objective, status FROM eurus_runs WHERE run_id = 'legacy-run'"
        ).fetchone()
        event = connection.execute(
            "SELECT event_type FROM eurus_runtime_events WHERE event_id = 'evt-1'"
        ).fetchone()
        tables = _table_names(connection)
    assert run is not None and run["owner_id"] == "legacy-owner"
    assert event is not None and event["event_type"] == "run.started"
    assert "eurus_jobs" in tables
    assert "eurus_run_controls" in tables
    assert "eurus_tool_attempts" in tables


def test_migrations_are_repeatable(tmp_path: Path) -> None:
    database = Database(tmp_path / "repeat.sqlite3")
    with database.connection() as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)
    assert first == LATEST_SCHEMA_VERSION
    assert second == LATEST_SCHEMA_VERSION


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE eurus_schema_migrations (\n"
        "    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL\n"
        ");"
        f"INSERT INTO eurus_schema_migrations VALUES ({LATEST_SCHEMA_VERSION + 1}, 'x');"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer than this Eurus version"):
        Database(path)


def test_memory_database_gains_trust_column_without_losing_rows(tmp_path: Path) -> None:
    legacy = tmp_path / "memories.sqlite3"
    store = SessionStore(legacy)
    with store._connection() as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(memories)")}
        assert "trust" in columns
        connection.execute("ALTER TABLE memories DROP COLUMN trust")
    upgraded = SessionStore(legacy)
    with upgraded._connection() as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(memories)")}
        assert "trust" in columns
    memory_id = upgraded.add_memory("survives upgrade", trust="derived")
    row = upgraded.get_memory(memory_id)
    assert row is not None and row["content"] == "survives upgrade"
    assert row["trust"] == "derived"
