from __future__ import annotations

import sqlite3
from pathlib import Path

from eurus.owner import OWNER_ID
from eurus.storage.database import Database
from eurus.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations


def build_legacy_schema(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE eurus_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    from eurus.storage.migrations import _MIGRATIONS

    for target in sorted(_MIGRATIONS):
        if target > version:
            break
        for statement in _MIGRATIONS[target]:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO eurus_schema_migrations(version, applied_at) "
            "VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    connection.commit()
    connection.close()


def test_fresh_database_reaches_latest_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3")
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(version) FROM eurus_schema_migrations").fetchone()[
            0
        ]
    assert int(version) == LATEST_SCHEMA_VERSION
    database.close()


def test_migration_from_v7_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "v7.sqlite3"
    build_legacy_schema(path, 7)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO eurus_jobs (job_id, schedule_id, scheduled_at, owner_id, objective, "
        "payload_json, status, attempts, max_attempts, available_at, lease_version, "
        "side_effect_started, cancel_requested, result, created_at, updated_at) "
        "VALUES ('legacy-job', NULL, NULL, 'alice', 'legacy objective', '{}', 'failed', "
        "1, 1, '2026-01-01T00:00:00+00:00', 1, 0, 0, '', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO eurus_dead_letters (dead_letter_id, job_id, owner_id, objective, "
        "source, reason, detail, attempts, payload_json, created_at) "
        "VALUES ('letter-1', 'legacy-job', 'alice', 'legacy objective', 'manual', "
        "'operator_failed', 'boom', 1, '{}', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(version) FROM eurus_schema_migrations").fetchone()[
            0
        ]
        job = connection.execute(
            "SELECT status, objective FROM eurus_jobs WHERE job_id='legacy-job'"
        ).fetchone()
        letters = connection.execute("SELECT COUNT(*) FROM eurus_dead_letters").fetchone()[0]
        learning_table = connection.execute("SELECT COUNT(*) FROM eurus_learning_tasks").fetchone()[
            0
        ]
    assert int(version) == LATEST_SCHEMA_VERSION
    assert job is not None
    assert job["status"] == "failed"
    assert job["objective"] == "legacy objective"
    assert int(letters) == 1
    assert int(learning_table) == 0
    database.close()


def test_migration_deduplicates_legacy_open_dead_letters(tmp_path: Path) -> None:
    path = tmp_path / "dupes.sqlite3"
    build_legacy_schema(path, 7)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO eurus_jobs (job_id, schedule_id, scheduled_at, owner_id, objective, "
        "payload_json, status, attempts, max_attempts, available_at, lease_version, "
        "side_effect_started, cancel_requested, result, created_at, updated_at) "
        "VALUES ('dup-job', NULL, NULL, 'alice', 'dup objective', '{}', 'failed', "
        "1, 1, '2026-01-01T00:00:00+00:00', 1, 0, 0, '', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    letters = [
        ("a", "2026-01-01T00:00:00+00:00"),
        ("b", "2026-01-02T00:00:00+00:00"),
    ]
    for letter_id, created in letters:
        connection.execute(
            "INSERT INTO eurus_dead_letters (dead_letter_id, job_id, owner_id, objective, "
            "source, reason, detail, attempts, payload_json, created_at) "
            "VALUES (?, 'dup-job', 'alice', 'dup objective', 'manual', "
            "'operator_failed', 'boom', 1, '{}', ?)",
            (letter_id, created),
        )
    connection.commit()
    connection.close()

    database = Database(path)
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT dead_letter_id FROM eurus_dead_letters WHERE resolved_at IS NULL"
        ).fetchall()
    assert [row["dead_letter_id"] for row in rows] == ["b"]
    database.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idem.sqlite3"
    connection = sqlite3.connect(path)
    first = apply_migrations(connection)
    second = apply_migrations(connection)
    assert first == second == LATEST_SCHEMA_VERSION
    connection.close()


def test_interrupted_migration_is_retried_safely(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE eurus_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    from eurus.storage.migrations import _MIGRATIONS

    for statement in _MIGRATIONS[1]:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO eurus_schema_migrations(version, applied_at) "
        "VALUES (1, '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(version) FROM eurus_schema_migrations").fetchone()[
            0
        ]
    assert int(version) == LATEST_SCHEMA_VERSION
    database.close()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE eurus_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO eurus_schema_migrations(version, applied_at) "
        "VALUES (999, '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    try:
        Database(path)
    except RuntimeError as exc:
        assert "newer than this Eurus version" in str(exc)
    else:
        raise AssertionError("opening a newer database must fail")
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE eurus_schema_migrations")
    connection.close()


def test_migration_from_v8_preserves_event_ownership_and_dedup(tmp_path: Path) -> None:
    path = tmp_path / "v8.sqlite3"
    build_legacy_schema(path, 8)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO eurus_domain_events (event_id, event_type, subject_type, subject_id, "
        "payload_json, correlation_id, causation_id, dedup_key, occurred_at, created_at) "
        "VALUES ('legacy-event', 'PING', '', 's1', '{}', '', '', 'ext-1', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO eurus_event_subscriptions (subscription_id, name, event_types_json, "
        "subject_type, subject_id, handler_kind, handler_config_json, enabled, "
        "created_at, updated_at) VALUES ('legacy-sub', 'legacy', '[]', '', '', "
        "'submit_job', '{}', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(version) FROM eurus_schema_migrations").fetchone()[
            0
        ]
        event = connection.execute(
            "SELECT owner_id, dedup_key FROM eurus_domain_events WHERE event_id='legacy-event'"
        ).fetchone()
        subscription = connection.execute(
            "SELECT owner_id FROM eurus_event_subscriptions WHERE subscription_id='legacy-sub'"
        ).fetchone()
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='eurus_domain_events'"
            ).fetchall()
        }
    assert int(version) == LATEST_SCHEMA_VERSION
    assert event is not None
    assert event["owner_id"] == OWNER_ID
    assert subscription is not None and subscription["owner_id"] == OWNER_ID
    assert "eurus_domain_events_owner" in indexes

    with database.connection() as connection:
        connection.execute(
            "INSERT INTO eurus_domain_events (event_id, event_type, dedup_key, "
            "occurred_at, created_at, owner_id) VALUES ('bob-event', 'PING', 'ext-1', "
            "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00', 'bob')"
        )
        with_conflict = False
        try:
            connection.execute(
                "INSERT INTO eurus_domain_events (event_id, event_type, dedup_key, "
                "occurred_at, created_at, owner_id) VALUES ('alice-dupe', 'PING', 'ext-1', "
                "'2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00', 'bob')"
            )
        except sqlite3.IntegrityError:
            with_conflict = True
    assert with_conflict is True
    database.close()


def test_v9_events_support_owner_scoped_queries(tmp_path: Path) -> None:
    from eurus.events.bus import EventBus

    path = tmp_path / "v9.sqlite3"
    build_legacy_schema(path, 8)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO eurus_domain_events (event_id, event_type, dedup_key, "
        "occurred_at, created_at) VALUES ('old-event', 'PING', 'k1', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO eurus_outbox (outbox_id, event_id, status, created_at) "
        "VALUES ('old-outbox', 'old-event', 'pending', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    bus = EventBus(database)
    scoped = bus.store.pending_outbox(owner_id=OWNER_ID)
    assert [event.event_id for event in scoped] == ["old-event"]
    admin = bus.store.pending_outbox()
    assert len(admin) == 1
    database.close()
