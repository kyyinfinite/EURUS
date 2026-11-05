from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus.learning.lessons import LessonStore
from eurus.learning.reflect import SECRET
from eurus.storage.database import Database, DatabasePath

EVALUATION_DIMENSIONS = frozenset(
    {
        "task_correctness",
        "goal_completion",
        "tool_reliability",
        "plan_quality",
        "workflow_quality",
        "recovery_effectiveness",
        "agent_performance",
        "resource_efficiency",
        "verification_coverage",
    }
)
LEARNING_MARKER_DIMENSION = "plan_quality"
EVALUATORS = frozenset({"deterministic", "agent"})
LESSON_MIN_LENGTH = 15
LESSON_MAX_LENGTH = 200
BUDGET_STOP_MARKERS = ("budget", "iteration", "deadline", "token")


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class Evaluation:
    evaluation_id: str
    subject_type: str
    subject_id: str
    dimension: str
    score: float
    passed: bool
    evidence: tuple[str, ...]
    evaluator: str
    created_at: datetime
    source_run_id: str | None = None
    provenance_id: str | None = None


@dataclass(slots=True)
class LessonCandidate:
    text: str
    source: str = "reflector"


@dataclass(slots=True)
class LessonReview:
    candidate: LessonCandidate
    accepted: bool
    reason: str
    lesson_id: int | None = None
    provenance_id: str | None = None


@dataclass(slots=True)
class LearningReport:
    subject_type: str
    subject_id: str
    evaluations: list[Evaluation] = field(default_factory=list)
    lesson_reviews: list[LessonReview] = field(default_factory=list)

    @property
    def accepted_lessons(self) -> list[str]:
        return [review.candidate.text for review in self.lesson_reviews if review.accepted]


