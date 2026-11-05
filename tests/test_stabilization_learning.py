from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus.learning.evaluation import EvaluationEngine
from eurus.learning.integration import LearningCoordinator
from eurus.learning.lessons import LessonStore
from eurus.runtime.models import Run, RunStatus, RuntimeEvent
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.jobs import Job
from eurus.storage.runs import RunStore


def make_job(job_id: str, lease_version: int = 1) -> Job:
    return Job(
        job_id=job_id,
        owner_id="alice",
        objective="study things",
        status="completed",
        schedule_id=None,
        scheduled_at=None,
        payload={"kind": "manual"},
        attempts=1,
        max_attempts=1,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=lease_version,
        side_effect_started=False,
        cancel_requested=False,
        result="output",
        error=None,
        created_at="",
        updated_at="",
        source="manual",
    )


def seed_run(database: Database, run_id: str, *, status: RunStatus, stop: str | None) -> Run:
    store = RunStore(database)
    run = Run(run_id=run_id, owner_id="alice", objective="study things", budget={})
    run.status = status
    run.stop_reason = stop
    run.result_ref = "runs/result.txt" if status == RunStatus.COMPLETED else None
    store.create(run)
    return run


def test_completion_enqueues_exactly_one_learning_task(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=False)
    job = make_job("job-1")
    coordinator.on_job_finished(job, "completed", "out", None)
    coordinator.on_job_finished(job, "completed", "out", None)
    tasks = coordinator.pending_tasks()
    assert len(tasks) == 1
    assert tasks[0].run_id == "job-1"
    assert tasks[0].job_id == "job-1"
    assert tasks[0].owner_id == "alice"
    database.close()


def test_replayed_job_creates_a_separate_learning_task(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=False)
    coordinator.on_job_finished(make_job("job-1", lease_version=1), "failed", "", "boom")
    coordinator.on_job_finished(make_job("job-1", lease_version=2), "completed", "out", None)
    tasks = coordinator.pending_tasks()
    assert {task.run_id for task in tasks} == {"job-1", "job-1.2"}
    database.close()


def test_non_terminal_statuses_are_ignored(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=False)
    coordinator.on_job_finished(make_job("job-1"), "queued", "", None)
    assert coordinator.pending_tasks() == []
    database.close()


