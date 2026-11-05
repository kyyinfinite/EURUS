from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eurus.learning.evaluation import (
    EvaluationEngine,
    LearningPipeline,
    LessonValidator,
)
from eurus.learning.lessons import LessonStore
from eurus.runtime.models import Run, RunStatus
from eurus.storage.database import Database


@dataclass(slots=True)
class FakeAttempt:
    tool_name: str
    status: str = "succeeded"
    is_error: bool = False

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def attempts(*entries: tuple[str, bool]) -> list[dict[str, Any]]:
    return [
        {"tool_name": name, "status": "failed" if failed else "succeeded", "is_error": failed}
        for name, failed in entries
    ]


def test_evaluation_record_and_list(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    evaluation = engine.record(
        subject_type="run",
        subject_id="run-1",
        dimension="task_correctness",
        score=1.0,
        passed=True,
        evidence=["status=completed", "result=present"],
    )
    assert evaluation.evaluation_id
    assert evaluation.provenance_id
    listed = engine.list_evaluations(subject_type="run", subject_id="run-1")
    assert len(listed) == 1
    assert listed[0].dimension == "task_correctness"
    assert listed[0].passed is True
    assert listed[0].evidence == ("status=completed", "result=present")


def test_evaluation_validation(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    with pytest.raises(ValueError):
        engine.record(
            subject_type="run",
            subject_id="run-1",
            dimension="happiness",
            score=1.0,
            passed=True,
        )
    with pytest.raises(ValueError):
        engine.record(
            subject_type="run",
            subject_id="run-1",
            dimension="task_correctness",
            score=2.0,
            passed=True,
        )
    with pytest.raises(ValueError):
        engine.record(
            subject_type="",
            subject_id="run-1",
            dimension="task_correctness",
            score=1.0,
            passed=True,
        )


def test_evaluate_run_deterministic_dimensions(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    run = Run(run_id="run-ok", owner_id="owner", objective="Do work")
    run.status = RunStatus.COMPLETED
    run.result_ref = "result text"
    evaluations = engine.evaluate_run(run, attempts(("read_file", False), ("write_file", False)))
    by_dimension = {item.dimension: item for item in evaluations}
    assert by_dimension["task_correctness"].passed is True
    assert by_dimension["task_correctness"].score == 1.0
    assert by_dimension["tool_reliability"].score == 1.0
    assert by_dimension["resource_efficiency"].passed is True


def test_evaluate_run_failure_paths(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    failed = Run(run_id="run-fail", owner_id="owner", objective="Do work")
    failed.status = RunStatus.FAILED
    failed.stop_reason = "budget_exhausted"
    evaluations = engine.evaluate_run(
        failed, attempts(("web_search", True), ("web_search", True), ("read_file", False))
    )
    by_dimension = {item.dimension: item for item in evaluations}
    assert by_dimension["task_correctness"].passed is False
    assert by_dimension["task_correctness"].score == 0.0
    assert by_dimension["tool_reliability"].score == pytest.approx(1 / 3, abs=0.001)
    assert by_dimension["tool_reliability"].passed is False
    assert by_dimension["resource_efficiency"].score == 0.2


def test_goal_evaluation(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    evaluation = engine.evaluate_goal(
        goal_id="goal-1",
        progress=1.0,
        all_tasks_succeeded=True,
        evidence=["task 'collect' succeeded", "task 'write' succeeded"],
    )
    assert evaluation.passed is True
    partial = engine.evaluate_goal(goal_id="goal-1", progress=0.5, all_tasks_succeeded=False)
    assert partial.passed is False
    assert partial.score == 0.5


def test_provenance_reversal(tmp_path: Path) -> None:
    engine = EvaluationEngine(Database(tmp_path / "eval.sqlite3"))
    provenance_id = engine.record_artifact_provenance(
        artifact_type="lesson",
        artifact_id="42",
        summary="prefer precise file paths",
        source_run_id="run-1",
    )
    active = engine.provenance_for("lesson", "42")
    assert len(active) == 1
    assert engine.reverse(provenance_id) is True
    assert engine.reverse(provenance_id) is False
    assert engine.provenance_for("lesson", "42") == []
    with pytest.raises(ValueError):
        engine.record_artifact_provenance(
            artifact_type="unfalsifiable", artifact_id="x", summary=""
        )


def test_lesson_validator_rejects_bad_lessons(tmp_path: Path) -> None:
    validator = LessonValidator(None)
    ok, reason = validator.review(
        __import__("eurus.learning.evaluation", fromlist=["LessonCandidate"]).LessonCandidate(
            "Always pass explicit paths to file tools when writing reports"
        )
    )
    assert ok is True
    short_ok, _ = validator.review(
        __import__("eurus.learning.evaluation", fromlist=["LessonCandidate"]).LessonCandidate(
            "too short"
        )
    )
    assert short_ok is False
    secret_ok, secret_reason = validator.review(
        __import__("eurus.learning.evaluation", fromlist=["LessonCandidate"]).LessonCandidate(
            "use the key sk-abcdef1234567890abcdef for faster results in tests"
        )
    )
    assert secret_ok is False
    assert "secret" in secret_reason
    newline_ok, _ = validator.review(
        __import__("eurus.learning.evaluation", fromlist=["LessonCandidate"]).LessonCandidate(
            "first line\nsecond line that is long enough for the length check"
        )
    )
    assert newline_ok is False


def test_learning_pipeline_end_to_end(tmp_path: Path) -> None:
    database = Database(tmp_path / "learning.sqlite3")
    sessions = tmp_path / "sessions.db"
    lessons = LessonStore(sessions)
    engine = EvaluationEngine(database)
    pipeline = LearningPipeline(engine, lessons=lessons)
    run = Run(run_id="run-learn", owner_id="owner", objective="Write the report")
    run.status = RunStatus.COMPLETED
    run.result_ref = "report content"
    report = pipeline.process_run(
        run,
        attempts(("read_file", False), ("write_file", False)),
        lesson_candidates=[
            "Always verify the output file exists after writing reports",
            "too short",
            "use token sk-abcdef1234567890abcdef1234 whenever possible",
        ],
    )
    assert len(report.evaluations) == 3
    accepted = report.lesson_reviews[0]
    assert accepted.accepted is True
    assert accepted.lesson_id is not None
    assert accepted.provenance_id is not None
    assert report.lesson_reviews[1].accepted is False
    assert report.lesson_reviews[2].accepted is False
    assert any("Always verify the output file exists" in lesson for lesson in lessons.all())
    duplicate = pipeline.process_run(
        run,
        attempts(),
        lesson_candidates=["Always verify the output file exists after writing reports"],
    )
    assert duplicate.lesson_reviews[0].accepted is False
    assert "duplicate" in duplicate.lesson_reviews[0].reason


def test_learning_pipeline_without_lesson_store(tmp_path: Path) -> None:
    database = Database(tmp_path / "learning.sqlite3")
    engine = EvaluationEngine(database)
    pipeline = LearningPipeline(engine)
    run = Run(run_id="run-bare", owner_id="owner", objective="Bare run")
    run.status = RunStatus.COMPLETED
    run.result_ref = "done"
    report = pipeline.process_run(run, attempts(), lesson_candidates=[])
    assert len(report.evaluations) == 2
    assert report.accepted_lessons == []


def test_skill_draft_provenance(tmp_path: Path) -> None:
    from eurus.learning.skills import SkillDraftStore

    database = Database(tmp_path / "skills.sqlite3")
    engine = EvaluationEngine(database)
    pipeline = LearningPipeline(engine)
    store = SkillDraftStore(tmp_path)
    draft = store.draft(
        "report-writer",
        "Writes structured reports",
        "# report writer\n\nUse this skill when asked to write a report with sections.",
    )
    provenance_id = pipeline.record_skill_draft(draft, source_run_id="run-skill")
    assert provenance_id
    records = engine.provenance_for("skill_draft", "report-writer")
    assert len(records) == 1
    assert records[0]["source_run_id"] == "run-skill"


async def test_executor_records_trajectory(tmp_path: Path) -> None:
    from eurus.automation.executor import AgentJobExecutor, ExecutionContext
    from eurus.storage.jobs import Job
    from eurus.trajectory import TrajectoryStore

    trajectory = TrajectoryStore(tmp_path / "trajectory.db")

    class FakeAgent:
        def __init__(self) -> None:
            self.registry = None

            class Boundary:
                registry = None

            self.boundary = Boundary()

    class FakeResources:
        def __init__(self, engine: Any) -> None:
            self.agent = FakeAgent()
            self.engine = engine

        async def cleanup(self) -> None:
            return None

    class FailingEngine:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    class SuccessEngine:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            result = type("Result", (), {})()
            result.run = type("RunView", (), {})()
            result.run.status = RunStatus.COMPLETED
            result.run.stop_reason = None
            result.output = "  finished output  "
            return result

    async def factory_success() -> Any:
        return FakeResources(SuccessEngine())

    config = type("Config", (), {})()
    executor = AgentJobExecutor(
        config,
        resource_factory=factory_success,
        trajectory_store=trajectory,
    )
    job = Job(
        job_id="job-traj-1",
        owner_id="owner",
        objective="Trajectory job",
        status="queued",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=0,
        max_attempts=1,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=0,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at="",
        updated_at="",
        source="manual",
    )
    context = ExecutionContext.from_job(
        job, budget=__import__("eurus.runtime.budgets", fromlist=["BudgetSpec"]).BudgetSpec()
    )
    output = await executor.execute(job, context)
    assert output == "finished output"

    async def factory_failure() -> Any:
        return FakeResources(FailingEngine())

    failing = AgentJobExecutor(
        config,
        resource_factory=factory_failure,
        trajectory_store=trajectory,
    )
    failed_job = Job(
        job_id="job-traj-2",
        owner_id="owner",
        objective="Failing job",
        status="queued",
        schedule_id=None,
        scheduled_at=None,
        payload={},
        attempts=0,
        max_attempts=1,
        available_at="",
        worker_id=None,
        lease_expires_at=None,
        lease_version=0,
        side_effect_started=False,
        cancel_requested=False,
        result="",
        error=None,
        created_at="",
        updated_at="",
        source="manual",
    )
    with pytest.raises(RuntimeError):
        await failing.execute(failed_job, context)

    events = trajectory.query(task_id="job-traj-1", limit=10)
    types = [entry["event_type"] for entry in events]
    assert "job_started" in types
    assert "job_finished" in types
    failed_events = trajectory.query(task_id="job-traj-2", limit=10)
    failed_types = [entry["event_type"] for entry in failed_events]
    assert "job_started" in failed_types
    assert "job_failed" in failed_types