class EvaluationEngine:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def record(
        self,
        *,
        subject_type: str,
        subject_id: str,
        dimension: str,
        score: float,
        passed: bool,
        evidence: Sequence[str] = (),
        evaluator: str = "deterministic",
        source_run_id: str | None = None,
    ) -> Evaluation:
        if dimension not in EVALUATION_DIMENSIONS:
            raise ValueError(f"unknown evaluation dimension: {dimension}")
        if evaluator not in EVALUATORS:
            raise ValueError(f"unknown evaluator: {evaluator}")
        if not 0.0 <= score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        if not subject_type.strip() or not subject_id.strip():
            raise ValueError("subject_type and subject_id are required")
        evaluation_id = uuid.uuid4().hex
        current = _now()
        provenance_id = uuid.uuid4().hex
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_evaluations(
                    evaluation_id, subject_type, subject_id, dimension, score, passed,
                    evidence_json, evaluator, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation_id,
                    subject_type,
                    subject_id,
                    dimension,
                    score,
                    int(passed),
                    json.dumps([str(item)[:600] for item in evidence[:20]]),
                    evaluator,
                    _time(current),
                ),
            )
            connection.execute(
                """INSERT INTO eurus_learning_provenance(
                    provenance_id, artifact_type, artifact_id, source_run_id, evaluation_id,
                    summary, created_at
                ) VALUES (?, 'evaluation', ?, ?, ?, ?, ?)""",
                (
                    provenance_id,
                    evaluation_id,
                    source_run_id,
                    evaluation_id,
                    f"{dimension}={score:.2f} passed={passed}",
                    _time(current),
                ),
            )
        return Evaluation(
            evaluation_id=evaluation_id,
            subject_type=subject_type,
            subject_id=subject_id,
            dimension=dimension,
            score=score,
            passed=passed,
            evidence=tuple(str(item)[:600] for item in evidence[:20]),
            evaluator=evaluator,
            created_at=current,
            source_run_id=source_run_id,
            provenance_id=provenance_id,
        )

    def record_artifact_provenance(
        self,
        *,
        artifact_type: str,
        artifact_id: str,
        summary: str,
        source_run_id: str | None = None,
        evaluation_id: str | None = None,
    ) -> str:
        if artifact_type not in {"lesson", "skill_draft", "skill", "evaluation"}:
            raise ValueError(f"unknown artifact type: {artifact_type}")
        provenance_id = uuid.uuid4().hex
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_learning_provenance(
                    provenance_id, artifact_type, artifact_id, source_run_id, evaluation_id,
                    summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    provenance_id,
                    artifact_type,
                    artifact_id,
                    source_run_id,
                    evaluation_id,
                    summary[:2000],
                    _time(_now()),
                ),
            )
        return provenance_id

    def reverse(self, provenance_id: str) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_learning_provenance SET reversed_at=? "
                "WHERE provenance_id=? AND reversed_at IS NULL",
                (_time(_now()), provenance_id),
            )
            return cursor.rowcount == 1

    def provenance_for(self, artifact_type: str, artifact_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM eurus_learning_provenance
                WHERE artifact_type=? AND artifact_id=? AND reversed_at IS NULL
                ORDER BY created_at DESC""",
                (artifact_type, artifact_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_provenance(self, provenance_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_learning_provenance WHERE provenance_id=?",
                (provenance_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_evaluations(
        self,
        *,
        subject_type: str,
        subject_id: str,
        dimension: str | None = None,
        limit: int = 50,
    ) -> list[Evaluation]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses = ["subject_type=?", "subject_id=?"]
        values: list[Any] = [subject_type, subject_id]
        if dimension is not None:
            clauses.append("dimension=?")
            values.append(dimension)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_evaluations WHERE {' AND '.join(clauses)} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._evaluation(row) for row in rows]

    def evaluate_run(
        self,
        run: Any,
        attempts: Sequence[Mapping[str, Any]],
        *,
        source_run_id: str | None = None,
    ) -> list[Evaluation]:
        evaluations: list[Evaluation] = []
        run_id = str(source_run_id or getattr(run, "run_id", "") or "")
        status = str(getattr(getattr(run, "status", ""), "value", getattr(run, "status", "")))
        stop_reason = str(getattr(run, "stop_reason", "") or "")
        result_ref = getattr(run, "result_ref", None)
        completed = status == "completed"
        correctness = self.record(
            subject_type="run",
            subject_id=run_id,
            dimension="task_correctness",
            score=1.0 if completed and result_ref else (0.5 if completed else 0.0),
            passed=completed,
            evidence=[
                f"status={status}",
                f"stop_reason={stop_reason or 'none'}",
                f"result={'present' if result_ref else 'missing'}",
            ],
            source_run_id=run_id,
        )
        evaluations.append(correctness)
        if attempts:
            errors = sum(1 for attempt in attempts if attempt.get("is_error"))
            ratio = 1.0 - (errors / len(attempts))
            evaluations.append(
                self.record(
                    subject_type="run",
                    subject_id=run_id,
                    dimension="tool_reliability",
                    score=round(ratio, 3),
                    passed=ratio >= 0.5,
                    evidence=[
                        f"attempts={len(attempts)}",
                        f"errors={errors}",
                        *[
                            f"{attempt.get('tool_name')}:{attempt.get('status')}"
                            for attempt in attempts[:10]
                        ],
                    ],
                    source_run_id=run_id,
                )
            )
        efficiency = 1.0
        if stop_reason and any(marker in stop_reason.lower() for marker in BUDGET_STOP_MARKERS):
            efficiency = 0.2
        elif status in ("failed", "cancelled"):
            efficiency = 0.4
        evaluations.append(
            self.record(
                subject_type="run",
                subject_id=run_id,
                dimension="resource_efficiency",
                score=efficiency,
                passed=efficiency >= 0.5,
                evidence=[f"status={status}", f"stop_reason={stop_reason or 'none'}"],
                source_run_id=run_id,
            )
        )
        return evaluations

    def evaluate_goal(
        self,
        *,
        goal_id: str,
        progress: float,
        all_tasks_succeeded: bool,
        evidence: Sequence[str] = (),
    ) -> Evaluation:
        return self.record(
            subject_type="goal",
            subject_id=goal_id,
            dimension="goal_completion",
            score=round(min(1.0, max(0.0, progress)), 3),
            passed=all_tasks_succeeded and progress >= 1.0,
            evidence=evidence,
            source_run_id=None,
        )

    def _evaluation(self, row: sqlite3.Row) -> Evaluation:
        return Evaluation(
            evaluation_id=row["evaluation_id"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            dimension=row["dimension"],
            score=float(row["score"]),
            passed=bool(row["passed"]),
            evidence=tuple(json.loads(row["evidence_json"] or "[]")),
            evaluator=row["evaluator"],
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
        )


class LessonValidator:
    def __init__(self, lessons: LessonStore | None = None) -> None:
        self.lessons = lessons

    def review(
        self, candidate: LessonCandidate, *, owner_id: str | None = None
    ) -> tuple[bool, str]:
        text = candidate.text.strip()
        if not LESSON_MIN_LENGTH <= len(text) <= LESSON_MAX_LENGTH:
            return False, f"length outside {LESSON_MIN_LENGTH}-{LESSON_MAX_LENGTH} characters"
        if "\n" in text:
            return False, "lesson contains line breaks"
        if SECRET.search(text):
            return False, "lesson contains secret-like content"
        if self.lessons is not None and self.lessons.is_duplicate(text, owner_id=owner_id):
            return False, "duplicate of an existing lesson"
        return True, "accepted"


class LearningPipeline:
    def __init__(
        self,
        evaluation: EvaluationEngine,
        *,
        lessons: LessonStore | None = None,
        skills: Any = None,
    ) -> None:
        self.evaluation = evaluation
        self.lessons = lessons
        self.skills = skills
        self.validator = LessonValidator(lessons)

    def process_run(
        self,
        run: Any,
        attempts: Sequence[Mapping[str, Any]],
        *,
        lesson_candidates: Sequence[str] = (),
        owner_id: str | None = None,
    ) -> LearningReport:
        run_id = str(getattr(run, "run_id", "") or "")
        report = LearningReport(subject_type="run", subject_id=run_id)
        report.evaluations = self.evaluation.evaluate_run(run, attempts, source_run_id=run_id)
        for text in lesson_candidates:
            candidate = LessonCandidate(text=text)
            accepted, reason = self.validator.review(candidate, owner_id=owner_id)
            review = LessonReview(candidate=candidate, accepted=accepted, reason=reason)
            if accepted and self.lessons is not None:
                lesson_id = self.lessons.add(candidate.text, owner_id=owner_id)
                if lesson_id is None:
                    review.accepted = False
                    review.reason = "lesson store rejected the entry"
                else:
                    review.lesson_id = lesson_id
                    review.provenance_id = self.evaluation.record_artifact_provenance(
                        artifact_type="lesson",
                        artifact_id=str(lesson_id),
                        summary=candidate.text[:2000],
                        source_run_id=run_id,
                    )
            elif accepted:
                review.provenance_id = self.evaluation.record_artifact_provenance(
                    artifact_type="lesson",
                    artifact_id="unstored",
                    summary=candidate.text[:2000],
                    source_run_id=run_id,
                )
            report.lesson_reviews.append(review)
        return report

    def record_skill_draft(self, draft: Any, *, source_run_id: str | None = None) -> str:
        return self.evaluation.record_artifact_provenance(
            artifact_type="skill_draft",
            artifact_id=str(getattr(draft, "name", "")),
            summary=str(getattr(draft, "description", ""))[:2000],
            source_run_id=source_run_id,
        )


def build_lesson_store(sessions_database: Path, **kwargs: Any) -> LessonStore:
    return LessonStore(sessions_database, **kwargs)