def test_processing_records_evaluations_with_provenance(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    seed_run(database, "job-1", status=RunStatus.COMPLETED, stop=None)
    coordinator = LearningCoordinator(database, process_inline=False)
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    report = coordinator.tick()
    assert report.processed == 1
    assert report.evaluations_recorded >= 1
    evaluations = EvaluationEngine(database).list_evaluations(
        subject_type="job", subject_id="job-1"
    )
    assert any(item.dimension == "task_correctness" and item.passed for item in evaluations)
    engine = EvaluationEngine(database)
    provenance = engine.provenance_for("evaluation", evaluations[0].evaluation_id)
    assert provenance
    tasks = coordinator.pending_tasks()
    assert tasks == []
    database.close()


def test_failed_run_produces_failure_signal_lesson_candidate(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    lessons_path = tmp_path / "lessons.sqlite3"
    seed_run(database, "job-1", status=RunStatus.FAILED, stop="provider_timeout_reached")
    coordinator = LearningCoordinator(
        database, lessons=LessonStore(lessons_path), process_inline=False
    )
    coordinator.on_job_finished(make_job("job-1"), "failed", "", "provider timeout")
    report = coordinator.tick()
    assert report.processed == 1
    assert report.lessons_accepted == 1
    store = LessonStore(lessons_path)
    lessons = store.all(owner_id="alice")
    assert len(lessons) == 1
    assert "provider_timeout_reached" in lessons[0]
    assert store.all(owner_id="bob") == []
    database.close()


def append_lesson_event(database: Database, run_id: str, text: str) -> None:
    events = EventStore(database)
    events.append(
        RuntimeEvent(
            run_id=run_id,
            event_type="lesson",
            sequence_no=events.last_sequence(run_id) + 1,
            payload={"text": text},
        )
    )


def test_lesson_candidates_from_runtime_events_are_validated(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    lessons_path = tmp_path / "lessons.sqlite3"
    seed_run(database, "job-1", status=RunStatus.COMPLETED, stop=None)
    append_lesson_event(
        database, "job-1", "prefer the streaming API for large provider responses in jobs"
    )
    coordinator = LearningCoordinator(
        database, lessons=LessonStore(lessons_path), process_inline=False
    )
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    report = coordinator.tick()
    assert report.lessons_accepted == 1
    assert "streaming API" in LessonStore(lessons_path).all(owner_id="alice")[0]
    database.close()


def test_secret_like_lesson_candidates_are_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    lessons_path = tmp_path / "lessons.sqlite3"
    seed_run(database, "job-1", status=RunStatus.COMPLETED, stop=None)
    append_lesson_event(database, "job-1", "the api key sk-abcdefghijklmnop1234 works everywhere")
    coordinator = LearningCoordinator(
        database, lessons=LessonStore(lessons_path), process_inline=False
    )
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    report = coordinator.tick()
    stored = LessonStore(lessons_path).all(owner_id="alice")
    assert all("sk-abcdefghijklmnop1234" not in lesson for lesson in stored)
    assert report.lessons_accepted == len(stored)
    database.close()


def test_duplicate_lessons_are_not_rerecorded(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    lessons_path = tmp_path / "lessons.sqlite3"
    seed_run(database, "job-1", status=RunStatus.FAILED, stop="provider_timeout_reached")
    seed_run(database, "job-2", status=RunStatus.FAILED, stop="provider_timeout_reached")
    coordinator = LearningCoordinator(
        database, lessons=LessonStore(lessons_path), process_inline=False
    )
    coordinator.on_job_finished(make_job("job-1"), "failed", "", "provider timeout")
    coordinator.on_job_finished(make_job("job-2"), "failed", "", "provider timeout")
    report = coordinator.tick(limit=10)
    assert report.lessons_accepted == 1
    database.close()


def test_processing_failure_is_retried_then_marked_failed(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=False)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("evaluation backend down")

    coordinator.evaluation.record = explode
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    for _ in range(coordinator.max_process_attempts):
        coordinator.tick()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, attempts FROM eurus_learning_tasks WHERE job_id='job-1'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert int(row["attempts"]) >= coordinator.max_process_attempts
    database.close()


def test_stale_running_learning_task_is_requeued(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=False, stale_running_seconds=60)
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    with database.connection() as connection:
        connection.execute(
            "UPDATE eurus_learning_tasks SET status='running', updated_at=? WHERE job_id='job-1'",
            ((datetime.now(UTC) - timedelta(seconds=120)).isoformat(),),
        )
    report = coordinator.tick()
    assert report.requeued == 1
    database.close()


def test_deactivation_reverses_provenance_and_removes_lesson(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    lessons_path = tmp_path / "lessons.sqlite3"
    seed_run(database, "job-1", status=RunStatus.FAILED, stop="provider_timeout_reached")
    coordinator = LearningCoordinator(
        database, lessons=LessonStore(lessons_path), process_inline=False
    )
    coordinator.on_job_finished(make_job("job-1"), "failed", "", "provider timeout")
    coordinator.tick()
    store = LessonStore(lessons_path)
    assert len(store.all(owner_id="alice")) == 1
    with database.connection() as connection:
        row = connection.execute(
            "SELECT provenance_id, artifact_id FROM eurus_learning_provenance "
            "WHERE artifact_type='lesson'"
        ).fetchone()
    assert row is not None
    assert coordinator.deactivate(str(row["provenance_id"])) is True
    assert store.all(owner_id="alice") == []
    assert EvaluationEngine(database).get_provenance(str(row["provenance_id"])) is not None
    reversed_at = EvaluationEngine(database).get_provenance(str(row["provenance_id"]))
    assert reversed_at is not None and reversed_at["reversed_at"] is not None
    database.close()


def test_learning_schema_present_after_migration(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    with database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(eurus_learning_tasks)").fetchall()
        }
    assert {"task_id", "run_id", "job_id", "owner_id", "status", "attempts"} <= columns
    database.close()


def test_inline_processing_does_not_block_on_success(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = LearningCoordinator(database, process_inline=True)
    coordinator.on_job_finished(make_job("job-1"), "completed", "out", None)
    tasks = coordinator.pending_tasks()
    assert tasks == []
    database.close()
