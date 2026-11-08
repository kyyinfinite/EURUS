from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eurus.agent.acceptance import ReviewStore, VerificationStore
from eurus.agent.mission_runtime import deadline_from_budget
from eurus.agent.supervisor import MissionStore
from eurus.learning.evaluation import (
    LEARNING_MARKER_DIMENSION,
    EvaluationEngine,
    LessonCandidate,
    LessonValidator,
)
from eurus.learning.lessons import LessonStore
from eurus.multiagent.contracts import (
    TERMINAL_MISSION_STATUSES,
    Mission,
    TaskSpec,
    VerificationEvidence,
)
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.owner import OWNER_ID
from eurus.storage.database import Database, DatabasePath
from eurus.trajectory import redact

log = logging.getLogger(__name__)

MISSION_SUBJECT_TYPE = "mission"
TERMINAL_STATUSES = tuple(sorted(TERMINAL_MISSION_STATUSES))
DEFAULT_BACKFILL_LIMIT = 25
MAX_BACKFILL_LIMIT = 200
DEFAULT_ANALYTICS_LIMIT = 100
MAX_ANALYTICS_LIMIT = 200
MIN_LESSON_CANDIDATES = 1
MAX_LESSON_CANDIDATES = 10
EXHAUSTED_ATTEMPT_FLOOR = 3
REVISION_PRESSURE_THRESHOLD = 0.34
OBJECTIVE_HEAD_CHARS = 40
OBJECTIVE_VIEW_CHARS = 200
RECENT_LESSONS_LIMIT = 10
MISSION_LESSON_QUERY_LIMIT = 50
ROLE_EVIDENCE_LIMIT = 8
ROLE_STATS_KEYS = ("tasks", "succeeded", "failed", "blocked", "revisions")
BLOCKING_SEVERITIES = frozenset({"blocker", "critical", "major"})
EVALUATION_VIEW_LIMIT = 50


