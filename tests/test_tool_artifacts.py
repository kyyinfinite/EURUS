from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.multiagent.artifacts import (
    STANDALONE_MISSION_ID,
    ArtifactError,
    ArtifactStore,
)
from eurus.storage.database import Database
from eurus.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from eurus.tools.artifacts import (
    ArtifactProvider,
    artifact_provider,
    classify_artifact,
    register_tool_artifact,
)
from eurus.tools.base import ToolContext


def _store(tmp_path: Path) -> ArtifactStore:
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.commit()
    connection.close()
    return ArtifactStore(Database(database), tmp_path / "artifacts")


def test_migration_fifteen_adds_run_scoped_columns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.publish(
        "payload",
        owner_id="eurus",
        mission_id="m1",
        artifact_type="report",
        name="report.txt",
        source_tool="office_specialist",
        run_id="run-1",
    )
    assert record.name == "report.txt"
    assert record.source_tool == "office_specialist"
    assert record.run_id == "run-1"
    assert record.expires_at is None
    assert LATEST_SCHEMA_VERSION >= 15


def test_publish_rejects_expired_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactError):
        store.publish(
            "payload",
            owner_id="eurus",
            mission_id="m1",
            artifact_type="report",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )


def test_read_rejects_expired_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.publish(
        "payload",
        owner_id="eurus",
        mission_id="m1",
        artifact_type="report",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=50),
    )
    while datetime.now(UTC) <= record.expires_at:
        continue
    with pytest.raises(ArtifactError, match="expired"):
        store.read(record.artifact_id, owner_id="eurus")


def test_list_for_run_and_recent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish("one", owner_id="eurus", mission_id="m1", artifact_type="report", run_id="run-1")
    store.publish("two", owner_id="eurus", mission_id="m2", artifact_type="report", run_id="run-2")
    store.publish(
        "three",
        owner_id="other",
        mission_id="m1",
        artifact_type="report",
        run_id="run-1",
    )
    run_rows = store.list_for_run("eurus", "run-1")
    assert [row.run_id for row in run_rows] == ["run-1"]
    recent = store.list_recent("eurus", limit=10)
    assert all(row.owner_id == "eurus" for row in recent)
    assert len(recent) == 2
    with pytest.raises(ArtifactError):
        store.list_for_run("eurus", "  ")


def test_list_recent_hides_expired(tmp_path: Path) -> None:
    import time

    store = _store(tmp_path)
    store.publish(
        "live",
        owner_id="eurus",
        mission_id="m1",
        artifact_type="report",
        run_id="run-1",
    )
    store.publish(
        "dead",
        owner_id="eurus",
        mission_id="m1",
        artifact_type="report",
        run_id="run-1",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=30),
    )
    time.sleep(0.1)
    rows = store.list_recent("eurus", limit=10)
    assert [row.run_id for row in rows if row.expires_at is not None] == []
    assert len(rows) == 1
    everything = store.list_recent("eurus", limit=10, include_expired=True)
    assert len(everything) == 2


def test_purge_expired_removes_rows_and_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.publish(
        "old",
        owner_id="eurus",
        mission_id="m1",
        artifact_type="report",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=50),
    )
    while datetime.now(UTC) <= record.expires_at:
        continue
    removed = store.purge_expired(now=datetime.now(UTC) + timedelta(seconds=5))
    assert removed >= 1
    with pytest.raises(KeyError):
        store.get_metadata(record.artifact_id, owner_id="eurus")


def test_backward_compatible_rows_get_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish("legacy", owner_id="eurus", mission_id="m1", artifact_type="report")
    with store.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_agent_artifacts SET name='', source_tool='', run_id='', expires_at=NULL"
        )
    records = store.list_for_mission("m1", owner_id="eurus")
    assert records[0].name == ""
    assert records[0].run_id == ""
    assert records[0].expires_at is None


def _context(tmp_path: Path, provider: ArtifactProvider | None) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        owner_id="eurus",
        run_id="run-9",
        mission_id="m-42",
        task_id="task-7",
        artifacts=provider,
    )


def test_register_tool_artifact_publishes_with_context_scope(tmp_path: Path) -> None:
    provider = artifact_provider(tmp_path / "sessions.db", tmp_path / "artifacts")
    target = tmp_path / "report.md"
    target.write_text("# report", encoding="utf-8")
    context = _context(tmp_path, provider)
    events: list[tuple[str, dict]] = []

    def sink(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    context = ToolContext(
        workspace=tmp_path,
        owner_id="eurus",
        run_id="run-9",
        mission_id="m-42",
        task_id="task-7",
        artifacts=provider,
        event_sink=sink,
    )
    view = register_tool_artifact(
        context, target, source_tool="document_create", artifact_type="document"
    )
    assert view is not None
    assert view["run_id"] == "run-9"
    record = provider().get_metadata(view["artifact_id"], owner_id="eurus")
    assert record.mission_id == "m-42"
    assert record.task_id == "task-7"
    assert record.source_tool == "document_create"
    assert record.media_type == "text/markdown"
    assert [item[0] for item in events] == ["artifact.registered"]


def test_register_tool_artifact_uses_standalone_scope_without_mission(tmp_path: Path) -> None:
    provider = artifact_provider(tmp_path / "sessions.db", tmp_path / "artifacts")
    target = tmp_path / "download.csv"
    target.write_text("a,b", encoding="utf-8")
    context = ToolContext(workspace=tmp_path, owner_id="eurus", run_id="run-10", artifacts=provider)
    view = register_tool_artifact(
        context, target, source_tool="browser_download", artifact_type="data"
    )
    assert view is not None
    record = provider().get_metadata(view["artifact_id"], owner_id="eurus")
    assert record.mission_id == STANDALONE_MISSION_ID
    assert record.media_type == "text/csv"


def test_register_tool_artifact_without_provider_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("x", encoding="utf-8")
    context = _context(tmp_path, None)
    assert register_tool_artifact(context, target, source_tool="document_create") is None


def test_register_tool_artifact_skips_missing_and_empty_files(tmp_path: Path) -> None:
    provider = artifact_provider(tmp_path / "sessions.db", tmp_path / "artifacts")
    context = _context(tmp_path, provider)
    assert register_tool_artifact(context, tmp_path / "missing.txt", source_tool="x") is None
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert register_tool_artifact(context, empty, source_tool="x") is None


def test_register_tool_artifact_skips_oversized_files(tmp_path: Path) -> None:
    provider = artifact_provider(tmp_path / "sessions.db", tmp_path / "artifacts")
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (provider().max_bytes + 1))
    context = _context(tmp_path, provider)
    assert register_tool_artifact(context, big, source_tool="office_specialist") is None


def test_classify_artifact_covers_common_suffixes() -> None:
    assert classify_artifact(Path("a.md")) == ("text", "text/markdown")
    assert classify_artifact(Path("a.pdf")) == ("document", "application/pdf")
    assert classify_artifact(Path("a.docx"))[0] == "document"
    assert classify_artifact(Path("a.unknown")) == ("text", "text/plain")


def test_provider_instances_are_cached(tmp_path: Path) -> None:
    first = artifact_provider(tmp_path / "a.db", tmp_path / "store")
    second = artifact_provider(tmp_path / "a.db", tmp_path / "store")
    assert first is second
    assert first() is first.store()
