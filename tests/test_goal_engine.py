from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.goals.engine import GoalEngine
from eurus.goals.states import GoalStatus, GoalTaskStatus, InvalidGoalTransition, MilestoneStatus
from eurus.storage.database import Database

EVENTS: list[tuple[str, str, str]] = []


class SimulatingExecutor:
    def __init__(self, goals: GoalEngine | None = None) -> None:
        self.goals = goals
        self.executed: list[str] = []
        self.behaviors: dict[str, str] = {}

    def bind(self, goals: GoalEngine) -> None:
        self.goals = goals

    async def execute(self, job: Any, context: Any) -> str:
        self.executed.append(job.job_id)
        payload = job.payload or {}
        kind = payload.get("kind")
        if kind == "goal_plan":
            goal_id = str(payload.get("goal_id"))
            plan = {
                "tasks": [
                    {
                        "title": "collect data",
                        "detail": "gather the inputs",
                        "success_criteria": "data gathered",
                    },
                    {
                        "title": "write summary",
                        "detail": "summarize findings",
                        "depends_on": ["collect data"],
                        "success_criteria": "summary written",
                        "milestone": "draft ready",
                    },
                ],
                "milestones": [{"title": "draft ready", "criteria": "summary exists"}],
            }
            if self.behaviors.get("plan") == "empty":
                plan = {"tasks": []}
            if self.behaviors.get("plan") != "empty":
                assert self.goals is not None
                self.goals.capture_plan(goal_id, plan)
            return "plan submitted"
        if kind == "goal_task":
            title = job.objective.split(":")[0]
            if self.behaviors.get(f"task:{title}") == "fail":
                raise TimeoutError("simulated task failure")
            return f"completed {title}"
        if kind == "goal_evaluate":
            goal_id = str(payload.get("goal_id"))
            assert self.goals is not None
            self.goals.capture_evaluation(
                goal_id, {"passed": True, "evidence": ["all outputs verified"]}
            )
            return "evaluation submitted"
        return "ok"


def build(
    tmp_path: Path,
    *,
    evaluation_mode: str = "deterministic",
    behaviors: dict[str, str] | None = None,
):
    database = Database(tmp_path / "goals.sqlite3")
    executor = SimulatingExecutor()
    retry = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=2,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.01,
            jitter_fraction=0.0,
        )
    )
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    goals = GoalEngine(
        database,
        automation,
        event_publisher=lambda event_type, subject_type, subject_id, payload: EVENTS.append(
            (event_type, subject_type, subject_id)
        ),
        evaluation_mode=evaluation_mode,
    )
    executor.bind(goals)
    if behaviors:
        executor.behaviors = behaviors
    return goals, automation, executor, database


async def run_until_idle(
    goals: GoalEngine, automation: AutomationEngine, *, rounds: int = 12
) -> None:
    for _ in range(rounds):
        goals.tick()
        worked = False
        for _ in range(10):
            if await automation.run_worker():
                worked = True
            else:
                break
        if not worked:
            await asyncio.sleep(0.02)
        if not worked and goals.store.counts_by_status().get(
            "completed", 0
        ) + goals.store.counts_by_status().get("failed", 0) >= max(
            1, len(goals.store.list_goals(limit=500))
        ):
            return
    goals.tick()
    for _ in range(10):
        if not await automation.run_worker():
            break


async def test_goal_lifecycle_from_creation_to_completion(tmp_path: Path) -> None:
    goals, automation, executor, _ = build(tmp_path)
    goal = goals.create_goal(
        "owner",
        "Produce a weekly market digest",
        description="Track and summarize",
        success_criteria=["digest exists", "sources cited"],
    )
    assert goal.status == GoalStatus.CREATED
    await run_until_idle(goals, automation)
    final = goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.COMPLETED
    assert final.progress == 1.0
    assert final.completion_evidence
    tasks = goals.store.list_tasks(goal.goal_id)
    assert len(tasks) == 2
    statuses = {task.status for task in tasks}
    assert statuses == {GoalTaskStatus.SUCCEEDED}
    assert any(
        event == "GOAL_COMPLETED" and subject_id == goal.goal_id
        for event, subject_type, subject_id in EVENTS
    )


