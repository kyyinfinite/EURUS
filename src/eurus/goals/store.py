from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from eurus.goals.states import (
    TERMINAL_GOAL_STATUSES,
    GoalStatus,
    GoalTaskStatus,
    InvalidGoalTransition,
    MilestoneStatus,
    can_transition,
)
from eurus.storage.database import Database, DatabasePath

GOAL_SOURCES = frozenset({"user", "agent", "automation", "event"})


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
class Goal:
    goal_id: str
    owner_id: str
    objective: str
    status: GoalStatus
    status_version: int
    created_at: datetime
    updated_at: datetime
    description: str = ""
    constraints: tuple[str, ...] = ()
    priority: int = 5
    deadline_at: datetime | None = None
    progress: float = 0.0
    success_criteria: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)
    parent_goal_id: str | None = None
    source: str = "user"
    completion_evidence: list[str] = field(default_factory=list)
    completed_at: datetime | None = None
    replan_count: int = 0

    def validate(self) -> None:
        if not self.owner_id.strip() or not self.objective.strip():
            raise ValueError("owner and objective are required")
        if not 1 <= self.priority <= 9:
            raise ValueError("priority must be between 1 and 9")
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        if self.source not in GOAL_SOURCES:
            raise ValueError(f"unknown goal source: {self.source}")


@dataclass(slots=True)
class GoalTask:
    task_id: str
    goal_id: str
    title: str
    status: GoalTaskStatus
    created_at: datetime
    updated_at: datetime
    detail: str = ""
    depends_on: tuple[str, ...] = ()
    success_criteria: str = ""
    milestone_id: str | None = None
    schedule_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    output: str = ""
    error: str | None = None
    attempts: int = 0


@dataclass(slots=True)
class Milestone:
    milestone_id: str
    goal_id: str
    title: str
    status: MilestoneStatus
    created_at: datetime
    updated_at: datetime
    criteria: str = ""
    target_progress: float = 1.0


class GoalStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def create(
        self,
        owner_id: str,
        objective: str,
        *,
        description: str = "",
        constraints: Sequence[str] = (),
        priority: int = 5,
        deadline_at: datetime | None = None,
        success_criteria: Sequence[str] = (),
        parent_goal_id: str | None = None,
        source: str = "user",
    ) -> Goal:
        current = _now()
        goal = Goal(
            goal_id=uuid.uuid4().hex,
            owner_id=owner_id,
            objective=objective,
            status=GoalStatus.CREATED,
            status_version=0,
            created_at=current,
            updated_at=current,
            description=description,
            constraints=tuple(constraints),
            priority=priority,
            deadline_at=deadline_at,
            success_criteria=tuple(success_criteria),
            parent_goal_id=parent_goal_id,
            source=source,
        )
        goal.validate()
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_goals(
                    goal_id, owner_id, objective, description, constraints_json, priority,
                    deadline_at, status, status_version, progress, success_criteria_json,
                    plan_json, parent_goal_id, source, completion_evidence_json,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    goal.goal_id,
                    goal.owner_id,
                    goal.objective,
                    goal.description,
                    json.dumps(list(goal.constraints)),
                    goal.priority,
                    _time(goal.deadline_at) if goal.deadline_at else None,
                    goal.status.value,
                    goal.status_version,
                    goal.progress,
                    json.dumps(list(goal.success_criteria)),
                    json.dumps(goal.plan),
                    goal.parent_goal_id,
                    goal.source,
                    json.dumps([]),
                    _time(goal.created_at),
                    _time(goal.updated_at),
                    None,
                ),
            )
        return goal

    def get(self, goal_id: str, *, owner_id: str | None = None) -> Goal | None:
        with self.database.connection() as connection:
            if owner_id is None:
                row = connection.execute(
                    "SELECT * FROM eurus_goals WHERE goal_id=?", (goal_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM eurus_goals WHERE goal_id=? AND owner_id=?",
                    (goal_id, owner_id),
                ).fetchone()
        return self._goal(row) if row is not None else None

    def list_goals(
        self,
        *,
        owner_id: str | None = None,
        statuses: Sequence[GoalStatus] = (),
        limit: int = 100,
    ) -> list[Goal]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        values: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(status.value for status in statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_goals{where} ORDER BY priority, created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._goal(row) for row in rows]

    def save(self, goal: Goal, *, expected_status_version: int | None = None) -> Goal:
        goal.validate()
        expected = (
            expected_status_version if expected_status_version is not None else goal.status_version
        )
        with self.database.connection() as connection:
            cursor = connection.execute(
                """UPDATE eurus_goals SET objective=?, description=?, constraints_json=?,
                priority=?, deadline_at=?, status=?, status_version=status_version+1, progress=?,
                success_criteria_json=?, plan_json=?, completion_evidence_json=?, updated_at=?,
                completed_at=? WHERE goal_id=? AND status_version=?""",
                (
                    goal.objective,
                    goal.description,
                    json.dumps(list(goal.constraints)),
                    goal.priority,
                    _time(goal.deadline_at) if goal.deadline_at else None,
                    goal.status.value,
                    goal.progress,
                    json.dumps(list(goal.success_criteria)),
                    json.dumps(goal.plan),
                    json.dumps(list(goal.completion_evidence)),
                    _time(_now()),
                    _time(goal.completed_at) if goal.completed_at else None,
                    goal.goal_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(goal.goal_id)
        return self.get(goal.goal_id) or goal

    def transition(
        self,
        goal_id: str,
        target: GoalStatus,
        *,
        note: str = "",
        expected_status_version: int | None = None,
        owner_id: str | None = None,
    ) -> Goal:
        current = self.get(goal_id, owner_id=owner_id)
        if current is None:
            raise KeyError("goal not found")
        if not can_transition(current.status, target):
            raise InvalidGoalTransition(
                f"cannot transition goal from {current.status.value} to {target.value}"
            )
        updated = replace(
            current,
            status=target,
            updated_at=_now(),
            completed_at=_now() if target in TERMINAL_GOAL_STATUSES else current.completed_at,
        )
        if note:
            updated.completion_evidence = [*updated.completion_evidence, note][:50]
        return self.save(updated, expected_status_version=expected_status_version)

    def set_replan_count(self, goal_id: str, count: int, *, owner_id: str | None = None) -> None:
        plan_key = "replan_count"
        goal = self.get(goal_id, owner_id=owner_id)
        if goal is None:
            raise KeyError("goal not found")
        plan = dict(goal.plan)
        plan[plan_key] = count
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET plan_json=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (
                    json.dumps(plan),
                    _time(_now()),
                    goal_id,
                    goal.status_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )

    def add_task(
        self,
        goal_id: str,
        title: str,
        *,
        detail: str = "",
        depends_on: Sequence[str] = (),
        success_criteria: str = "",
        milestone_id: str | None = None,
        schedule_id: str | None = None,
    ) -> GoalTask:
        if not title.strip():
            raise ValueError("task title is required")
        current = _now()
        task = GoalTask(
            task_id=uuid.uuid4().hex,
            goal_id=goal_id,
            title=title.strip(),
            status=GoalTaskStatus.PENDING,
            created_at=current,
            updated_at=current,
            detail=detail,
            depends_on=tuple(depends_on),
            success_criteria=success_criteria,
            milestone_id=milestone_id,
            schedule_id=schedule_id,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_goal_tasks(
                    task_id, goal_id, title, detail, status, depends_on_json,
                    success_criteria, milestone_id, schedule_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.goal_id,
                    task.title,
                    task.detail,
                    task.status.value,
                    json.dumps(list(task.depends_on)),
                    task.success_criteria,
                    task.milestone_id,
                    task.schedule_id,
                    _time(task.created_at),
                    _time(task.updated_at),
                ),
            )
        return task

    def get_task(self, task_id: str) -> GoalTask | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_goal_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._task(row) if row is not None else None

    def task_for_job(self, job_id: str) -> GoalTask | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_goal_tasks WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._task(row) if row is not None else None

    def list_tasks(self, goal_id: str) -> list[GoalTask]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_goal_tasks WHERE goal_id=? ORDER BY created_at",
                (goal_id,),
            ).fetchall()
        return [self._task(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        *,
        status: GoalTaskStatus | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> GoalTask | None:
        current = self.get_task(task_id)
        if current is None:
            return None
        updated = replace(current, updated_at=_now())
        if status is not None:
            updated.status = status
        if job_id is not None:
            updated.job_id = job_id
        if run_id is not None:
            updated.run_id = run_id
        if output is not None:
            updated.output = output
        if error is not None:
            updated.error = error
        if increment_attempts:
            updated.attempts = current.attempts + 1
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE eurus_goal_tasks SET status=?, job_id=?, run_id=?, output=?,
                error=?, attempts=?, updated_at=? WHERE task_id=?""",
                (
                    updated.status.value,
                    updated.job_id,
                    updated.run_id,
                    updated.output,
                    updated.error,
                    updated.attempts,
                    _time(updated.updated_at),
                    task_id,
                ),
            )
        return updated

    def update_task_dependencies(self, task_id: str, depends_on: Sequence[str]) -> GoalTask | None:
        current = self.get_task(task_id)
        if current is None:
            return None
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_goal_tasks SET depends_on_json=?, updated_at=? WHERE task_id=?",
                (json.dumps(list(depends_on)), _time(_now()), task_id),
            )
        return self.get_task(task_id)

    def add_milestone(
        self,
        goal_id: str,
        title: str,
        *,
        criteria: str = "",
        target_progress: float = 1.0,
    ) -> Milestone:
        if not title.strip():
            raise ValueError("milestone title is required")
        if not 0.0 < target_progress <= 1.0:
            raise ValueError("target_progress must be between 0 and 1")
        current = _now()
        milestone = Milestone(
            milestone_id=uuid.uuid4().hex,
            goal_id=goal_id,
            title=title.strip(),
            status=MilestoneStatus.PENDING,
            created_at=current,
            updated_at=current,
            criteria=criteria,
            target_progress=target_progress,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_goal_milestones(
                    milestone_id, goal_id, title, criteria, status, target_progress,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    milestone.milestone_id,
                    milestone.goal_id,
                    milestone.title,
                    milestone.criteria,
                    milestone.status.value,
                    milestone.target_progress,
                    _time(milestone.created_at),
                    _time(milestone.updated_at),
                ),
            )
        return milestone

    def list_milestones(self, goal_id: str) -> list[Milestone]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_goal_milestones WHERE goal_id=? ORDER BY created_at",
                (goal_id,),
            ).fetchall()
        return [self._milestone(row) for row in rows]

    def update_milestone(self, milestone_id: str, status: MilestoneStatus) -> Milestone | None:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goal_milestones SET status=?, updated_at=? WHERE milestone_id=?",
                (status.value, _time(_now()), milestone_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM eurus_goal_milestones WHERE milestone_id=?", (milestone_id,)
            ).fetchone()
        return self._milestone(row) if row is not None else None

    def set_progress(self, goal_id: str, progress: float) -> None:
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        goal = self.get(goal_id)
        if goal is None:
            raise KeyError("goal not found")
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET progress=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (progress, _time(_now()), goal_id, goal.status_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )

    def capture_plan(self, goal_id: str, plan: dict[str, Any]) -> None:
        goal = self.get(goal_id)
        if goal is None:
            raise KeyError("goal not found")
        merged = dict(goal.plan)
        merged["pending_plan"] = plan
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET plan_json=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (json.dumps(merged), _time(_now()), goal_id, goal.status_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )

    def take_pending_plan(self, goal_id: str) -> dict[str, Any] | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        plan = dict(goal.plan)
        pending = plan.pop("pending_plan", None)
        if not isinstance(pending, dict):
            return None
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET plan_json=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (json.dumps(plan), _time(_now()), goal_id, goal.status_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )
        return dict(pending)

    def capture_evaluation(self, goal_id: str, evaluation: dict[str, Any]) -> None:
        goal = self.get(goal_id)
        if goal is None:
            raise KeyError("goal not found")
        plan = dict(goal.plan)
        plan["pending_evaluation"] = evaluation
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET plan_json=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (json.dumps(plan), _time(_now()), goal_id, goal.status_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )

    def take_pending_evaluation(self, goal_id: str) -> dict[str, Any] | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        plan = dict(goal.plan)
        pending = plan.pop("pending_evaluation", None)
        if not isinstance(pending, dict):
            return None
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_goals SET plan_json=?, updated_at=?, "
                "status_version=status_version+1 "
                "WHERE goal_id=? AND status_version=?",
                (json.dumps(plan), _time(_now()), goal_id, goal.status_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentGoalUpdate(
                    f"goal {goal_id} changed since it was read; reload and retry"
                )
        return dict(pending)

    def counts_by_status(self) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM eurus_goals GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def _goal(self, row: sqlite3.Row) -> Goal:
        deadline = _parse(row["deadline_at"])
        completed = _parse(row["completed_at"])
        plan = json.loads(row["plan_json"]) if row["plan_json"] else {}
        replan_count = int(plan.get("replan_count", 0)) if isinstance(plan, dict) else 0
        return Goal(
            goal_id=str(row["goal_id"]),
            owner_id=str(row["owner_id"]),
            objective=str(row["objective"]),
            status=GoalStatus(str(row["status"])),
            status_version=int(row["status_version"]),
            created_at=_parse(str(row["created_at"])) or _now(),
            updated_at=_parse(str(row["updated_at"])) or _now(),
            description=str(row["description"] or ""),
            constraints=tuple(json.loads(row["constraints_json"] or "[]")),
            priority=int(row["priority"]),
            deadline_at=deadline,
            progress=float(row["progress"]),
            success_criteria=tuple(json.loads(row["success_criteria_json"] or "[]")),
            plan=plan if isinstance(plan, dict) else {},
            parent_goal_id=row["parent_goal_id"],
            source=str(row["source"] or "user"),
            completion_evidence=list(json.loads(row["completion_evidence_json"] or "[]")),
            completed_at=completed,
            replan_count=replan_count,
        )

    def _task(self, row: sqlite3.Row) -> GoalTask:
        return GoalTask(
            task_id=str(row["task_id"]),
            goal_id=str(row["goal_id"]),
            title=str(row["title"]),
            status=GoalTaskStatus(str(row["status"])),
            created_at=_parse(str(row["created_at"])) or _now(),
            updated_at=_parse(str(row["updated_at"])) or _now(),
            detail=str(row["detail"] or ""),
            depends_on=tuple(json.loads(row["depends_on_json"] or "[]")),
            success_criteria=str(row["success_criteria"] or ""),
            milestone_id=row["milestone_id"],
            schedule_id=row["schedule_id"],
            job_id=row["job_id"],
            run_id=row["run_id"],
            output=str(row["output"] or ""),
            error=row["error"],
            attempts=int(row["attempts"]),
        )

    def _milestone(self, row: sqlite3.Row) -> Milestone:
        return Milestone(
            milestone_id=str(row["milestone_id"]),
            goal_id=str(row["goal_id"]),
            title=str(row["title"]),
            status=MilestoneStatus(str(row["status"])),
            created_at=_parse(str(row["created_at"])) or _now(),
            updated_at=_parse(str(row["updated_at"])) or _now(),
            criteria=str(row["criteria"] or ""),
            target_progress=float(row["target_progress"]),
        )


class ConcurrentGoalUpdate(RuntimeError):
    def __init__(self, goal_id: str) -> None:
        super().__init__(f"goal {goal_id} was modified concurrently")
        self.goal_id = goal_id
