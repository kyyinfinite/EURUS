from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from eurus.automation.engine import AutomationEngine
from eurus.goals.states import (
    TERMINAL_GOAL_STATUSES,
    GoalStatus,
    GoalTaskStatus,
    MilestoneStatus,
)
from eurus.goals.store import Goal, GoalStore, GoalTask
from eurus.storage.database import Database, DatabasePath

log = logging.getLogger(__name__)

MAX_GOAL_TASKS = 32
MAX_PLAN_TITLES = 32


class GoalEventPublisher(Protocol):
    def __call__(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, Any],
    ) -> None: ...


@dataclass(slots=True)
class GoalTickReport:
    goals_advanced: int = 0
    jobs_submitted: int = 0
    goals_completed: int = 0
    goals_failed: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GoalEvaluation:
    progress: float
    all_tasks_succeeded: bool
    any_task_failed: bool
    open_tasks: int
    evidence: tuple[str, ...]


class GoalEngine:
    def __init__(
        self,
        database: Database | DatabasePath,
        automation: AutomationEngine,
        *,
        event_publisher: GoalEventPublisher | None = None,
        max_replans: int = 2,
        max_active_goals: int = 16,
        max_concurrent_task_jobs: int = 4,
        evaluation_mode: str = "deterministic",
    ) -> None:
        if not 0 <= max_replans <= 10:
            raise ValueError("max_replans must be between 0 and 10")
        if not 1 <= max_active_goals <= 256:
            raise ValueError("max_active_goals must be between 1 and 256")
        if evaluation_mode not in {"deterministic", "agent"}:
            raise ValueError("evaluation_mode must be deterministic or agent")
        self.store = GoalStore(database)
        self.automation = automation
        self.event_publisher = event_publisher
        self.max_replans = max_replans
        self.max_active_goals = max_active_goals
        self.max_concurrent_task_jobs = max_concurrent_task_jobs
        self.evaluation_mode = evaluation_mode
        automation.on_job_finished.append(self._on_job_finished)

    def create_goal(
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
        active_statuses = (
            GoalStatus.CREATED,
            GoalStatus.ANALYZING,
            GoalStatus.PLANNING,
            GoalStatus.REPLANNING,
            GoalStatus.ACTIVE,
            GoalStatus.EXECUTING,
            GoalStatus.EVALUATING,
        )
        active = self.store.list_goals(owner_id=owner_id, statuses=active_statuses, limit=500)
        if len(active) >= self.max_active_goals:
            raise ValueError(
                f"owner {owner_id!r} already has {len(active)} active goals "
                f"(limit {self.max_active_goals}); finish or cancel one first"
            )
        goal = self.store.create(
            owner_id,
            objective,
            description=description,
            constraints=constraints,
            priority=priority,
            deadline_at=deadline_at,
            success_criteria=success_criteria,
            parent_goal_id=parent_goal_id,
            source=source,
        )
        self._publish("GOAL_CREATED", goal, {"objective": goal.objective[:200]})
        return goal

    def get(self, goal_id: str, *, owner_id: str | None = None) -> Goal | None:
        return self.store.get(goal_id, owner_id=owner_id)

    def list_goals(self, *, owner_id: str | None = None, limit: int = 100) -> list[Goal]:
        return self.store.list_goals(owner_id=owner_id, limit=limit)

    def pause(self, goal_id: str, owner_id: str) -> Goal | None:
        goal = self.store.get(goal_id, owner_id=owner_id)
        if goal is None:
            return None
        if goal.status in TERMINAL_GOAL_STATUSES or goal.status == GoalStatus.WAITING:
            return goal
        return self.store.transition(
            goal_id, GoalStatus.WAITING, note="paused by owner", owner_id=owner_id
        )

    def resume(self, goal_id: str, owner_id: str) -> Goal | None:
        goal = self.store.get(goal_id, owner_id=owner_id)
        if goal is None or goal.status != GoalStatus.WAITING:
            return goal
        return self.store.transition(
            goal_id, GoalStatus.ACTIVE, note="resumed by owner", owner_id=owner_id
        )

    def cancel(self, goal_id: str, owner_id: str) -> Goal | None:
        goal = self.store.get(goal_id, owner_id=owner_id)
        if goal is None:
            return None
        if goal.status in TERMINAL_GOAL_STATUSES:
            return goal
        for task in self.store.list_tasks(goal_id):
            if task.status in (GoalTaskStatus.QUEUED, GoalTaskStatus.RUNNING):
                if task.job_id:
                    self.automation.jobs.request_cancel(task.job_id, owner_id=owner_id)
                self.store.update_task(task.task_id, status=GoalTaskStatus.CANCELLED)
        updated = self.store.transition(
            goal_id, GoalStatus.CANCELLED, note="cancelled by owner", owner_id=owner_id
        )
        self._publish("GOAL_CANCELLED", updated, {})
        return updated

    def capture_plan(self, goal_id: str, plan: dict[str, Any]) -> bool:
        validated = self._validate_plan(plan)
        if validated is None:
            return False
        self.store.capture_plan(goal_id, validated)
        return True

    def capture_evaluation(self, goal_id: str, evaluation: dict[str, Any]) -> bool:
        if not isinstance(evaluation, dict):
            return False
        passed = evaluation.get("passed")
        if not isinstance(passed, bool):
            return False
        evidence = evaluation.get("evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
            return False
        self.store.capture_evaluation(
            goal_id,
            {"passed": passed, "evidence": [item[:500] for item in evidence[:10]]},
        )
        return True

    def evaluate(self, goal_id: str) -> GoalEvaluation | None:
        goal = self.store.get(goal_id)
        if goal is None:
            return None
        tasks = self.store.list_tasks(goal_id)
        if not tasks:
            return GoalEvaluation(
                progress=0.0,
                all_tasks_succeeded=False,
                any_task_failed=False,
                open_tasks=0,
                evidence=("goal has no tasks",),
            )
        succeeded = sum(1 for task in tasks if task.status == GoalTaskStatus.SUCCEEDED)
        failed = sum(1 for task in tasks if task.status == GoalTaskStatus.FAILED)
        cancelled = sum(1 for task in tasks if task.status == GoalTaskStatus.CANCELLED)
        open_tasks = sum(
            1
            for task in tasks
            if task.status
            in (GoalTaskStatus.PENDING, GoalTaskStatus.QUEUED, GoalTaskStatus.RUNNING)
        )
        progress = succeeded / len(tasks)
        evidence: list[str] = []
        for task in tasks:
            if task.status == GoalTaskStatus.SUCCEEDED:
                evidence.append(f"task '{task.title}' succeeded: {task.output[:200]}")
            elif task.status == GoalTaskStatus.FAILED:
                evidence.append(f"task '{task.title}' failed: {task.error or 'unknown error'}")
            elif task.status == GoalTaskStatus.CANCELLED:
                evidence.append(f"task '{task.title}' was superseded by a replan and is excluded")
        return GoalEvaluation(
            progress=progress,
            all_tasks_succeeded=(succeeded + cancelled) == len(tasks) and succeeded > 0,
            any_task_failed=failed > 0,
            open_tasks=open_tasks,
            evidence=tuple(evidence[:20]),
        )

    def advance_goal(self, goal_id: str, *, owner_id: str | None = None) -> GoalTickReport:
        report = GoalTickReport()
        goal = self.store.get(goal_id, owner_id=owner_id)
        if goal is None:
            report.notes.append(f"goal {goal_id} not found")
            return report
        if goal.status in TERMINAL_GOAL_STATUSES:
            return report
        if self._deadline_expired(goal):
            failed = self.store.transition(
                goal_id, GoalStatus.FAILED, note="deadline_exceeded", owner_id=goal.owner_id
            )
            self._publish("GOAL_FAILED", failed, {"reason": "deadline_exceeded"})
            report.goals_failed += 1
            report.notes.append(f"goal {goal_id} failed: deadline exceeded")
            return report
        if goal.status == GoalStatus.CREATED:
            self._submit_plan_job(goal, report)
            return report
        if goal.status in (GoalStatus.ANALYZING, GoalStatus.PLANNING, GoalStatus.REPLANNING):
            pending = self._plan_payload(goal)
            if pending is not None:
                self._apply_plan(goal, pending, report)
            elif self._goal_job_running(goal):
                report.notes.append(f"goal {goal_id} planning job still running")
            elif goal.status == GoalStatus.REPLANNING:
                self._submit_plan_job(goal, report)
            else:
                failed = self.store.transition(
                    goal_id,
                    GoalStatus.FAILED,
                    note="planning produced no usable plan",
                    owner_id=goal.owner_id,
                )
                self._publish("GOAL_FAILED", failed, {"reason": "planning_empty"})
                report.goals_failed += 1
                report.notes.append(f"goal {goal_id} failed: planning produced no plan")
            return report
        if goal.status == GoalStatus.WAITING:
            return report
        if goal.status in (GoalStatus.ACTIVE, GoalStatus.EXECUTING):
            self._submit_ready_tasks(goal, report)
            evaluation = self.evaluate(goal_id)
            if evaluation is not None:
                self.store.set_progress(goal_id, evaluation.progress)
                self._update_milestones(goal_id, evaluation.progress)
                if evaluation.open_tasks == 0:
                    self._handle_tasks_finished(goal, evaluation, report)
                    return report
                if goal.status == GoalStatus.ACTIVE and evaluation.progress > 0.0:
                    self.store.transition(goal_id, GoalStatus.EXECUTING, owner_id=goal.owner_id)
                    goal = self.store.get(goal_id) or goal
            self._publish(
                "GOAL_PROGRESS_CHANGED",
                goal,
                {"progress": round((evaluation.progress if evaluation else goal.progress), 3)},
            )
            report.goals_advanced += 1
            return report
        if goal.status == GoalStatus.EVALUATING:
            self._handle_evaluation_result(goal, report)
            return report
        if goal.status in (GoalStatus.BLOCKED, GoalStatus.NEEDS_APPROVAL):
            return report
        return report

    def tick(self, *, owner_id: str | None = None, limit: int = 50) -> GoalTickReport:
        report = GoalTickReport()
        active = self.store.list_goals(
            owner_id=owner_id,
            statuses=tuple(status for status in GoalStatus if status not in TERMINAL_GOAL_STATUSES),
            limit=limit,
        )
        for goal in active:
            partial = self.advance_goal(goal.goal_id, owner_id=owner_id)
            report.goals_advanced += partial.goals_advanced
            report.jobs_submitted += partial.jobs_submitted
            report.goals_completed += partial.goals_completed
            report.goals_failed += partial.goals_failed
            report.notes.extend(partial.notes)
        return report

    def on_job_finished(
        self, job_id: str, status: str, result: str, error: str | None
    ) -> GoalTask | None:
        task = self.store.task_for_job(job_id)
        if task is None:
            return None
        if status == "completed":
            updated = self.store.update_task(
                task.task_id,
                status=GoalTaskStatus.SUCCEEDED,
                output=result[:4000],
            )
        elif status == "failed":
            updated = self.store.update_task(
                task.task_id,
                status=GoalTaskStatus.FAILED,
                error=(error or result)[:1000],
            )
        elif status == "cancelled":
            updated = self.store.update_task(task.task_id, status=GoalTaskStatus.CANCELLED)
        else:
            updated = self.store.update_task(
                task.task_id,
                status=GoalTaskStatus.PENDING,
                error=(error or f"job status {status}")[:1000],
            )
        goal = self.store.get(task.goal_id)
        if goal is not None:
            self._publish(
                "GOAL_PROGRESS_CHANGED",
                goal,
                {"task": task.title, "task_status": status},
            )
            if status == "failed" and goal.status in (GoalStatus.ACTIVE, GoalStatus.EXECUTING):
                self._maybe_replan(goal)
        return updated

    def _on_job_finished(self, job: Any, status: str, result: str, error: str | None) -> None:
        payload = getattr(job, "payload", {}) or {}
        kind = payload.get("kind")
        if kind == "goal_plan":
            goal = self.store.get(str(payload.get("goal_id", "")))
            if goal is not None and status == "completed":
                pending = self._plan_payload(goal)
                if pending is not None:
                    report = GoalTickReport()
                    self._apply_plan(goal, pending, report)
            elif goal is not None and status == "failed":
                self.store.transition(
                    goal.goal_id,
                    GoalStatus.FAILED,
                    note=f"planning failed: {error or status}",
                    owner_id=goal.owner_id,
                )
                self._publish("GOAL_FAILED", goal, {"reason": "planning_failed"})
        elif kind == "goal_evaluate":
            goal = self.store.get(str(payload.get("goal_id", "")))
            if goal is not None and goal.status == GoalStatus.EVALUATING:
                if status == "completed":
                    self.advance_goal(goal.goal_id, owner_id=goal.owner_id)
                else:
                    self.store.transition(
                        goal.goal_id,
                        GoalStatus.FAILED,
                        note=f"evaluation failed: {error or ''}",
                        owner_id=goal.owner_id,
                    )
                    self._publish("GOAL_FAILED", goal, {"reason": "evaluation_failed"})
        else:
            self.on_job_finished(getattr(job, "job_id", ""), status, result, error)

    def _submit_plan_job(self, goal: Goal, report: GoalTickReport) -> None:
        if goal.status == GoalStatus.CREATED:
            self.store.transition(goal.goal_id, GoalStatus.ANALYZING, owner_id=goal.owner_id)
            goal = self.store.get(goal.goal_id) or goal
            self.store.transition(goal.goal_id, GoalStatus.PLANNING, owner_id=goal.owner_id)
            goal = self.store.get(goal.goal_id) or goal
        self.automation.submit(
            goal.owner_id,
            f"Decompose the goal '{goal.objective}' into concrete executable tasks. "
            "Call the goal_submit_plan tool with the task list before finishing.",
            payload={
                "kind": "goal_plan",
                "goal_id": goal.goal_id,
                "mode": "goal_planning",
                "max_steps": 12,
                "success_criteria": "; ".join(goal.success_criteria),
            },
            source="goal",
            priority=max(1, goal.priority - 1),
            max_attempts=2,
        )
        report.jobs_submitted += 1
        report.goals_advanced += 1
        report.notes.append(f"submitted planning job for goal {goal.goal_id}")

    def _submit_ready_tasks(self, goal: Goal, report: GoalTickReport) -> None:
        tasks = self.store.list_tasks(goal.goal_id)
        running = sum(
            1 for task in tasks if task.status in (GoalTaskStatus.QUEUED, GoalTaskStatus.RUNNING)
        )
        capacity = max(0, self.max_concurrent_task_jobs - running)
        if capacity == 0:
            return
        by_id = {task.task_id: task for task in tasks}
        for task in tasks:
            if capacity == 0:
                break
            if task.status != GoalTaskStatus.PENDING:
                continue
            if not self._dependencies_satisfied(task, by_id):
                continue
            job = self.automation.submit(
                goal.owner_id,
                task.title if not task.detail else f"{task.title}: {task.detail}",
                payload={
                    "kind": "goal_task",
                    "goal_id": goal.goal_id,
                    "task_id": task.task_id,
                    "mode": "goal_execution",
                    "success_criteria": task.success_criteria,
                    "deadline_at": (goal.deadline_at.isoformat() if goal.deadline_at else ""),
                },
                source="goal",
                priority=goal.priority,
                max_attempts=2,
            )
            self.store.update_task(
                task.task_id,
                status=GoalTaskStatus.QUEUED,
                job_id=job.job_id,
                increment_attempts=True,
            )
            capacity -= 1
            report.jobs_submitted += 1

    def _apply_plan(self, goal: Goal, plan: dict[str, Any], report: GoalTickReport) -> None:
        superseded = self._cancel_superseded_tasks(goal)
        tasks = plan.get("tasks", [])
        milestones = plan.get("milestones", [])
        milestone_ids: dict[str, str] = {}
        for milestone in milestones[:10]:
            created = self.store.add_milestone(
                goal.goal_id,
                str(milestone.get("title", "")),
                criteria=str(milestone.get("criteria", "")),
                target_progress=float(milestone.get("target_progress", 1.0)),
            )
            milestone_ids[created.title] = created.milestone_id
        task_id_by_title: dict[str, str] = {}
        pending_dependencies: list[tuple[str, list[str]]] = []
        for task in tasks[:MAX_GOAL_TASKS]:
            created_task = self.store.add_task(
                goal.goal_id,
                str(task.get("title", "")),
                detail=str(task.get("detail", "")),
                success_criteria=str(task.get("success_criteria", "")),
                milestone_id=milestone_ids.get(str(task.get("milestone", ""))),
            )
            task_id_by_title[created_task.title] = created_task.task_id
            depends = [str(dep) for dep in task.get("depends_on", []) if str(dep).strip()]
            if depends:
                pending_dependencies.append((created_task.task_id, depends))
        for task_id, depends in pending_dependencies:
            resolved = [task_id_by_title[dep] for dep in depends if dep in task_id_by_title]
            if resolved:
                self.store.update_task_dependencies(task_id, resolved)
        if goal.status in (GoalStatus.PLANNING, GoalStatus.REPLANNING, GoalStatus.ANALYZING):
            self.store.transition(
                goal.goal_id, GoalStatus.ACTIVE, note="plan applied", owner_id=goal.owner_id
            )
        report.goals_advanced += 1
        report.notes.append(
            f"applied plan with {len(tasks)} tasks and {len(milestones)} milestones"
            + (f", cancelled {superseded} superseded tasks" if superseded else "")
        )

    def _cancel_superseded_tasks(self, goal: Goal) -> int:
        cancelled = 0
        for task in self.store.list_tasks(goal.goal_id):
            if task.status not in (
                GoalTaskStatus.PENDING,
                GoalTaskStatus.QUEUED,
                GoalTaskStatus.RUNNING,
            ):
                continue
            if task.job_id and task.status in (GoalTaskStatus.QUEUED, GoalTaskStatus.RUNNING):
                self.automation.jobs.request_cancel(task.job_id, owner_id=goal.owner_id)
            self.store.update_task(
                task.task_id,
                status=GoalTaskStatus.CANCELLED,
                error="superseded by replan",
            )
            cancelled += 1
        return cancelled

    def _handle_tasks_finished(
        self, goal: Goal, evaluation: GoalEvaluation, report: GoalTickReport
    ) -> None:
        if evaluation.all_tasks_succeeded:
            self.store.transition(
                goal.goal_id,
                GoalStatus.EVALUATING,
                note="all tasks succeeded",
                owner_id=goal.owner_id,
            )
            goal = self.store.get(goal.goal_id) or goal
            if self.evaluation_mode == "deterministic":
                evidence = [
                    *evaluation.evidence,
                    *goal.success_criteria,
                ]
                completed = self.store.transition(
                    goal.goal_id,
                    GoalStatus.COMPLETED,
                    note="completed: " + "; ".join(evidence)[:1000],
                    owner_id=goal.owner_id,
                )
                self._publish(
                    "GOAL_COMPLETED", completed, {"progress": 1.0, "evidence": evidence[:10]}
                )
                report.goals_completed += 1
                report.notes.append(f"goal {goal.goal_id} completed")
            else:
                self._submit_evaluation_job(goal, report)
            return
        if evaluation.any_task_failed:
            self._maybe_replan(goal)
            report.notes.append(f"goal {goal.goal_id} scheduled replanning")
            return
        if goal.status == GoalStatus.EXECUTING:
            self.store.transition(
                goal.goal_id, GoalStatus.ACTIVE, note="no open tasks", owner_id=goal.owner_id
            )

    def _handle_evaluation_result(self, goal: Goal, report: GoalTickReport) -> None:
        if self.evaluation_mode == "agent":
            pending = self.store.take_pending_evaluation(goal.goal_id)
            if pending is not None:
                if bool(pending.get("passed")):
                    evidence = [str(item) for item in pending.get("evidence", [])]
                    completed = self.store.transition(
                        goal.goal_id,
                        GoalStatus.COMPLETED,
                        note="completed: " + "; ".join(evidence)[:1000],
                    )
                    self._publish("GOAL_COMPLETED", completed, {"evidence": evidence[:10]})
                    report.goals_completed += 1
                    return
                self._maybe_replan(goal)
                return
            if self._goal_job_running(goal):
                report.notes.append(f"goal {goal.goal_id} evaluation job still running")
                return
            self._submit_evaluation_job(goal, report)
            return
        evaluation = self.evaluate(goal.goal_id)
        if evaluation is not None and evaluation.all_tasks_succeeded:
            completed = self.store.transition(
                goal.goal_id,
                GoalStatus.COMPLETED,
                note="completed: deterministic evaluation",
                owner_id=goal.owner_id,
            )
            self._publish("GOAL_COMPLETED", completed, {})
            report.goals_completed += 1

    def _submit_evaluation_job(self, goal: Goal, report: GoalTickReport) -> None:
        self.automation.submit(
            goal.owner_id,
            f"Evaluate whether the goal '{goal.objective}' is achieved. "
            "Call the goal_submit_evaluation tool with the verdict and evidence.",
            payload={
                "kind": "goal_evaluate",
                "goal_id": goal.goal_id,
                "mode": "goal_evaluation",
                "max_steps": 8,
                "success_criteria": "; ".join(goal.success_criteria),
            },
            source="goal",
            priority=goal.priority,
            max_attempts=1,
        )
        report.jobs_submitted += 1

    def _maybe_replan(self, goal: Goal) -> None:
        if goal.replan_count >= self.max_replans:
            failed = self.store.transition(
                goal.goal_id,
                GoalStatus.FAILED,
                note="replan budget exhausted",
                owner_id=goal.owner_id,
            )
            self._publish("GOAL_FAILED", failed, {"reason": "replan_exhausted"})
            return
        if goal.status in (
            GoalStatus.ACTIVE,
            GoalStatus.EXECUTING,
            GoalStatus.EVALUATING,
            GoalStatus.BLOCKED,
        ):
            self.store.transition(
                goal.goal_id, GoalStatus.REPLANNING, note="task failure", owner_id=goal.owner_id
            )
            updated = self.store.get(goal.goal_id) or goal
            self.store.set_replan_count(
                updated.goal_id, updated.replan_count + 1, owner_id=goal.owner_id
            )
            self._submit_plan_job(updated, GoalTickReport())

    def _dependencies_satisfied(self, task: GoalTask, by_id: dict[str, GoalTask]) -> bool:
        for dep in task.depends_on:
            dependency = by_id.get(dep)
            if dependency is None or dependency.status != GoalTaskStatus.SUCCEEDED:
                return False
        return True

    def _goal_job_running(self, goal: Goal) -> bool:
        for job in self.automation.jobs.list_jobs(limit=500):
            payload = job.payload or {}
            if (
                payload.get("goal_id") == goal.goal_id
                and payload.get("kind") in ("goal_plan", "goal_evaluate")
                and job.status in ("queued", "running")
            ):
                return True
        return False

    def _plan_payload(self, goal: Goal) -> dict[str, Any] | None:
        pending = self.store.take_pending_plan(goal.goal_id)
        if pending is None:
            return None
        return pending

    def _validate_plan(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(plan, dict):
            return None
        tasks = plan.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return None
        validated_tasks: list[dict[str, Any]] = []
        titles: set[str] = set()
        for task in tasks[:MAX_GOAL_TASKS]:
            if not isinstance(task, dict):
                continue
            title = str(task.get("title", "")).strip()
            if not title or len(title) > 200 or title in titles:
                continue
            titles.add(title)
            validated_tasks.append(
                {
                    "title": title,
                    "detail": str(task.get("detail", ""))[:2000],
                    "depends_on": [
                        str(dep) for dep in task.get("depends_on", []) if str(dep).strip()
                    ][:10],
                    "success_criteria": str(task.get("success_criteria", ""))[:600],
                    "milestone": str(task.get("milestone", "")),
                }
            )
        if not validated_tasks:
            return None
        milestones: list[dict[str, Any]] = []
        for milestone in plan.get("milestones", [])[:10]:
            if not isinstance(milestone, dict):
                continue
            title = str(milestone.get("title", "")).strip()
            if not title:
                continue
            try:
                target = float(milestone.get("target_progress", 1.0))
            except (TypeError, ValueError):
                target = 1.0
            milestones.append(
                {
                    "title": title[:200],
                    "criteria": str(milestone.get("criteria", ""))[:600],
                    "target_progress": min(1.0, max(0.01, target)),
                }
            )
        return {"tasks": validated_tasks, "milestones": milestones}

    def _update_milestones(self, goal_id: str, progress: float) -> None:
        for milestone in self.store.list_milestones(goal_id):
            if milestone.status != MilestoneStatus.PENDING:
                continue
            if progress >= milestone.target_progress:
                self.store.update_milestone(milestone.milestone_id, MilestoneStatus.REACHED)

    def _deadline_expired(self, goal: Goal) -> bool:
        return goal.deadline_at is not None and goal.deadline_at <= datetime.now(UTC)

    def _publish(self, event_type: str, goal: Goal, payload: dict[str, Any]) -> None:
        if self.event_publisher is None:
            return
        try:
            self.event_publisher(event_type, "goal", goal.goal_id, payload)
        except Exception:
            log.exception("goal event publication failed")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