async def test_goal_task_dependencies_are_respected(tmp_path: Path) -> None:
    goals, automation, executor, _ = build(tmp_path)
    goal = goals.create_goal("owner", "ordered work")
    goals.tick()
    assert goals.store.get(goal.goal_id).status == GoalStatus.PLANNING
    await automation.run_worker()
    tasks = goals.store.list_tasks(goal.goal_id)
    by_title = {task.title: task for task in tasks}
    assert by_title["write summary"].depends_on == (by_title["collect data"].task_id,)
    goals.tick()
    queued = [
        task
        for task in goals.store.list_tasks(goal.goal_id)
        if task.status == GoalTaskStatus.QUEUED
    ]
    assert len(queued) == 1
    assert queued[0].title == "collect data"
    await automation.run_worker()
    goals.tick()
    remaining = [
        task
        for task in goals.store.list_tasks(goal.goal_id)
        if task.status == GoalTaskStatus.QUEUED
    ]
    assert len(remaining) == 1
    assert remaining[0].title == "write summary"
    await automation.run_worker()
    await run_until_idle(goals, automation)
    assert goals.store.get(goal.goal_id).status == GoalStatus.COMPLETED


async def test_goal_survives_restart(tmp_path: Path) -> None:
    goals, automation, executor, database = build(tmp_path)
    goal = goals.create_goal("owner", "persistent goal")
    goals.tick()
    await automation.run_worker()
    assert goals.store.get(goal.goal_id).status in (GoalStatus.PLANNING, GoalStatus.ACTIVE)
    database.close()
    reopened = Database(tmp_path / "goals.sqlite3")
    executor2 = SimulatingExecutor()
    automation2 = AutomationEngine(reopened, executor2, poll_seconds=0.05)
    goals2 = GoalEngine(reopened, automation2)
    executor2.bind(goals2)
    await run_until_idle(goals2, automation2)
    final = goals2.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.COMPLETED


async def test_goal_failure_triggers_replanning_then_failure(tmp_path: Path) -> None:
    goals, automation, executor, _ = build(tmp_path, behaviors={"task:collect data": "fail"})
    goal = goals.create_goal("owner", "flaky goal")
    await run_until_idle(goals, automation)
    final = goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.FAILED
    assert final.replan_count >= 1
    tasks = goals.store.list_tasks(goal.goal_id)
    assert any(task.status == GoalTaskStatus.FAILED for task in tasks)


async def test_goal_deadline_expires(tmp_path: Path) -> None:
    goals, automation, _, _ = build(tmp_path)
    goal = goals.create_goal(
        "owner",
        "urgent goal",
        deadline_at=datetime.now(UTC) + timedelta(milliseconds=1),
    )
    await asyncio_sleep()
    goals.tick()
    final = goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.FAILED
    assert "deadline" in " ".join(final.completion_evidence)


async def asyncio_sleep() -> None:
    await asyncio.sleep(0.01)


async def test_goal_pause_resume_and_cancel(tmp_path: Path) -> None:
    goals, automation, _, _ = build(tmp_path)
    goal = goals.create_goal("owner", "controllable goal")
    goals.tick()
    paused = goals.pause(goal.goal_id, "owner")
    assert paused is not None
    assert paused.status == GoalStatus.WAITING
    report = goals.tick()
    assert report.jobs_submitted == 0
    resumed = goals.resume(goal.goal_id, "owner")
    assert resumed is not None
    assert resumed.status == GoalStatus.ACTIVE
    cancelled = goals.cancel(goal.goal_id, "owner")
    assert cancelled is not None
    assert cancelled.status == GoalStatus.CANCELLED
    assert goals.cancel(goal.goal_id, "owner").status == GoalStatus.CANCELLED
    assert goals.pause("missing", "owner") is None
    assert goals.resume("missing", "owner") is None


async def test_goal_agent_evaluation_mode(tmp_path: Path) -> None:
    goals, automation, executor, _ = build(tmp_path, evaluation_mode="agent")
    goal = goals.create_goal("owner", "agent evaluated goal")
    await run_until_idle(goals, automation)
    final = goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.COMPLETED
    assert any("all outputs verified" in item for item in final.completion_evidence)


