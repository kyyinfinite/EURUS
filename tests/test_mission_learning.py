from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from eurus import paths
from eurus.agent.acceptance import AcceptanceService, ReviewStore
from eurus.agent.mission_runtime import MissionProgression
from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor, MissionStore
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.automation.supervision import RuntimeSupervisor
from eurus.cli import main
from eurus.config import load_config
from eurus.learning.integration import LearningCoordinator
from eurus.learning.lessons import LessonStore
from eurus.learning.missions import MissionLearning
from eurus.multiagent.contracts import ReviewFinding, TaskSpec
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database

OWNER = "owner-a"

TASKS_TWO = [
    {"task_key": "ui", "objective": "implement the page", "preferred_role": "frontend"},
    {"task_key": "api", "objective": "implement the API", "preferred_role": "backend"},
]


class SelectiveExecutor:
    def __init__(self, fail_markers: tuple[str, ...] = ()) -> None:
        self.executed: list[str] = []
        self.fail_markers = fail_markers

    async def execute(self, job: Any, context: Any) -> str:
        self.executed.append(job.objective)
        if any(marker in job.objective for marker in self.fail_markers):
            raise RuntimeError(f"intentional failure: {job.objective}")
        return "done"


class Stack:
    def __init__(
        self,
        tmp_path: Path,
        *,
        fail_markers: tuple[str, ...] = (),
        max_task_attempts: int = 3,
    ) -> None:
        self.database = Database(tmp_path / "missions.sqlite3")
        self.executor = SelectiveExecutor(fail_markers=fail_markers)
        self.automation = AutomationEngine(
            self.database,
            self.executor,
            retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
            poll_seconds=0.05,
        )
        self.orchestrator = AgentOrchestrator(self.database, self.automation)
        registry = SpecialistRegistry(self.database)
        registry.register("frontend", allowed_tools=["read_file"])
        registry.register("backend", allowed_tools=["read_file"])
        registry.register("qa", allowed_tools=["read_file"])
        self.supervisor = MainSupervisor(self.database, self.orchestrator)
        self.lessons = LessonStore(tmp_path / "missions.sqlite3", limit=5, max_lessons=500)
        self.learning = MissionLearning(self.database, lessons=self.lessons, backfill_limit=10)
        self.progression = MissionProgression(
            self.supervisor,
            review=ReviewLoop(self.database, max_cycles=3),
            max_task_attempts=max_task_attempts,
        )

    async def drain(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            worked = False
            for _ in range(10):
                if await self.automation.run_worker():
                    worked = True
                else:
                    break
            if not worked:
                return
            await asyncio.sleep(0.01)

    def seed(
        self,
        *,
        objective: str = "Ship the feature end to end",
        tasks: list[dict[str, Any]] | None = None,
        budget: dict[str, Any] | None = None,
        owner_id: str = OWNER,
    ) -> str:
        mission = self.supervisor.create_mission(
            objective,
            owner_id=owner_id,
            acceptance_criteria=["evidence recorded"],
            budget=budget,
        )
        self.supervisor.plan_mission(
            mission.mission_id, owner_id=owner_id, tasks=tasks or TASKS_TWO
        )
        self.supervisor.start_mission(mission.mission_id, owner_id=owner_id)
        return mission.mission_id

    def tasks_by_key(self, mission_id: str, *, owner_id: str = OWNER) -> dict[str, TaskSpec]:
        return {
            task.task_key: task
            for task in self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id)
        }

    def succeed_all(
        self,
        mission_id: str,
        *,
        check: str = "pytest",
        status: str = "passed",
        owner_id: str = OWNER,
    ) -> None:
        for task in self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status != "succeeded":
                self.supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=owner_id)
            self.supervisor.record_verification(
                mission_id,
                task.task_id,
                owner_id=owner_id,
                check_name=check,
                status=status,
                exit_code=0 if status == "passed" else 1,
            )

    def exhaust(self, mission_id: str, task_key: str, *, owner_id: str = OWNER) -> None:
        task = self.tasks_by_key(mission_id, owner_id=owner_id)[task_key]
        for _ in range(3):
            self.supervisor.tasks.update_status(
                task.task_id, "ready", owner_id=owner_id, increment_attempt=True
            )
        self.supervisor.tasks.update_status(task.task_id, "failed", owner_id=owner_id)


@pytest.fixture()
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


