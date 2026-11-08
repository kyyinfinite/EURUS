from __future__ import annotations

from pathlib import Path

from eurus.memory.manager import MemoryManager


def make_manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(tmp_path / "sessions.sqlite3")


def test_indonesian_statements_are_captured_by_type(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    cases = [
        (
            "Ingat bahwa deadline rilis adalah hari Jumat.",
            "fact",
            "deadline rilis adalah hari Jumat",
        ),
        (
            "Catat bahwa server staging memakai port 8081.",
            "fact",
            "server staging memakai port 8081",
        ),
        ("Aku lebih suka jawaban singkat.", "preference", "jawaban singkat"),
        ("Saya lebih memilih Python daripada Bash.", "preference", "Python daripada Bash"),
        (
            "Tolong selalu jalankan test sebelum commit.",
            "preference",
            "jalankan test sebelum commit",
        ),
        ("Keputusan proyek: gunakan SQLite WAL.", "project_fact", "gunakan SQLite WAL"),
        ("Untuk proyek ini, kami memakai uv.", "project_fact", "kami memakai uv"),
        (
            "Kami memutuskan bahwa schema berubah lewat migrasi.",
            "project_fact",
            "schema berubah lewat migrasi",
        ),
        ("Namaku Budi.", "profile", "Budi"),
        ("Nama saya Budi Putra.", "profile", "Budi Putra"),
        (
            "Alur kerja yang terbukti: build lalu test lalu deploy.",
            "procedural",
            "build lalu test lalu deploy",
        ),
        (
            "Untuk referensi ke depan: deployment memakai compose profile production.",
            "episode",
            "deployment memakai compose profile production",
        ),
    ]
    for text, memory_type, content in cases:
        candidates = manager.extract_candidates(text)
        assert candidates, text
        assert candidates[0]["memory_type"] == memory_type, text
        assert candidates[0]["content"] == content, text


def test_english_patterns_still_captured(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    candidates = manager.extract_candidates(
        "For this project, we decided that SQLite is the persistence layer."
    )
    assert candidates[0]["memory_type"] == "project_fact"
    assert candidates[0]["content"] == "we decided that SQLite is the persistence layer"
    fact = manager.extract_candidates("Remember that the build is cached")
    assert fact[0]["memory_type"] == "fact"
    assert fact[0]["content"] == "the build is cached"
    assert manager.extract_candidates("I prefer concise answers")[0]["memory_type"] == "preference"
    assert manager.extract_candidates("My name is Alice")[0]["memory_type"] == "profile"
    decided = manager.extract_candidates("We decided that SQLite is the persistence layer.")
    assert decided[0]["content"] == "SQLite is the persistence layer"


def test_plain_sentences_are_not_captured(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    negatives = [
        "Hallo, apa kabar?",
        "Aku suka kopi di pagi hari.",
        "Nama file konfigurasi itu settings.toml.",
        "Tolong baca file README dulu.",
        "Kami akan rapat besok pagi.",
        "Apa itu SQLite WAL?",
        "Halo, bisa tolong jelaskan bagian ini?",
    ]
    for text in negatives:
        assert manager.extract_candidates(text) == [], text


def test_indonesian_statement_is_stored_and_recallable(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    result = manager.learn_from_interaction(
        "Ingat bahwa deadline rilis adalah hari Jumat.", scope="/project/id"
    )
    assert result == {"considered": 1, "accepted": 1, "rejected": 0}
    rows = manager.recall("deadline rilis", scope="/project/id")
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "fact"
    assert rows[0]["source"] == "auto:explicit_user_statement"


def test_sensitive_indonesian_statements_are_never_stored(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    statements = [
        "Ingat bahwa nomor kartu kreditku adalah 4111111111111111.",
        "Ingat bahwa agama saya adalah Islam.",
        "Catat bahwa saya didiagnosis dengan diabetes.",
        "Ingat bahwa nik saya 3175012345678901.",
        "Ingat bahwa kondisi medisku butuh obat rutin.",
    ]
    for text in statements:
        result = manager.learn_from_interaction(text, scope="/project/id")
        assert result["considered"] == 0, text
    assert manager.recall("kartu kredit", scope="/project/id") == []
    assert manager.recall("agama", scope="/project/id") == []
    assert manager.recall("diabetes", scope="/project/id") == []


def test_sensitive_english_statements_are_still_rejected(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    result = manager.learn_from_interaction(
        "Remember that my religion requires special scheduling.", scope="/project/a"
    )
    assert result["considered"] == 0