async def test_goal_invalid_transitions_rejected(tmp_path: Path) -> None:
    goals, _, _, _ = build(tmp_path)
    goal = goals.create_goal("owner", "transition test")
    with pytest.raises(InvalidGoalTransition):
        goals.store.transition(goal.goal_id, GoalStatus.COMPLETED)
    with pytest.raises(InvalidGoalTransition):
        goals.store.transition(goal.goal_id, GoalStatus.EXECUTING)
    completed = goals.store.transition(goal.goal_id, GoalStatus.CANCELLED)
    with pytest.raises(InvalidGoalTransition):
        goals.store.transition(completed.goal_id, GoalStatus.ACTIVE)


async def test_goal_milestones_reached(tmp_path: Path) -> None:
    goals, automation, _, _ = build(tmp_path)
    goal = goals.create_goal("owner", "milestone goal")
    goals.tick()
    await automation.run_worker()
    milestones = goals.store.list_milestones(goal.goal_id)
    assert len(milestones) == 1
    assert milestones[0].title == "draft ready"
    assert milestones[0].status == MilestoneStatus.PENDING
    await run_until_idle(goals, automation)
    refreshed = goals.store.list_milestones(goal.goal_id)
    assert refreshed[0].status == MilestoneStatus.REACHED


async def test_goal_waiting_does_not_burn_jobs(tmp_path: Path) -> None:
    goals, automation, executor, _ = build(tmp_path)
    goal = goals.create_goal("owner", "patient goal")
    goals.tick()
    await automation.run_worker()
    assert goals.store.get(goal.goal_id).status == GoalStatus.ACTIVE
    goals.tick()
    running = [
        task
        for task in goals.store.list_tasks(goal.goal_id)
        if task.status in (GoalTaskStatus.QUEUED, GoalTaskStatus.RUNNING)
    ]
    assert len(running) >= 1
    submitted_before = automation.jobs_submitted
    for _ in range(3):
        goals.tick()
    assert automation.jobs_submitted == submitted_before


async def test_goal_plan_validation_rejects_bad_plans(tmp_path: Path) -> None:
    goals, _, _, _ = build(tmp_path)
    goal = goals.create_goal("owner", "validation goal")
    assert goals.capture_plan(goal.goal_id, {"tasks": []}) is False
    assert goals.capture_plan(goal.goal_id, {"tasks": [{"title": ""}]}) is False
    assert goals.capture_plan(goal.goal_id, {"tasks": "nope"}) is False
    assert (
        goals.capture_plan(
            goal.goal_id,
            {
                "tasks": [
                    {"title": "same"},
                    {"title": "same"},
                    {"title": "ok"},
                ]
            },
        )
        is True
    )
    plan = goals.store.take_pending_plan(goal.goal_id)
    assert plan is not None
    assert len(plan["tasks"]) == 2
    assert goals.capture_evaluation(goal.goal_id, {"passed": "yes"}) is False
    assert goals.capture_evaluation(goal.goal_id, {"passed": True}) is True


async def test_goal_empty_plan_fails_goal(tmp_path: Path) -> None:
    goals, automation, _, _ = build(tmp_path, behaviors={"plan": "empty"})
    goal = goals.create_goal("owner", "doomed goal")
    goals.tick()
    await automation.run_worker()
    goals.tick()
    final = goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.FAILED
    assert any("no usable plan" in item for item in final.completion_evidence)


async def test_goal_owner_isolation(tmp_path: Path) -> None:
    goals, _, _, _ = build(tmp_path)
    goal = goals.create_goal("alice", "private goal")
    assert goals.get(goal.goal_id, owner_id="bob") is None
    assert goals.get(goal.goal_id, owner_id="alice") is not None
    assert goals.pause(goal.goal_id, "bob") is None


async def test_goal_task_capacity_limit(tmp_path: Path) -> None:
    goals, automation, _, _ = build(tmp_path)
    original = goals.max_concurrent_task_jobs
    goals.max_concurrent_task_jobs = 1
    goal = goals.create_goal("owner", "limited parallelism")
    goals.tick()
    await automation.run_worker()
    goals.tick()
    queued = [
        task
        for task in goals.store.list_tasks(goal.goal_id)
        if task.status == GoalTaskStatus.QUEUED
    ]
    assert len(queued) == 1
    goals.max_concurrent_task_jobs = original