def _dimension(view: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [item for item in view["evaluations"] if item["dimension"] == name]
    assert matches, f"missing evaluation dimension {name!r}"
    return matches[0]


class TestRecord:
    def test_healthy_completed_mission_records_five_evaluations_and_no_lessons(
        self, stack: Stack
    ) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        report = stack.learning.record(mission_id, owner_id=OWNER)
        assert report.processed == 1
        assert report.evaluations_recorded == 5
        assert report.lessons_accepted == 0
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None and view["learned"]
        for name in (
            "plan_quality",
            "task_correctness",
            "verification_coverage",
            "resource_efficiency",
            "agent_performance",
        ):
            item = _dimension(view, name)
            assert item["passed"], f"{name} should pass on a verified completion"
            assert item["score"] == 1.0
        assert view["lessons"] == []
        evidence = _dimension(view, "agent_performance")["evidence"]
        assert any(line.startswith("role=frontend:") for line in evidence)
        assert any(line.startswith("role=backend:") for line in evidence)

    def test_exhausted_and_blocked_dependents_lower_scores_and_learn_lessons(
        self, stack: Stack
    ) -> None:
        mission_id = stack.seed(
            objective="Ship the dependent pipeline",
            tasks=[
                {"task_key": "base", "objective": "build the base", "preferred_role": "backend"},
                {
                    "task_key": "front",
                    "objective": "build the front",
                    "preferred_role": "frontend",
                    "depends_on": ["base"],
                },
                {
                    "task_key": "docs",
                    "objective": "write docs",
                    "preferred_role": "qa",
                    "depends_on": ["base"],
                },
            ],
        )
        stack.exhaust(mission_id, "base")
        for key in ("front", "docs"):
            task = stack.tasks_by_key(mission_id)[key]
            stack.supervisor.tasks.update_status(task.task_id, "blocked", owner_id=OWNER)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "blocked"
        report = stack.learning.record(mission_id, owner_id=OWNER)
        assert report.processed == 1
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        plan_quality = _dimension(view, "plan_quality")
        assert plan_quality["score"] < 1.0
        assert not plan_quality["passed"]
        agent_performance = _dimension(view, "agent_performance")
        assert agent_performance["score"] < 1.0
        assert not agent_performance["passed"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("exhausted dispatch attempts" in text for text in texts)
        assert any("dependency ordering" in text for text in texts)

    def test_deadline_violation_records_expired_evidence_and_lesson(self, stack: Stack) -> None:
        deadline = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        mission_id = stack.seed(budget={"deadline": deadline})
        stack.supervisor.missions.update(mission_id, owner_id=OWNER, status="cancelled")
        report = stack.learning.record(mission_id, owner_id=OWNER)
        assert report.processed == 1
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        efficiency = _dimension(view, "resource_efficiency")
        assert efficiency["score"] == 0.0
        assert not efficiency["passed"]
        assert "expired=yes" in efficiency["evidence"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("missed its budget deadline" in text for text in texts)

    def test_cancelled_before_deadline_scores_partial_efficiency(self, stack: Stack) -> None:
        deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        mission_id = stack.seed(budget={"deadline": deadline})
        stack.supervisor.missions.update(mission_id, owner_id=OWNER, status="cancelled")
        stack.learning.record(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        efficiency = _dimension(view, "resource_efficiency")
        assert efficiency["score"] == 0.4
        assert "expired=no" in efficiency["evidence"]

    def test_unverified_completion_records_coverage_lesson(self, stack: Stack) -> None:
        mission_id = stack.seed()
        for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
            stack.supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=OWNER)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "completed_unverified"
        stack.learning.record(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        coverage = _dimension(view, "verification_coverage")
        assert coverage["score"] == 0.0
        assert not coverage["passed"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("complete verification evidence" in text for text in texts)

    def test_failed_check_records_failed_check_lesson(self, stack: Stack) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id, check="pytest", status="failed")
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "blocked"
        stack.learning.record(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        coverage = _dimension(view, "verification_coverage")
        assert "failed_checks=pytest" in coverage["evidence"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("failed verification checks (pytest)" in text for text in texts)

    def test_open_blocking_review_finding_is_counted_and_learned(self, stack: Stack) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id)
        task = stack.tasks_by_key(mission_id)["ui"]
        ReviewStore(stack.database).submit(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task.task_id,
            reviewer_assignment_id=None,
            status="changes_requested",
            findings=(
                ReviewFinding(
                    finding_id="f1",
                    severity="blocker",
                    category="correctness",
                    location="page.tsx",
                    evidence="broken render",
                    suggested_remediation="fix render",
                    disposition="open",
                ),
            ),
            summary="blocking finding",
        )
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "blocked"
        stack.learning.record(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        plan_quality = _dimension(view, "plan_quality")
        assert "open_blocking_findings=1" in plan_quality["evidence"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("open blocking review finding" in text for text in texts)

    def test_revision_pressure_penalizes_performance_and_plan(self, stack: Stack) -> None:
        mission_id = stack.seed(
            objective="Ship the hardened feature with reviews",
            tasks=[
                {"task_key": "ui", "objective": "build the page", "preferred_role": "frontend"},
                {"task_key": "api", "objective": "build the API", "preferred_role": "backend"},
                {"task_key": "docs", "objective": "write docs", "preferred_role": "qa"},
            ],
        )
        tasks = stack.tasks_by_key(mission_id)
        for key in ("ui", "api"):
            revision = TaskSpec(
                task_id=uuid.uuid4().hex,
                mission_id=mission_id,
                objective="apply review fixes",
                task_key=f"{key}-revision-1",
                parent_task_id=tasks[key].task_id,
                preferred_role=tasks[key].preferred_role,
                status="succeeded",
            )
            stack.supervisor.tasks.add_task(revision, owner_id=OWNER)
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "completed"
        stack.learning.record(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        assert _dimension(view, "plan_quality")["score"] == 0.8
        performance = _dimension(view, "agent_performance")
        assert performance["score"] == 0.7
        assert not performance["passed"]
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("revision task(s)" in text for text in texts)

    def test_record_is_idempotent(self, stack: Stack) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        first = stack.learning.record(mission_id, owner_id=OWNER)
        second = stack.learning.record(mission_id, owner_id=OWNER)
        assert first.processed == 1
        assert second.processed == 0
        assert second.skipped == 1
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        assert len(view["evaluations"]) == 5

    def test_record_refuses_non_terminal_mission(self, stack: Stack) -> None:
        mission_id = stack.seed()
        report = stack.learning.record(mission_id, owner_id=OWNER)
        assert report.processed == 0
        assert report.skipped == 1
        assert not stack.learning.has_learned(mission_id)

    def test_record_skips_unknown_mission(self, stack: Stack) -> None:
        report = stack.learning.record("missing", owner_id=OWNER)
        assert report.skipped == 1
        assert report.notes

    def test_record_without_lesson_store_still_records_evaluations(self, stack: Stack) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        bare = MissionLearning(stack.database, backfill_limit=10)
        report = bare.record(mission_id, owner_id=OWNER)
        assert report.processed == 1
        assert report.evaluations_recorded == 5
        assert report.lessons_accepted == 0
        assert bare.has_learned(mission_id)


class TestBackfill:
    def _terminal(self, stack: Stack, count: int) -> list[str]:
        ids = []
        for index in range(count):
            mission_id = stack.seed(objective=f"Ship feature number {index}")
            stack.succeed_all(mission_id)
            stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
            ids.append(mission_id)
        return ids

    def test_backfill_processes_then_has_nothing_left(self, stack: Stack) -> None:
        ids = self._terminal(stack, 3)
        first = stack.learning.backfill(owner_id=OWNER)
        assert first.processed == 3
        assert all(stack.learning.has_learned(mission_id) for mission_id in ids)
        second = stack.learning.backfill(owner_id=OWNER)
        assert second.processed == 0
        assert second.failed == 0

    def test_backfill_respects_limit_and_drains_the_backlog(self, stack: Stack) -> None:
        self._terminal(stack, 3)
        first = stack.learning.backfill(owner_id=OWNER, limit=2)
        assert first.processed == 2
        second = stack.learning.backfill(owner_id=OWNER, limit=2)
        assert second.processed == 1
        third = stack.learning.backfill(owner_id=OWNER, limit=2)
        assert third.processed == 0

    def test_backfill_isolation_on_learning_failure(
        self, stack: Stack, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good_id = stack.seed(objective="Ship the good feature")
        stack.succeed_all(good_id)
        stack.supervisor.finalize_mission(good_id, owner_id=OWNER)
        bad_id = stack.seed(objective="Ship the broken feature")
        stack.succeed_all(bad_id)
        stack.supervisor.finalize_mission(bad_id, owner_id=OWNER)
        original = MissionLearning._learn

        def flaky(self: MissionLearning, mission: Any, *, owner_id: str) -> Any:
            if mission.mission_id == bad_id:
                raise RuntimeError("boom")
            return original(self, mission, owner_id=owner_id)

        monkeypatch.setattr(MissionLearning, "_learn", flaky)
        report = stack.learning.backfill(owner_id=OWNER)
        assert report.failed == 1
        assert report.processed == 1
        assert stack.learning.has_learned(good_id)
        assert not stack.learning.has_learned(bad_id)

    def test_backfill_is_owner_scoped(self, stack: Stack) -> None:
        mission_id = stack.seed(owner_id=OWNER)
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        other = stack.learning.backfill(owner_id="owner-b")
        assert other.processed == 0 and other.skipped == 0
        own = stack.learning.backfill(owner_id=OWNER)
        assert own.processed == 1

    def test_constructor_validates_bounds(self, tmp_path: Path) -> None:
        database = Stack(tmp_path).database
        with pytest.raises(ValueError):
            MissionLearning(database, backfill_limit=0)
        with pytest.raises(ValueError):
            MissionLearning(database, lesson_candidates_limit=0)
        with pytest.raises(ValueError):
            MissionLearning(database, backfill_limit=201)


class TestStatsAndView:
    def test_stats_counts_terminal_and_learned(self, stack: Stack) -> None:
        learned_id = stack.seed(objective="Ship the learned feature")
        stack.succeed_all(learned_id)
        stack.supervisor.finalize_mission(learned_id, owner_id=OWNER)
        pending_id = stack.seed(objective="Ship the pending feature")
        stack.succeed_all(pending_id)
        stack.supervisor.finalize_mission(pending_id, owner_id=OWNER)
        stack.learning.record(learned_id, owner_id=OWNER)
        stats = stack.learning.stats(owner_id=OWNER)
        assert stats == {"terminal": 2, "learned": 1, "pending": 1}

    def test_view_for_unlearned_terminal_mission(self, stack: Stack) -> None:
        mission_id = stack.seed()
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        view = stack.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        assert not view["learned"]
        assert view["evaluations"] == []
        assert view["lessons"] == []

    def test_view_returns_none_for_unknown_mission(self, stack: Stack) -> None:
        assert stack.learning.learning_view("missing", owner_id=OWNER) is None

    def test_view_is_owner_scoped(self, stack: Stack) -> None:
        mission_id = stack.seed(owner_id=OWNER)
        assert stack.learning.learning_view(mission_id, owner_id="owner-b") is None


class TestAnalytics:
    def test_analytics_aggregates_decisions_means_roles_and_lessons(self, stack: Stack) -> None:
        healthy = stack.seed(objective="Ship the clean feature")
        stack.succeed_all(healthy)
        stack.supervisor.finalize_mission(healthy, owner_id=OWNER)

        deadline = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        broken = stack.seed(
            objective="Ship the stalled feature",
            budget={"deadline": deadline},
            tasks=[
                {"task_key": "base", "objective": "build the base", "preferred_role": "backend"},
                {
                    "task_key": "front",
                    "objective": "build the front",
                    "preferred_role": "qa",
                    "depends_on": ["base"],
                },
            ],
        )
        stack.exhaust(broken, "base")
        front = stack.tasks_by_key(broken)["front"]
        stack.supervisor.tasks.update_status(front.task_id, "blocked", owner_id=OWNER)
        stack.supervisor.missions.update(broken, owner_id=OWNER, status="cancelled")

        stack.learning.backfill(owner_id=OWNER)
        snapshot = stack.learning.analytics(owner_id=OWNER)
        assert snapshot["missions_total"] == 2
        assert snapshot["missions_learned"] == 2
        assert snapshot["decisions"]["completed"] == 1
        assert snapshot["decisions"]["cancelled"] == 1
        assert snapshot["expired"] == 1
        means = snapshot["dimension_means"]
        for name in (
            "plan_quality",
            "task_correctness",
            "verification_coverage",
            "resource_efficiency",
            "agent_performance",
        ):
            assert name in means
        assert means["task_correctness"] == 0.5
        roles = {entry["role"]: entry for entry in snapshot["roles"]}
        assert roles["frontend"]["tasks"] == 1
        assert roles["frontend"]["succeeded"] == 1
        assert roles["backend"]["failed"] == 1
        assert roles["qa"]["blocked"] == 1
        lesson_texts = {lesson["text"] for lesson in snapshot["lessons"]}
        assert any("exhausted dispatch attempts" in text for text in lesson_texts)
        assert any("missed its budget deadline" in text for text in lesson_texts)
        assert all(lesson["mission_id"] for lesson in snapshot["lessons"])

    def test_analytics_empty_state(self, stack: Stack) -> None:
        snapshot = stack.learning.analytics(owner_id=OWNER)
        assert snapshot["missions_total"] == 0
        assert snapshot["dimension_means"] == {}
        assert snapshot["roles"] == []
        assert snapshot["lessons"] == []

    def test_analytics_is_owner_scoped(self, stack: Stack) -> None:
        mission_id = stack.seed(owner_id=OWNER)
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        stack.learning.backfill(owner_id=OWNER)
        other = stack.learning.analytics(owner_id="owner-b")
        assert other["missions_total"] == 0


class TestLessonValidation:
    def test_duplicate_objectives_do_not_duplicate_lessons(self, stack: Stack) -> None:
        first = stack.seed(objective="Ship the recurring broken feature")
        stack.succeed_all(first, status="failed")
        stack.supervisor.finalize_mission(first, owner_id=OWNER)
        second = stack.seed(objective="Ship the recurring broken feature")
        stack.succeed_all(second, status="failed")
        stack.supervisor.finalize_mission(second, owner_id=OWNER)
        first_report = stack.learning.record(first, owner_id=OWNER)
        second_report = stack.learning.record(second, owner_id=OWNER)
        assert first_report.lessons_accepted >= 1
        assert second_report.lessons_accepted == 0
        lessons = stack.lessons.all(owner_id=OWNER)
        assert sum(1 for text in lessons if "failed verification checks" in text) == 1

    def test_lesson_texts_are_single_line_and_bounded(self, stack: Stack) -> None:
        mission_id = stack.seed(
            objective="Ship the feature " + "with a very long objective tail " * 6
        )
        stack.succeed_all(mission_id, status="failed")
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        stack.learning.record(mission_id, owner_id=OWNER)
        for text in stack.lessons.all(owner_id=OWNER):
            assert "\n" not in text
            assert len(text) <= 200


class TestCoordinatorIntegration:
    def test_coordinator_tick_processes_runs_and_missions(self, stack: Stack) -> None:
        mission_id = stack.seed(objective="Ship the coordinated feature")
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        coordinator = LearningCoordinator(
            stack.database, lessons=stack.lessons, missions=stack.learning
        )
        created = coordinator.enqueue(run_id="run-1", job_id="job-1", owner_id=OWNER)
        assert created
        report = coordinator.tick(owner_id=OWNER)
        assert report.processed == 1
        assert report.missions_processed == 1
        assert report.evaluations_recorded == 6
        assert stack.learning.has_learned(mission_id)

    def test_coordinator_stats_include_mission_counters(self, stack: Stack) -> None:
        mission_id = stack.seed(objective="Ship the counted feature")
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        coordinator = LearningCoordinator(
            stack.database, lessons=stack.lessons, missions=stack.learning
        )
        coordinator.tick(owner_id=OWNER)
        stats = coordinator.stats(owner_id=OWNER)
        assert stats["mission_terminal"] == 1
        assert stats["mission_learned"] == 1
        assert stats["mission_pending"] == 0

    def test_coordinator_without_missions_is_unchanged(self, stack: Stack) -> None:
        mission_id = stack.seed(objective="Ship the untouched feature")
        stack.succeed_all(mission_id)
        stack.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        coordinator = LearningCoordinator(stack.database, lessons=stack.lessons)
        report = coordinator.tick(owner_id=OWNER)
        assert report.missions_processed == 0
        assert not stack.learning.has_learned(mission_id)
        assert "mission_terminal" not in coordinator.stats(owner_id=OWNER)


class TestSupervisorIntegration:
    def test_learning_subsystem_learns_finalized_missions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        local = Stack(tmp_path)
        mission_id = local.seed(objective="Ship the supervised feature")
        local.succeed_all(mission_id)
        local.supervisor.finalize_mission(mission_id, owner_id=OWNER)
        coordinator = LearningCoordinator(
            local.database, lessons=local.lessons, missions=local.learning
        )
        supervisor = RuntimeSupervisor(
            local.automation,
            missions=local.progression,
            learning=coordinator,
            owner_id=OWNER,
        )
        try:
            supervisor.supervise()
            assert local.learning.has_learned(mission_id)
            snapshot = supervisor.health()
            assert snapshot["learning"]["mission_learned"] == 1
            assert snapshot["subsystems"]["learning"]["healthy"]
        finally:
            supervisor.close()

    async def test_progression_driven_exhaustion_produces_lesson(self, tmp_path: Path) -> None:
        local = Stack(tmp_path, fail_markers=("verify",))
        mission_id = local.seed(
            objective="Build a settings page and verify it",
            tasks=[
                {
                    "task_key": "ui",
                    "objective": "implement the settings page",
                    "preferred_role": "frontend",
                },
                {
                    "task_key": "verify",
                    "objective": "verify the settings page integration",
                    "depends_on": ["ui"],
                    "preferred_role": "qa",
                },
            ],
        )
        final = None
        for _ in range(16):
            mission = local.supervisor.missions.get(mission_id, owner_id=OWNER)
            assert mission is not None
            if mission.status in {
                "completed",
                "completed_unverified",
                "failed",
                "cancelled",
                "blocked",
            }:
                final = mission
                break
            local.progression.tick(owner_id=OWNER)
            await local.drain()
        assert final is not None
        assert final.status == "blocked"
        tasks = local.tasks_by_key(mission_id)
        assert tasks["verify"].status == "failed"
        assert tasks["verify"].attempt == 3
        report = local.learning.record(mission_id, owner_id=OWNER)
        assert report.processed == 1
        view = local.learning.learning_view(mission_id, owner_id=OWNER)
        assert view is not None
        texts = [lesson["text"] for lesson in view["lessons"]]
        assert any("exhausted dispatch attempts" in text for text in texts)


class TestConfigContract:
    def _write(self, tmp_path: Path, body: str) -> None:
        (tmp_path / "config.toml").write_text(
            '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n' + body,
            encoding="utf-8",
        )

    def test_mission_learning_defaults_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        self._write(tmp_path, "")
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        assert config.learning.mission_learning is True

    def test_mission_learning_can_be_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        self._write(tmp_path, "[learning]\nmission_learning = false\n")
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        assert config.learning.mission_learning is False

    def test_unknown_learning_key_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eurus.errors import ConfigError

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        self._write(tmp_path, "[learning]\nbogus = true\n")
        with pytest.raises(ConfigError):
            load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})


class TestBuildStackWiring:
    def test_build_stack_wires_mission_learning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eurus.cli.autonomy import build_stack
        from eurus.config import Config

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        built = build_stack(Config(), workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert built.learning is not None
            assert built.learning.missions is not None
        finally:
            built.close()

    def test_build_stack_respects_mission_learning_toggle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eurus.cli.autonomy import build_stack
        from eurus.config import Config, LearningConfig

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = Config()
        config = replace(config, learning=replace(LearningConfig(), mission_learning=False))
        built = build_stack(config, workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert built.learning is not None
            assert built.learning.missions is None
        finally:
            built.close()

    def test_build_stack_skips_mission_learning_when_agentic_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eurus.cli.autonomy import build_stack
        from eurus.config import AgenticEngineeringConfig, Config

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = Config()
        config = replace(
            config, agentic_engineering=replace(AgenticEngineeringConfig(), enabled=False)
        )
        built = build_stack(config, workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert built.missions is None
            assert built.learning is not None
            assert built.learning.missions is None
        finally:
            built.close()


class TestGatewayApi:
    AUTH = {"Authorization": "Bearer unit-test-token"}

    @pytest.fixture()
    def gateway(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from eurus.gateway.web.app import build_context, create_app
        from eurus.gateway.web.auth import GatewaySecurity

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        (tmp_path / "config.toml").write_text(
            '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
        ctx = build_context(config, security=security, approval_timeout=60)
        app = create_app(ctx)
        with TestClient(app) as test_client:
            yield test_client
        asyncio.run(ctx.runtime.stop())

    def _force_learned(self, mission_id: str, *, owner_id: str) -> None:
        database = Database(paths.database_path())
        tasks = TaskGraphStore(database)
        acceptance = AcceptanceService(database)
        for task in tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status != "succeeded":
                tasks.update_status(task.task_id, "succeeded", owner_id=owner_id)
            acceptance.record_verification(
                mission_id=mission_id,
                task_id=task.task_id,
                owner_id=owner_id,
                check_name="pytest",
                status="passed",
                exit_code=0,
            )
        MissionStore(database).update(mission_id, owner_id=owner_id, status="completed")

    def test_mission_learning_endpoint_round_trip(self, gateway: TestClient) -> None:
        created = gateway.post(
            "/api/missions",
            json={"objective": "Ship the gateway feature", "tasks": TASKS_TWO},
            headers=self.AUTH,
        )
        assert created.status_code == 201, created.text
        mission_id = created.json()["mission_id"]
        before = gateway.get(f"/api/missions/{mission_id}/learning", headers=self.AUTH)
        assert before.status_code == 200, before.text
        assert before.json()["learned"] is False

        self._force_learned(mission_id, owner_id="eurus")
        MissionLearning(paths.database_path()).record(mission_id, owner_id="eurus")

        after = gateway.get(f"/api/missions/{mission_id}/learning", headers=self.AUTH)
        assert after.status_code == 200, after.text
        payload = after.json()
        assert payload["learned"] is True
        assert payload["status"] == "completed"
        assert len(payload["evaluations"]) == 5

    def test_mission_analytics_endpoint(self, gateway: TestClient) -> None:
        snapshot = gateway.get("/api/missions/analytics", headers=self.AUTH)
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["missions_total"] == 0
        assert body["decisions"] == {
            "blocked": 0,
            "cancelled": 0,
            "completed": 0,
            "completed_unverified": 0,
            "failed": 0,
        }
        assert body["roles"] == []
        assert "expired" in body

    def test_mission_learning_unknown_mission_is_404(self, gateway: TestClient) -> None:
        missing = gateway.get("/api/missions/missing/learning", headers=self.AUTH)
        assert missing.status_code == 404

    def test_mission_surfaces_reject_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eurus.gateway.web.app import build_context, create_app
        from eurus.gateway.web.auth import GatewaySecurity

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        (tmp_path / "config.toml").write_text(
            '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n\n'
            "[agentic_engineering]\nenabled = false\n",
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
        ctx = build_context(config, security=security, approval_timeout=60)
        app = create_app(ctx)
        with TestClient(app) as client:
            analytics = client.get("/api/missions/analytics", headers=self.AUTH)
            assert analytics.status_code == 422
            assert "disabled by configuration" in analytics.json()["error"]["detail"]
        asyncio.run(ctx.runtime.stop())


class TestCli:
    def test_cli_learning_and_analytics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        created = CliRunner().invoke(
            main,
            [
                "missions",
                "create",
                "Ship the CLI feature",
                "--task",
                '{"task_key": "ui", "objective": "build the page", "preferred_role": "frontend"}',
            ],
            env={"EURUS_HOME": str(tmp_path)},
        )
        assert created.exit_code == 0, created.output
        mission_id = str(created.output).split()[1]

        database = Database(paths.database_path())
        tasks = TaskGraphStore(database)
        acceptance = AcceptanceService(database)
        for task in tasks.list_tasks(mission_id, owner_id="local"):
            if task.status != "succeeded":
                tasks.update_status(task.task_id, "succeeded", owner_id="local")
            acceptance.record_verification(
                mission_id=mission_id,
                task_id=task.task_id,
                owner_id="local",
                check_name="pytest",
                status="passed",
                exit_code=0,
            )
        MissionStore(database).update(mission_id, owner_id="local", status="completed")
        MissionLearning(paths.database_path()).record(mission_id, owner_id="local")

        learning = CliRunner().invoke(
            main, ["missions", "learning", mission_id], env={"EURUS_HOME": str(tmp_path)}
        )
        assert learning.exit_code == 0, learning.output
        assert "learned=true" in learning.output
        assert "plan_quality" in learning.output

        analytics = CliRunner().invoke(
            main, ["missions", "analytics"], env={"EURUS_HOME": str(tmp_path)}
        )
        assert analytics.exit_code == 0, analytics.output
        assert "missions=1" in analytics.output
        assert "frontend" in analytics.output

    def test_cli_learning_unknown_mission_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        result = CliRunner().invoke(
            main, ["missions", "learning", "missing"], env={"EURUS_HOME": str(tmp_path)}
        )
        assert result.exit_code != 0
