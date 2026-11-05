from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.config import AgenticEngineeringConfig, Config, LearningConfig, load_config
from eurus.errors import ConfigError
from eurus.learning.evaluation import EvaluationEngine
from eurus.learning.integration import LearningCoordinator
from eurus.learning.lessons import LessonStore, lesson_scope
from eurus.multiagent.artifacts import ArtifactError, ArtifactStore
from eurus.storage.database import Database


def _old(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class TestRankedLessonRetrieval:
    def test_relevant_ranks_by_objective_similarity(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", limit=3, max_lessons=500)
        lessons = [
            "flaky pytest fixtures make tests fail nightly",
            "pytest fixtures fail with stale cache",
            "nightly tests catch regressions early",
            "tests must pass before merging branches",
            "pytest needs the plugin installed first",
            "fail fast on broken builds",
        ]
        for lesson in lessons:
            assert store.add(lesson) is not None
        found = store.relevant("tests flaky pytest fixtures fail nightly")
        assert found == [
            "flaky pytest fixtures make tests fail nightly",
            "pytest fixtures fail with stale cache",
            "nightly tests catch regressions early",
        ]

    def test_relevant_returns_store_order_when_pool_fits_limit(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", limit=5, max_lessons=500)
        store.add("Run the tests after every edit to confirm the fix.")
        store.add("Prefer small patches over rewriting whole files.")
        found = store.relevant("fix the failing tests")
        assert found == ["Run the tests after every edit to confirm the fix."]

    def test_relevant_empty_objective_returns_nothing(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3")
        store.add("Run the tests after every edit to confirm the fix.")
        assert store.relevant("the and for with") == []
        assert store.relevant("") == []

    def test_relevant_owner_scoping_survives_ranking(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", limit=3, max_lessons=500)
        lessons = [
            "pin dependency versions before cutting the release",
            "restart the worker pool after config reloads",
            "archive build logs before the disk fills up",
            "reindex the search cache when migrations run",
            "rotate the credentials on a fixed schedule",
            "snapshot the database before schema changes",
        ]
        for lesson in lessons:
            assert store.add(lesson, owner_id="alice") is not None
        found = store.relevant("rotate credentials on a fixed schedule", owner_id="alice")
        assert found[:1] == ["rotate the credentials on a fixed schedule"]
        assert store.relevant("rotate credentials on a fixed schedule", owner_id="bob") == []


class TestLessonRotation:
    def test_add_rotates_oldest_lesson_in_same_scope(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", max_lessons=2)
        assert store.add("first lesson about running tests", owner_id="alice") is not None
        assert store.add("second lesson about small patches", owner_id="alice") is not None
        assert store.add("third lesson about git diffs", owner_id="alice") is not None
        remaining = store.all(owner_id="alice")
        assert remaining == [
            "third lesson about git diffs",
            "second lesson about small patches",
        ]

    def test_owner_adds_are_not_blocked_by_global_capacity(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", max_lessons=2)
        assert store.add("global lesson one about testing", shared=True, owner_id="root")
        assert store.add("global lesson two about patches", shared=True, owner_id="root")
        assert store.add("alice lesson about pytest", owner_id="alice") is not None
        assert store.add("alice lesson about coverage", owner_id="alice") is not None
        assert store.all() == [
            "global lesson two about patches",
            "global lesson one about testing",
        ]
        assert store.all(owner_id="alice") == [
            "alice lesson about coverage",
            "alice lesson about pytest",
        ]

    def test_shared_add_rotates_global_scope_only(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", max_lessons=2)
        assert store.add("global lesson one about testing", shared=True, owner_id="root")
        assert store.add("global lesson two about patches", shared=True, owner_id="root")
        assert store.add("alice lesson about pytest", owner_id="alice") is not None
        assert store.add("global lesson three about reviews", shared=True, owner_id="root")
        assert store.all() == [
            "global lesson three about reviews",
            "global lesson two about patches",
        ]
        assert store.all(owner_id="alice") == [
            "global lesson three about reviews",
            "alice lesson about pytest",
        ]

    def test_duplicate_refusal_does_not_rotate(self, tmp_path: Path) -> None:
        store = LessonStore(tmp_path / "lessons.sqlite3", max_lessons=2)
        store.add("run the tests after every edit")
        store.add("prefer small patches over rewrites")
        assert store.add("run the tests after every edit!") is None
        assert store.all() == [
            "prefer small patches over rewrites",
            "run the tests after every edit",
        ]

    def test_scope_helper_matches_rotation_scopes(self) -> None:
        assert lesson_scope(None) == "global"
        assert lesson_scope("  ") == "global"
        assert lesson_scope("alice") == "owner:alice"


class TestEvaluationRetention:
    def _engine(self, tmp_path: Path) -> EvaluationEngine:
        return EvaluationEngine(Database(tmp_path / "learning.sqlite3"))

    def _backdate(self, engine: EvaluationEngine, evaluation_id: str, days: int) -> None:
        with engine.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_evaluations SET created_at=? WHERE evaluation_id=?",
                (_old(days), evaluation_id),
            )

    def test_prune_removes_old_rows_and_their_provenance(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        fresh = engine.record(
            subject_type="run",
            subject_id="run-1",
            dimension="task_correctness",
            score=1.0,
            passed=True,
        )
        stale = engine.record(
            subject_type="run",
            subject_id="run-2",
            dimension="task_correctness",
            score=0.2,
            passed=False,
        )
        self._backdate(engine, stale.evaluation_id, 400)
        lesson_provenance = engine.record_artifact_provenance(
            artifact_type="lesson", artifact_id="42", summary="lesson kept"
        )
        removed = engine.prune(retention_days=180)
        assert removed == 1
        assert engine.list_evaluations(subject_type="run", subject_id="run-2") == []
        remaining = engine.list_evaluations(subject_type="run", subject_id="run-1")
        assert [item.evaluation_id for item in remaining] == [fresh.evaluation_id]
        assert engine.get_provenance(stale.provenance_id) is None
        assert engine.get_provenance(lesson_provenance) is not None

    def test_prune_preserves_mission_learning_markers(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        marker = engine.record(
            subject_type="mission",
            subject_id="mission-old",
            dimension="plan_quality",
            score=0.4,
            passed=False,
        )
        self._backdate(engine, marker.evaluation_id, 400)
        removed = engine.prune(retention_days=30)
        assert removed == 0
        kept = engine.list_evaluations(
            subject_type="mission", subject_id="mission-old", dimension="plan_quality"
        )
        assert [item.evaluation_id for item in kept] == [marker.evaluation_id]
        assert engine.get_provenance(marker.provenance_id) is not None

    def test_prune_respects_the_retention_boundary(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        recent = engine.record(
            subject_type="run",
            subject_id="run-new",
            dimension="tool_reliability",
            score=1.0,
            passed=True,
        )
        self._backdate(engine, recent.evaluation_id, 29)
        assert engine.prune(retention_days=30) == 0
        self._backdate(engine, recent.evaluation_id, 31)
        assert engine.prune(retention_days=30) == 1

    def test_prune_is_batched_and_repeatable(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        for index in range(5):
            evaluation = engine.record(
                subject_type="run",
                subject_id=f"run-{index}",
                dimension="tool_reliability",
                score=1.0,
                passed=True,
            )
            self._backdate(engine, evaluation.evaluation_id, 400)
        assert engine.prune(retention_days=180, batch_limit=2) == 2
        assert engine.prune(retention_days=180, batch_limit=2) == 2
        assert engine.prune(retention_days=180, batch_limit=2) == 1
        assert engine.prune(retention_days=180, batch_limit=2) == 0

    def test_prune_disabled_and_validation(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        assert engine.prune(retention_days=0) == 0
        assert engine.prune(retention_days=-5) == 0
        with pytest.raises(ValueError):
            engine.prune(retention_days=10, batch_limit=0)
        with pytest.raises(ValueError):
            engine.prune(retention_days=10, batch_limit=2001)

    def test_coordinator_tick_prunes_when_retention_configured(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "learning.sqlite3")
        engine = EvaluationEngine(database)
        evaluation = engine.record(
            subject_type="run",
            subject_id="run-1",
            dimension="tool_reliability",
            score=1.0,
            passed=True,
        )
        with database.connection() as connection:
            connection.execute(
                "UPDATE eurus_evaluations SET created_at=? WHERE evaluation_id=?",
                (_old(400), evaluation.evaluation_id),
            )
        with_retention = LearningCoordinator(
            database, evaluation=engine, evaluation_retention_days=180
        )
        report = with_retention.tick()
        assert report.evaluations_pruned == 1
        without_retention = LearningCoordinator(database, evaluation=engine)
        assert without_retention.tick().evaluations_pruned == 0

    def test_coordinator_retention_validation(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "learning.sqlite3")
        with pytest.raises(ValueError):
            LearningCoordinator(database, evaluation_retention_days=-1)
        with pytest.raises(ValueError):
            LearningCoordinator(database, evaluation_retention_days=3651)
        LearningCoordinator(database, evaluation_retention_days=3650)

    def test_build_stack_wires_retention_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        from dataclasses import replace

        from eurus.cli.autonomy import build_stack

        config = replace(
            Config(),
            learning=replace(LearningConfig(), evaluation_retention_days=90),
            agentic_engineering=replace(AgenticEngineeringConfig(), enabled=False),
        )
        stack = build_stack(config, workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert stack.learning is not None
            assert stack.learning.evaluation_retention_days == 90
        finally:
            stack.close()

    def test_learning_config_retention_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        environ = {"EURUS_HOME": str(tmp_path)}
        config = load_config(cwd=tmp_path, environ=environ)
        assert config.learning.evaluation_retention_days == 180
        (tmp_path / "config.toml").write_text(
            "[learning]\nevaluation_retention_days = 0\n", encoding="utf-8"
        )
        config = load_config(cwd=tmp_path, environ=environ)
        assert config.learning.evaluation_retention_days == 0
        (tmp_path / "config.toml").write_text(
            "[learning]\nevaluation_retention_days = 3651\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            load_config(cwd=tmp_path, environ=environ)
        (tmp_path / "config.toml").write_text(
            "[learning]\nevaluation_retention_days = -1\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            load_config(cwd=tmp_path, environ=environ)


class TestArtifactPagination:
    def _store(self, tmp_path: Path) -> ArtifactStore:
        return ArtifactStore(tmp_path / "artifacts.sqlite3", tmp_path / "artifact-files")

    def _publish(self, store: ArtifactStore, name: str, **overrides) -> str:
        record = store.publish(
            b"payload",
            owner_id="eurus",
            mission_id="m-1",
            artifact_type="report",
            name=name,
            source_tool="office_specialist",
            run_id="run-1",
            **overrides,
        )
        return record.artifact_id

    def test_pages_walk_the_whole_registry(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        published = [self._publish(store, f"report-{index}.txt") for index in range(5)]
        first, cursor = store.list_page("eurus", limit=2)
        assert [record.artifact_id for record in first] == [published[4], published[3]]
        assert cursor is not None
        second, cursor = store.list_page("eurus", limit=2, cursor=cursor)
        assert [record.artifact_id for record in second] == [published[2], published[1]]
        assert cursor is not None
        third, cursor = store.list_page("eurus", limit=2, cursor=cursor)
        assert [record.artifact_id for record in third] == [published[0]]
        assert cursor is None

    def test_pages_do_not_skip_or_repeat_on_tied_timestamps(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        published = [self._publish(store, f"tied-{index}.txt") for index in range(4)]
        same = datetime.now(UTC).isoformat()
        with store.database.connection() as connection:
            connection.execute("UPDATE eurus_agent_artifacts SET created_at=?", (same,))
        seen: list[str] = []
        cursor: str | None = None
        while True:
            records, cursor = store.list_page("eurus", limit=2, cursor=cursor)
            seen.extend(record.artifact_id for record in records)
            if cursor is None:
                break
        assert sorted(seen) == sorted(published)
        assert len(seen) == len(set(seen))

    def test_expired_artifacts_leave_pages_and_counts(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        self._publish(store, "fresh.txt")
        expired = self._publish(
            store, "gone.txt", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
        with store.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_agent_artifacts SET expires_at=? WHERE artifact_id=?",
                (_old(1), expired),
            )
        records, cursor = store.list_page("eurus", limit=10)
        assert [record.name for record in records] == ["fresh.txt"]
        assert cursor is None
        assert store.count_active("eurus") == 1
        records, _ = store.list_page("eurus", limit=10, include_expired=True)
        assert {record.name for record in records} == {"fresh.txt", "gone.txt"}
        assert store.count_active("eurus", include_expired=True) == 2

    def test_pages_are_owner_scoped(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        mine = self._publish(store, "mine.txt")
        store.publish(
            b"payload",
            owner_id="bob",
            mission_id="m-1",
            artifact_type="report",
            name="theirs.txt",
            source_tool="office_specialist",
            run_id="run-1",
        )
        records, cursor = store.list_page("eurus", limit=10)
        assert [record.artifact_id for record in records] == [mine]
        assert cursor is None
        assert store.count_active("eurus") == 1
        assert store.count_active("bob") == 1

    def test_malformed_cursor_rejected(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        self._publish(store, "fresh.txt")
        for bad in ("not-a-cursor", "a|", "|b", " | ", "a|b|c"):
            with pytest.raises(ArtifactError):
                store.list_page("eurus", limit=10, cursor=bad)

    def test_limit_is_clamped(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        self._publish(store, "only.txt")
        records, cursor = store.list_page("eurus", limit=0)
        assert [record.name for record in records] == ["only.txt"]
        assert cursor is None