@dataclass(slots=True)
class MissionLearningReport:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    lessons_accepted: int = 0
    evaluations_recorded: int = 0
    notes: list[str] = field(default_factory=list)

    def merge(self, other: MissionLearningReport) -> None:
        self.processed += other.processed
        self.skipped += other.skipped
        self.failed += other.failed
        self.lessons_accepted += other.lessons_accepted
        self.evaluations_recorded += other.evaluations_recorded
        self.notes.extend(other.notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "lessons_accepted": self.lessons_accepted,
            "evaluations_recorded": self.evaluations_recorded,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class _TaskStats:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    revisions: int = 0
    exhausted: int = 0
    covered: int = 0
    blocking_findings: int = 0
    failed_checks: set[str] = field(default_factory=set)
    unavailable_checks: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _RoleStats:
    role: str
    tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    revisions: int = 0


def _owner(owner_id: str | None) -> str:
    resolved = (owner_id or OWNER_ID).strip()
    return resolved or OWNER_ID


def _now() -> datetime:
    return datetime.now(UTC)


def _head(objective: str) -> str:
    return " ".join(objective.split())[:OBJECTIVE_HEAD_CHARS]


def _task_stats(
    tasks: Sequence[TaskSpec],
    verifications: Sequence[VerificationEvidence],
    blocking_findings: int,
) -> _TaskStats:
    stats = _TaskStats(blocking_findings=blocking_findings)
    stats.total = len(tasks)
    covered: set[str] = set()
    for task in tasks:
        if task.status == "succeeded":
            stats.succeeded += 1
        elif task.status == "failed":
            stats.failed += 1
        elif task.status == "blocked":
            stats.blocked += 1
        if task.parent_task_id is not None:
            stats.revisions += 1
        if task.status == "failed" and task.attempt >= EXHAUSTED_ATTEMPT_FLOOR:
            stats.exhausted += 1
    for verification in verifications:
        if verification.unavailable:
            stats.unavailable_checks.add(verification.check_name)
            continue
        covered.add(verification.task_id)
        if verification.failed:
            stats.failed_checks.add(verification.check_name)
    stats.covered = len(covered)
    return stats


def _role_stats(tasks: Sequence[TaskSpec]) -> list[_RoleStats]:
    buckets: dict[str, _RoleStats] = {}
    for task in tasks:
        role = task.preferred_role or "unassigned"
        entry = buckets.setdefault(role, _RoleStats(role=role))
        entry.tasks += 1
        if task.status == "succeeded":
            entry.succeeded += 1
        elif task.status == "failed":
            entry.failed += 1
        elif task.status == "blocked":
            entry.blocked += 1
        if task.parent_task_id is not None:
            entry.revisions += 1
    return sorted(buckets.values(), key=lambda item: (-item.tasks, item.role))


def _parse_role_line(line: str) -> tuple[str, dict[str, int]] | None:
    if not line.startswith("role="):
        return None
    role_segment, separator, rest = line[len("role=") :].partition(":")
    if not separator:
        return None
    stats: dict[str, int] = {key: 0 for key in ROLE_STATS_KEYS}
    for chunk in rest.split(";"):
        key, split, value = chunk.partition("=")
        if not split or key not in stats:
            continue
        if value.isdigit():
            stats[key] = int(value)
    return role_segment, stats


def _lesson_candidates(
    mission: Mission, stats: _TaskStats, *, deadline_violated: bool, coverage: float
) -> list[str]:
    head = _head(mission.objective)
    candidates: list[str] = []
    if stats.exhausted:
        candidates.append(
            f"Mission '{head}' stalled: {stats.exhausted} task(s) exhausted dispatch attempts; "
            "review scope and role fit before retrying"
        )
    if deadline_violated:
        candidates.append(
            f"Mission '{head}' missed its budget deadline and was cancelled; "
            "set deadlines with headroom for review cycles"
        )
    if mission.status == "completed_unverified" or (stats.total and coverage < 1.0):
        candidates.append(
            f"Mission '{head}' finished without complete verification evidence; "
            "require machine checks on every task"
        )
    if stats.failed_checks:
        shown = ", ".join(sorted(stats.failed_checks)[:3])
        candidates.append(
            f"Mission '{head}' failed verification checks ({shown}); "
            "fix failures before finalization"
        )
    if stats.blocking_findings:
        candidates.append(
            f"Mission '{head}' closed with {stats.blocking_findings} open blocking "
            "review finding(s); tighten review criteria"
        )
    if stats.total and stats.revisions / stats.total > REVISION_PRESSURE_THRESHOLD:
        candidates.append(
            f"Mission '{head}' drove {stats.revisions} revision task(s); "
            "sharpen task acceptance criteria in plans"
        )
    if stats.blocked:
        candidates.append(
            f"Mission '{head}' ended blocked with {stats.blocked} task(s) blocked; "
            "check dependency ordering in plans"
        )
    return [redact(" ".join(text.split()))[:200] for text in candidates]


class MissionLearning:
    def __init__(
        self,
        database: Database | DatabasePath,
        *,
        lessons: LessonStore | None = None,
        evaluation: EvaluationEngine | None = None,
        lesson_candidates_limit: int = 3,
        backfill_limit: int = DEFAULT_BACKFILL_LIMIT,
    ) -> None:
        if not MIN_LESSON_CANDIDATES <= lesson_candidates_limit <= MAX_LESSON_CANDIDATES:
            raise ValueError("lesson_candidates_limit must be between 1 and 10")
        if not 1 <= backfill_limit <= MAX_BACKFILL_LIMIT:
            raise ValueError("backfill_limit must be between 1 and 200")
        self.database = database if isinstance(database, Database) else Database(database)
        self.lessons = lessons
        self.evaluation = evaluation or EvaluationEngine(self.database)
        self.lesson_candidates_limit = lesson_candidates_limit
        self.backfill_limit = backfill_limit
        self.missions = MissionStore(self.database)
        self.tasks = TaskGraphStore(self.database)
        self.verifications = VerificationStore(self.database)
        self.reviews = ReviewStore(self.database)

    def has_learned(self, mission_id: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM eurus_evaluations WHERE subject_type=? AND subject_id=? "
                "AND dimension=? LIMIT 1",
                (MISSION_SUBJECT_TYPE, mission_id, LEARNING_MARKER_DIMENSION),
            ).fetchone()
        return row is not None

    def record(self, mission_id: str, *, owner_id: str | None = None) -> MissionLearningReport:
        owner = _owner(owner_id)
        report = MissionLearningReport()
        mission = self.missions.get(mission_id, owner_id=owner)
        if mission is None:
            report.skipped += 1
            report.notes.append(f"mission {mission_id[:8]} not found for owner")
            return report
        if mission.status not in TERMINAL_MISSION_STATUSES:
            report.skipped += 1
            report.notes.append(
                f"mission {mission_id[:8]} status {mission.status!r} is not terminal"
            )
            return report
        if self.has_learned(mission_id):
            report.skipped += 1
            report.notes.append(f"mission {mission_id[:8]} already learned")
            return report
        try:
            outcome = self._learn(mission, owner_id=owner)
        except Exception as exc:
            log.exception("mission learning failed for %s", mission_id)
            report.failed += 1
            report.notes.append(
                f"mission {mission_id[:8]} learning failed: {type(exc).__name__}: {exc}"[:400]
            )
            return report
        if outcome.skipped:
            report.merge(outcome)
            return report
        outcome.processed = 1
        report.merge(outcome)
        return report

    def backfill(
        self, *, owner_id: str | None = None, limit: int | None = None
    ) -> MissionLearningReport:
        owner = _owner(owner_id)
        report = MissionLearningReport()
        bounded = self.backfill_limit if limit is None else max(1, min(limit, MAX_BACKFILL_LIMIT))
        for mission in self._unlearned(owner, bounded):
            single = self.record(mission.mission_id, owner_id=owner)
            report.merge(single)
        return report

    def _unlearned(self, owner: str, limit: int) -> list[Mission]:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT m.mission_id FROM eurus_agent_missions m
                WHERE m.owner_id=? AND m.status IN ({placeholders}) AND NOT EXISTS (
                    SELECT 1 FROM eurus_evaluations e
                    WHERE e.subject_type=? AND e.subject_id=m.mission_id
                    AND e.dimension=?
                ) ORDER BY m.updated_at DESC LIMIT ?""",
                (owner, *TERMINAL_STATUSES, MISSION_SUBJECT_TYPE, LEARNING_MARKER_DIMENSION, limit),
            ).fetchall()
        missions = []
        for row in rows:
            mission = self.missions.get(str(row["mission_id"]), owner_id=owner)
            if mission is not None:
                missions.append(mission)
        return missions

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]:
        owner = _owner(owner_id)
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self.database.connection() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM eurus_agent_missions WHERE owner_id=? "
                f"AND status IN ({placeholders})",
                (owner, *TERMINAL_STATUSES),
            ).fetchone()
            learned_row = connection.execute(
                f"""SELECT COUNT(*) FROM eurus_agent_missions m WHERE m.owner_id=?
                AND m.status IN ({placeholders}) AND EXISTS (
                    SELECT 1 FROM eurus_evaluations e
                    WHERE e.subject_type=? AND e.subject_id=m.mission_id
                    AND e.dimension=?
                )""",
                (owner, *TERMINAL_STATUSES, MISSION_SUBJECT_TYPE, LEARNING_MARKER_DIMENSION),
            ).fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        learned = int(learned_row[0]) if learned_row is not None else 0
        return {
            "terminal": total,
            "learned": learned,
            "pending": max(0, total - learned),
        }

    def learning_view(
        self, mission_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        owner = _owner(owner_id)
        mission = self.missions.get(mission_id, owner_id=owner)
        if mission is None:
            return None
        evaluations = self.evaluation.list_evaluations(
            subject_type=MISSION_SUBJECT_TYPE, subject_id=mission_id, limit=EVALUATION_VIEW_LIMIT
        )
        return {
            "mission_id": mission_id,
            "status": mission.status,
            "objective": " ".join(mission.objective.split())[:OBJECTIVE_VIEW_CHARS],
            "learned": self.has_learned(mission_id),
            "evaluations": [
                {
                    "dimension": item.dimension,
                    "score": item.score,
                    "passed": item.passed,
                    "evidence": list(item.evidence),
                    "created_at": item.created_at.isoformat(),
                }
                for item in evaluations
            ],
            "lessons": self._mission_lessons(mission_id),
        }

    def analytics(self, *, owner_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
        owner = _owner(owner_id)
        bounded = (
            DEFAULT_ANALYTICS_LIMIT if limit is None else max(1, min(limit, MAX_ANALYTICS_LIMIT))
        )
        missions = self.missions.list_missions(
            owner_id=owner, statuses=TERMINAL_STATUSES, limit=bounded
        )
        decisions: dict[str, int] = {status: 0 for status in TERMINAL_STATUSES}
        for mission in missions:
            decisions[mission.status] = decisions.get(mission.status, 0) + 1
        learned = [mission for mission in missions if self.has_learned(mission.mission_id)]
        rows = self._evaluation_rows([mission.mission_id for mission in learned])
        sums: dict[str, list[float]] = {}
        expired = 0
        roles: dict[str, dict[str, int]] = {}
        for row in rows:
            dimension = str(row["dimension"])
            sums.setdefault(dimension, []).append(float(row["score"]))
            evidence = _evidence_lines(str(row["evidence_json"]))
            if dimension == "resource_efficiency" and any(
                str(line) == "expired=yes" for line in evidence
            ):
                expired += 1
            if dimension == "agent_performance":
                for line in evidence:
                    parsed = _parse_role_line(str(line))
                    if parsed is None:
                        continue
                    role, values = parsed
                    bucket = roles.setdefault(role, {key: 0 for key in ROLE_STATS_KEYS})
                    for key in ROLE_STATS_KEYS:
                        bucket[key] += values[key]
        means = {
            dimension: round(sum(values) / len(values), 3) for dimension, values in sums.items()
        }
        return {
            "missions_total": len(missions),
            "missions_learned": len(learned),
            "decisions": decisions,
            "expired": expired,
            "dimension_means": means,
            "roles": [
                {"role": role, **stats}
                for role, stats in sorted(
                    roles.items(), key=lambda item: (-item[1]["tasks"], item[0])
                )
            ],
            "lessons": self._recent_lessons([mission.mission_id for mission in learned]),
        }

    def _learn(self, mission: Mission, *, owner_id: str) -> MissionLearningReport:
        tasks = self.tasks.list_tasks(mission.mission_id, owner_id=owner_id)
        verifications = self.verifications.list_for_mission(mission.mission_id, owner_id=owner_id)
        blocking_findings = 0
        for task in tasks:
            for review in self.reviews.list_for_task(
                mission.mission_id, task.task_id, owner_id=owner_id
            ):
                blocking_findings += sum(
                    1
                    for finding in review.findings
                    if finding.severity in BLOCKING_SEVERITIES and finding.disposition == "open"
                )
        stats = _task_stats(tasks, verifications, blocking_findings)
        roles = _role_stats(tasks)
        coverage = (stats.covered / stats.total) if stats.total else 0.0
        deadline = deadline_from_budget(mission.budget)
        completed_at = mission.completed_at
        deadline_violated = (
            mission.status == "cancelled"
            and deadline is not None
            and completed_at is not None
            and completed_at > deadline
        )
        outcome = MissionLearningReport()
        plan_score = round(max(0.0, 1.0 - 0.15 * stats.blocked - 0.10 * stats.revisions), 3)
        plan_passed = stats.blocked == 0 and stats.revisions == 0
        plan_evidence = [
            f"tasks={stats.total}",
            f"blocked={stats.blocked}",
            f"revisions={stats.revisions}",
            f"open_blocking_findings={stats.blocking_findings}",
            f"decision={mission.status}",
        ]
        if not self._claim_marker(
            mission.mission_id, score=plan_score, passed=plan_passed, evidence=plan_evidence
        ):
            outcome.skipped = 1
            outcome.notes.append(f"mission {mission.mission_id[:8]} learned concurrently")
            return outcome
        outcome.evaluations_recorded += 1
        self.evaluation.record(
            subject_type=MISSION_SUBJECT_TYPE,
            subject_id=mission.mission_id,
            dimension="task_correctness",
            score=round((stats.succeeded / stats.total) if stats.total else 0.0, 3),
            passed=stats.total > 0 and stats.succeeded == stats.total,
            evidence=[
                f"succeeded={stats.succeeded}/{stats.total}",
                f"failed={stats.failed}",
                f"blocked={stats.blocked}",
                f"decision={mission.status}",
            ],
        )
        outcome.evaluations_recorded += 1
        self.evaluation.record(
            subject_type=MISSION_SUBJECT_TYPE,
            subject_id=mission.mission_id,
            dimension="verification_coverage",
            score=round(coverage, 3),
            passed=stats.total > 0 and coverage >= 1.0 and not stats.failed_checks,
            evidence=[
                f"verifications={len(verifications)}",
                f"covered_tasks={stats.covered}/{stats.total}",
                f"failed_checks={','.join(sorted(stats.failed_checks)) or 'none'}",
                f"unavailable_checks={','.join(sorted(stats.unavailable_checks)) or 'none'}",
            ],
        )
        outcome.evaluations_recorded += 1
        efficiency, efficiency_passed = self._efficiency(
            mission, deadline=deadline, deadline_violated=deadline_violated
        )
        self.evaluation.record(
            subject_type=MISSION_SUBJECT_TYPE,
            subject_id=mission.mission_id,
            dimension="resource_efficiency",
            score=efficiency,
            passed=efficiency_passed,
            evidence=[
                f"status={mission.status}",
                f"deadline={deadline.isoformat() if deadline else 'none'}",
                f"completed_at={completed_at.isoformat() if completed_at else 'none'}",
                f"expired={'yes' if deadline_violated else 'no'}",
            ],
        )
        outcome.evaluations_recorded += 1
        revision_pressure = (stats.revisions / stats.total) if stats.total else 0.0
        performance_evidence = [
            f"exhausted={stats.exhausted}",
            f"revision_pressure={round(revision_pressure, 3)}",
        ]
        for entry in roles[:ROLE_EVIDENCE_LIMIT]:
            performance_evidence.append(
                f"role={entry.role}:tasks={entry.tasks};succeeded={entry.succeeded};"
                f"failed={entry.failed};blocked={entry.blocked};revisions={entry.revisions}"
            )
        self.evaluation.record(
            subject_type=MISSION_SUBJECT_TYPE,
            subject_id=mission.mission_id,
            dimension="agent_performance",
            score=round(max(0.0, 1.0 - 0.2 * stats.exhausted - 0.15 * stats.revisions), 3),
            passed=stats.exhausted == 0 and revision_pressure <= REVISION_PRESSURE_THRESHOLD,
            evidence=performance_evidence,
        )
        outcome.evaluations_recorded += 1
        if self.lessons is None:
            return outcome
        validator = LessonValidator(self.lessons)
        candidates = _lesson_candidates(
            mission, stats, deadline_violated=deadline_violated, coverage=coverage
        )
        for text in candidates[: self.lesson_candidates_limit]:
            candidate = LessonCandidate(text=text)
            accepted, reason = validator.review(candidate, owner_id=owner_id)
            if not accepted:
                log.debug("mission lesson rejected: %s", reason)
                continue
            lesson_id = self.lessons.add(candidate.text, owner_id=owner_id)
            if lesson_id is None:
                continue
            self.evaluation.record_artifact_provenance(
                artifact_type="lesson",
                artifact_id=str(lesson_id),
                summary=candidate.text[:2000],
                source_run_id=mission.mission_id,
            )
            outcome.lessons_accepted += 1
        return outcome

    def _claim_marker(
        self, mission_id: str, *, score: float, passed: bool, evidence: Sequence[str]
    ) -> bool:
        evaluation_id = uuid.uuid4().hex
        provenance_id = uuid.uuid4().hex
        current = _now().isoformat()
        payload = json.dumps([str(item)[:600] for item in evidence[:20]])
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO eurus_evaluations("
                "evaluation_id, subject_type, subject_id, dimension, score, passed, "
                "evidence_json, evaluator, created_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, 'deterministic', ? WHERE NOT EXISTS ("
                "SELECT 1 FROM eurus_evaluations WHERE subject_type=? AND subject_id=? "
                "AND dimension=?)",
                (
                    evaluation_id,
                    MISSION_SUBJECT_TYPE,
                    mission_id,
                    LEARNING_MARKER_DIMENSION,
                    score,
                    int(passed),
                    payload,
                    current,
                    MISSION_SUBJECT_TYPE,
                    mission_id,
                    LEARNING_MARKER_DIMENSION,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO eurus_learning_provenance("
                "provenance_id, artifact_type, artifact_id, source_run_id, evaluation_id, "
                "summary, created_at) VALUES (?, 'evaluation', ?, ?, ?, ?, ?)",
                (
                    provenance_id,
                    evaluation_id,
                    None,
                    evaluation_id,
                    f"{LEARNING_MARKER_DIMENSION}={score:.2f} passed={passed}",
                    current,
                ),
            )
        return True

    def _efficiency(
        self, mission: Mission, *, deadline: Any, deadline_violated: bool
    ) -> tuple[float, bool]:
        completed_at = mission.completed_at
        if mission.status in ("completed", "completed_unverified"):
            if deadline is None or completed_at is None:
                return 1.0, True
            if completed_at <= deadline:
                return 1.0, True
            return 0.3, False
        if mission.status == "blocked":
            return 0.3, False
        if mission.status == "failed":
            return 0.2, False
        if deadline_violated:
            return 0.0, False
        return 0.4, False

    def _mission_lessons(self, mission_id: str) -> list[dict[str, str]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT artifact_id, summary FROM eurus_learning_provenance "
                "WHERE artifact_type='lesson' AND source_run_id=? AND reversed_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (mission_id, MISSION_LESSON_QUERY_LIMIT),
            ).fetchall()
        return [{"lesson_id": str(row["artifact_id"]), "text": str(row["summary"])} for row in rows]

    def _recent_lessons(self, mission_ids: list[str]) -> list[dict[str, str]]:
        if not mission_ids:
            return []
        placeholders = ",".join("?" for _ in mission_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT artifact_id, summary, source_run_id FROM eurus_learning_provenance "
                f"WHERE artifact_type='lesson' AND source_run_id IN ({placeholders}) "
                "AND reversed_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (*mission_ids, RECENT_LESSONS_LIMIT),
            ).fetchall()
        return [
            {
                "lesson_id": str(row["artifact_id"]),
                "text": str(row["summary"]),
                "mission_id": str(row["source_run_id"]),
            }
            for row in rows
        ]

    def _evaluation_rows(self, mission_ids: list[str]) -> list[dict[str, Any]]:
        if not mission_ids:
            return []
        placeholders = ",".join("?" for _ in mission_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT dimension, score, evidence_json FROM eurus_evaluations "
                f"WHERE subject_type=? AND subject_id IN ({placeholders})",
                (MISSION_SUBJECT_TYPE, *mission_ids),
            ).fetchall()
        return [dict(row) for row in rows]


def _evidence_lines(payload: str) -> list[Any]:
    try:
        parsed = json.loads(payload) if payload else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


__all__ = [
    "MissionLearning",
    "MissionLearningReport",
    "MISSION_SUBJECT_TYPE",
    "LEARNING_MARKER_DIMENSION",
]
