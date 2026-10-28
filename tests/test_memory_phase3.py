from pathlib import Path

from eurus.memory.manager import (
    MemoryManager,
    MemoryProvider,
    SQLiteMemoryProvider,
    build_memory_provider,
)
from eurus.models import Message
from eurus.sessions import SessionStore


def test_session_search_indexes_messages_and_respects_project_scope(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save_messages(
        "project-a", [Message.user("decision use sqlite fts search")], project_scope="/project/a"
    )
    store.save_messages(
        "project-b", [Message.user("decision use sqlite fts search")], project_scope="/project/b"
    )

    rows = store.search_session_messages("sqlite search", project_scope="/project/a")

    assert [row["session_id"] for row in rows] == ["project-a"]
    assert rows[0]["role"] == "user"


def test_session_search_retrieves_historical_messages(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save_messages(
        "history",
        [
            Message.user("Remember the migration decision"),
            Message.assistant("Use additive SQLite migrations"),
        ],
    )

    rows = store.search_session_messages("additive migrations")

    assert len(rows) == 1
    assert rows[0]["content"] == "Use additive SQLite migrations"


def test_memory_search_is_scoped_and_global_memories_are_shared(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.add_memory("Use SQLite for project persistence", scope="/project/a")
    store.add_memory("Use SQLite for global preferences", scope="global")

    rows = store.search_memories("SQLite", scope="/project/a")

    assert {row["scope"] for row in rows} == {"/project/a", "global"}
    assert len(store.search_memories("SQLite", scope="/project/b")) == 1


def test_memory_provider_is_single_select_and_sqlite_backed(tmp_path: Path) -> None:
    provider = build_memory_provider("sqlite", tmp_path / "sessions.sqlite3")

    assert isinstance(provider, MemoryProvider)
    assert isinstance(provider, SQLiteMemoryProvider)
    memory_id = provider.remember("Keep concise answers", category="preference")
    assert provider.recall("concise answers")[0]["id"] == memory_id
    assert provider.forget(memory_id)


def test_memory_provider_rejects_unknown_backend(tmp_path: Path) -> None:
    try:
        build_memory_provider("unknown", tmp_path / "sessions.sqlite3")
    except ValueError as exc:
        assert "unsupported memory provider" in str(exc)
    else:
        raise AssertionError("unknown provider should fail closed")


def test_curated_context_is_bounded_and_scope_aware(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    manager.store.add_memory(
        "Prefer direct concise answers", memory_type="preference", scope="global"
    )
    manager.store.add_memory("Project uses Python", memory_type="project_fact", scope="/project/a")
    manager.store.add_memory(
        "Private other project detail", memory_type="project_fact", scope="/project/b"
    )

    context = manager.curated_context(scope="/project/a", max_chars=180)

    assert any("concise answers" in item for item in context)
    assert any("Project uses Python" in item for item in context)
    assert all("Private other project" not in item for item in context)
    assert sum(map(len, context)) <= 180


def test_privacy_export_and_purge_cover_saved_personal_data(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save_messages("private-session", [Message.user("private prompt")])
    store.add_memory("private note")
    store.record_event("test_event", session_id="private-session")

    exported = store.export_privacy_data()
    counts = store.purge_privacy_data()

    assert len(exported["sessions"]) == 1
    assert len(exported["messages"]) == 1
    assert len(exported["memories"]) == 1
    assert counts == {"sessions": 1, "messages": 1, "memories": 1, "events": 1}
    assert store.export_privacy_data() == {
        "sessions": [],
        "messages": [],
        "memories": [],
        "session_summaries": [],
        "session_lineage": [],
    }


def test_compacted_session_can_resume_from_original_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save_messages("original", [Message.user("first")])
    store.save_session_lineage("original", "compacted", "compact", "earlier context")
    store.save_messages("compacted", [Message.user("recent")])

    assert store.latest_session_descendant("original") == "compacted"


def test_memory_curation_nudge_is_rate_limited_by_event(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")

    assert store.memory_curation_due()
    store.record_event("memory_curation_nudge")
    assert not store.memory_curation_due()


def test_existing_session_database_is_backfilled_into_fts(tmp_path: Path) -> None:
    import json
    import sqlite3

    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE messages(session_id TEXT NOT NULL REFERENCES sessions(id) "
        "ON DELETE CASCADE, sequence INTEGER NOT NULL, payload TEXT NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY(session_id, sequence));"
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)",
        ("legacy", "legacy", "2026-01-01", "2026-01-01"),
    )
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?)",
        (
            "legacy",
            0,
            json.dumps({"role": "user", "content": "backfill historic message search"}),
            "2026-01-01",
        ),
    )
    connection.commit()
    connection.close()

    store = SessionStore(database)

    rows = store.search_session_messages("historic message")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "legacy"


def test_autonomous_memory_stores_explicit_durable_statements(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")

    result = manager.learn_from_interaction(
        "For this project, we decided that SQLite is the persistence layer.",
        scope="/project/a",
        session_id="session-a",
    )

    assert result == {"considered": 1, "accepted": 1, "rejected": 0}
    rows = manager.recall("SQLite persistence layer", scope="/project/a")
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "project_fact"
    assert rows[0]["source"] == "auto:explicit_user_statement"
    assert rows[0]["scope"] == "/project/a"


def test_autonomous_memory_rejects_duplicates_and_credentials(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    statement = "I prefer concise answers"

    first = manager.learn_from_interaction(statement, scope="/project/a")
    duplicate = manager.learn_from_interaction(statement + ".", scope="/project/a")
    credential = manager.learn_from_interaction(
        "Remember that api_key=sk-abcdefghijklmnopqrstuvwxyz", scope="/project/a"
    )

    assert first["accepted"] == 1
    assert duplicate["rejected"] == 1
    assert credential["considered"] == 0
    assert len(manager.recall("concise answers", scope="/project/a")) == 1
    assert manager.recall("api_key", scope="/project/a") == []


def test_autonomous_memory_does_not_cross_project_scopes(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    manager.learn_from_interaction(
        "For this project, use SQLite for persistence.", scope="/project/a"
    )

    assert manager.recall("SQLite persistence", scope="/project/b") == []
    assert len(manager.recall("SQLite persistence", scope="/project/a")) == 1


def test_memory_provenance_and_expiration_are_persisted(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3", retention_days=1)
    result = manager.learn_from_interaction(
        "For future reference, the deployment uses the production compose profile.",
        scope="/project/a",
        session_id="session-1",
    )
    assert result["accepted"] == 1
    rows = manager.recall("deployment production compose", scope="/project/a")
    assert rows[0]["memory_type"] == "episode"
    assert rows[0]["source"] == "auto:explicit_user_statement"
    import json

    provenance = json.loads(rows[0]["provenance"])
    assert provenance["session_id"] == "session-1"
    assert rows[0]["expires_at"]


def test_memory_correction_and_deletion_respect_scope(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    memory_id = manager.remember("Use SQLite for persistence", scope="/project/a")
    assert not manager.correct(memory_id, "Use Postgres instead", scope="/project/b")
    assert manager.correct(memory_id, "Use Postgres instead", scope="/project/a")
    assert "Postgres" in manager.recall("Postgres", scope="/project/a")[0]["content"]
    assert not manager.forget_scoped(memory_id, scope="/project/b")
    assert manager.forget_scoped(memory_id, scope="/project/a")


def test_memory_manager_rejects_credentials_for_manual_and_corrected_writes(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    try:
        manager.remember("my password is hunter2")
    except ValueError:
        pass
    else:
        raise AssertionError("credential should not be persisted")
    memory_id = manager.remember("Use SQLite for persistence")
    try:
        manager.update(memory_id, "api_key=sk-abcdefghijklmnopqrstuvwxyz")
    except ValueError:
        pass
    else:
        raise AssertionError("credential should not replace a memory")
    assert manager.recall("password", scope="global") == []


def test_procedural_memory_requires_explicit_validated_workflow_language(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    result = manager.learn_from_interaction(
        "Validated workflow: run unit tests, inspect failures, then rerun focused tests.",
        scope="/project/a",
    )
    assert result["accepted"] == 1
    recalled = manager.recall("unit tests inspect failures", scope="/project/a")
    assert recalled[0]["memory_type"] == "procedural"


def test_scoped_memory_management_cannot_edit_or_delete_global_memory(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3")
    memory_id = manager.remember("Global preference for concise answers", scope="global")
    assert not manager.correct(memory_id, "Global preference changed", scope="/project/a")
    assert not manager.forget_scoped(memory_id, scope="/project/a")
    assert manager.recall("concise answers", scope="/project/a")


def test_project_budget_does_not_evict_global_memories(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "sessions.sqlite3", max_memories=10)
    global_id = manager.remember("Global preference number one", scope="global")
    for index in range(11):
        manager.remember(f"Project-specific durable fact number {index}", scope="/project/a")
    manager.enforce_budget(scope="/project/a")
    assert manager.store.list_memories(limit=100, scope="global")
    assert any(row["id"] == global_id for row in manager.store.list_memories(limit=100))
