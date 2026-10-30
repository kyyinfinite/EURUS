"""Phase 0: opt-in timing spans and SQLite accounting."""

from __future__ import annotations

import sqlite3

import pytest

from eurus.runtime import instrumentation
from eurus.storage.database import Database


@pytest.fixture(autouse=True)
def _reset():
    instrumentation.disable()
    instrumentation.clear()
    yield
    instrumentation.disable()
    instrumentation.clear()


def test_disabled_span_has_zero_overhead_and_records_nothing():
    with instrumentation.span("nothing"):
        pass
    instrumentation.record("nothing", 1.0)
    assert instrumentation.snapshot() == []
    assert instrumentation.summary() == {}


def test_enabled_span_records_duration():
    instrumentation.enable()
    with instrumentation.span("unit"):
        pass
    rows = instrumentation.snapshot()
    assert len(rows) == 1
    assert rows[0]["name"] == "unit"
    assert rows[0]["ms"] >= 0
    summary = instrumentation.summary()
    assert summary["unit"]["count"] == 1


def test_record_and_summary_aggregation():
    instrumentation.enable()
    instrumentation.record("x", 0.001)
    instrumentation.record("x", 0.003)
    bucket = instrumentation.summary()["x"]
    assert bucket["count"] == 2
    assert bucket["total_ms"] == pytest.approx(4.0, abs=0.001)
    assert bucket["max_ms"] == pytest.approx(3.0, abs=0.001)


def test_sql_accounting_counts_statements(tmp_path):
    instrumentation.enable()
    database = Database(tmp_path / "db.sqlite3")
    with database.connection() as connection:
        connection.execute("SELECT 1").fetchall()  # warm the pool (PRAGMA setup)
    instrumentation.clear()
    with database.connection() as connection:
        connection.execute("CREATE TABLE t (a TEXT)")
        connection.execute("INSERT INTO t VALUES ('x')")
        connection.execute("SELECT * FROM t").fetchall()
    statements, seconds = instrumentation.sql_snapshot()
    assert statements == 3
    assert seconds >= 0


def test_sql_accounting_disabled(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    with database.connection() as connection:
        connection.execute("CREATE TABLE t (a TEXT)")
    assert instrumentation.sql_snapshot() == (0, 0.0)


def test_sql_counter_survives_rollback(tmp_path):
    instrumentation.enable()
    database = Database(tmp_path / "db.sqlite3")
    with pytest.raises(sqlite3.OperationalError), database.connection() as connection:
        connection.execute("SELECT * FROM missing_table")
    statements, _ = instrumentation.sql_snapshot()
    assert statements >= 1


def test_format_summary_lists_labels():
    instrumentation.enable()
    instrumentation.record("alpha", 0.002)
    text = instrumentation.format_summary("turn")
    assert "turn" in text and "alpha" in text


def test_dump_writes_json(tmp_path):
    instrumentation.enable()
    instrumentation.record("beta", 0.001)
    target = tmp_path / "timing" / "turn.json"
    instrumentation.dump(str(target), {"turn_id": "t1"})
    import json

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["turn_id"] == "t1"
    assert any(row["name"] == "beta" for row in payload["spans"])
