from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.agent.mission_runtime import MissionProgression, deadline_from_budget
from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.storage.database import Database

OWNER = "owner-a"


class SelectiveExecutor:
    def __init__(self, fail_markers: tuple[str, ...] = ()) -> None:
        self.executed: list[str] = []
        self.fail_markers = fail_markers

    async def execute(self, job: Any, context: Any) -> str:
        self.executed.append(job.objective)
        if any(marker in job.objective for marker in self.fail_markers):
            raise RuntimeError(f"intentional failure: {job.objective}")
        return "done"


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def __call__(self, event_type: str, subject_type: str, subject_id: str, payload: Any) -> None:
        self.events.append((event_type, subject_type, subject_id, dict(payload)))

    def of_type(self, event_type: str) -> list[tuple[str, str, str, dict[str, Any]]]:
        return [event for event in self.events if event[0] == event_type]


class Stack:
    def __init__(
        self,
        tmp_path: Path,
        *,
        fail_markers: tuple[str, ...] = (),
        events: EventRecorder | None = None,
        hierarchical_dispatch: bool = True,
        max_task_attempts: int = 3,
        stale_after_seconds: float = 900.0,
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
        self.progression = MissionProgression(
            self.supervisor,
            review=ReviewLoop(self.database, max_cycles=3),
            hierarchical_dispatch=hierarchical_dispatch,
            max_task_attempts=max_task_attempts,
            stale_after_seconds=stale_after_seconds,
            event_publisher=events,
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


@pytest.fixture()
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


def _seed_mission(
    stack: Stack, *, owner_id: str = OWNER, budget: dict[str, Any] | None = None
) -> str:
    mission = stack.supervisor.create_mission(
        "Build a settings page and verify it",
        owner_id=owner_id,
        acceptance_criteria=["evidence recorded"],
        budget=budget,
    )
    stack.supervisor.plan_mission(
        mission.mission_id,
        owner_id=owner_id,
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
    stack.supervisor.start_mission(mission.mission_id, owner_id=owner_id)
    return mission.mission_id


def _record_evidence(stack: Stack, mission_id: str, check_name: str = "pytest") -> None:
    for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
        stack.supervisor.record_verification(
            mission_id,
            task.task_id,
            owner_id=OWNER,
            check_name=check_name,
            status="passed",
            exit_code=0,
        )


def _terminal_statuses() -> set[str]:
    return {"completed", "completed_unverified", "failed", "cancelled", "blocked"}


class TestMissionProgressionTick:
    async def test_tick_dispatches_ready_tasks(self, stack: Stack) -> None:
        mission_id = _seed_mission(stack)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.missions_checked == 1
        assert report.dispatched, "ready tasks are dispatched through the orchestrator"
        tasks = {
            task.task_key: task
            for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert tasks["ui"].status == "dispatched"
        assert tasks["verify"].status == "pending"
        assert (
            len(
                stack.orchestrator.store.list_assignments(
                    owner_id=OWNER, mission_id=mission_id, limit=10
                )
            )
            == 1
        )

    def test_tick_ignores_missions_outside_running_status(self, stack: Stack) -> None:
        mission = stack.supervisor.create_mission("Idle work", owner_id=OWNER)
        stack.supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[{"task_key": "a", "objective": "a"}],
        )
        report = stack.progression.tick(owner_id=OWNER)
        assert report.missions_checked == 0
        assert report.dispatched == []
        assert stack.supervisor.missions.get(mission.mission_id, owner_id=OWNER) is not None

    async def test_verified_mission_completes_end_to_end(self, tmp_path: Path) -> None:
        events = EventRecorder()
        stack = Stack(tmp_path, events=events)
        mission_id = _seed_mission(stack)
        _record_evidence(stack, mission_id)
        final = None
        for _ in range(12):
            mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
            assert mission is not None
            if mission.status in _terminal_statuses():
                final = mission
                break
            stack.progression.tick(owner_id=OWNER)
            await stack.drain()
        assert final is not None, "mission must reach a terminal state"
        assert final.status == "completed"
        tasks = stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        assert all(task.status == "succeeded" for task in tasks)
        finalized = events.of_type("MISSION_FINALIZED")
        assert len(finalized) == 1
        assert finalized[0][1] == "mission"
        assert finalized[0][3]["decision"] == "completed"

    async def test_unverified_mission_completes_without_evidence(self, stack: Stack) -> None:
        mission_id = _seed_mission(stack)
        final = None
        for _ in range(12):
            mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
            assert mission is not None
            if mission.status in _terminal_statuses():
                final = mission
                break
            stack.progression.tick(owner_id=OWNER)
            await stack.drain()
        assert final is not None
        assert final.status == "completed_unverified"
        assert final.completed_at is not None

    async def test_failing_task_exhausts_attempts_and_blocks_mission(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, fail_markers=("verify",))
        mission_id = _seed_mission(stack)
        exhausted_seen = False
        final = None
        for _ in range(16):
            mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
            assert mission is not None
            if mission.status in _terminal_statuses():
                final = mission
                break
            report = stack.progression.tick(owner_id=OWNER)
            exhausted_seen = exhausted_seen or bool(report.exhausted)
            await stack.drain()
        assert final is not None
        assert final.status == "blocked"
        assert exhausted_seen, "attempt exhaustion must be reported"
        tasks = {
            task.task_key: task
            for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert tasks["ui"].status == "succeeded"
        assert tasks["verify"].status == "failed"
        assert tasks["verify"].attempt == 3
        assignments = stack.orchestrator.store.list_assignments(
            owner_id=OWNER, mission_id=mission_id, limit=10
        )
        verify_assignments = [
            item for item in assignments if item.task_id == tasks["verify"].task_id
        ]
        assert len(verify_assignments) == 3, "attempts are bounded by max_task_attempts"

    async def test_deadline_budget_expires_mission(self, tmp_path: Path) -> None:
        events = EventRecorder()
        stack = Stack(tmp_path, events=events)
        budget = {"deadline": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()}
        mission_id = _seed_mission(stack, budget=budget)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.expired == [mission_id]
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None
        assert mission.status == "cancelled"
        tasks = stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        assert all(task.status == "cancelled" for task in tasks)
        expired = events.of_type("MISSION_EXPIRED")
        assert len(expired) == 1
        assert expired[0][3]["deadline"] is not None

    async def test_future_deadline_does_not_expire_mission(self, stack: Stack) -> None:
        budget = {"deadline": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
        mission_id = _seed_mission(stack, budget=budget)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.expired == []
        assert report.dispatched, "progression continues while the deadline is ahead"
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None
        assert mission.status == "running"

    async def test_hierarchical_dispatch_disabled_defers_dispatch(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, hierarchical_dispatch=False)
        mission_id = _seed_mission(stack)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.dispatched == []
        tasks = {
            task.task_key: task
            for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert tasks["ui"].status == "ready"
        assert (
            stack.orchestrator.store.list_assignments(
                owner_id=OWNER, mission_id=mission_id, limit=10
            )
            == []
        )

    async def test_dispatch_disabled_mission_can_still_finalize(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path, hierarchical_dispatch=False)
        mission_id = _seed_mission(stack)
        for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
            stack.supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=OWNER)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.finalized and report.finalized[0]["decision"] == "completed_unverified"
        mission = stack.supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None
        assert mission.status == "completed_unverified"

    async def test_owner_scoping_only_progresses_own_missions(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path)
        mine = _seed_mission(stack, owner_id=OWNER)
        theirs = _seed_mission(stack, owner_id="owner-b")
        report = stack.progression.tick(owner_id=OWNER)
        assert report.missions_checked == 1
        mine_tasks = {
            task.task_key: task for task in stack.supervisor.tasks.list_tasks(mine, owner_id=OWNER)
        }
        assert mine_tasks["ui"].status == "dispatched"
        assert mine_tasks["verify"].status == "pending"
        their_tasks = stack.supervisor.tasks.list_tasks(theirs, owner_id="owner-b")
        assert all(task.status in {"pending", "ready"} for task in their_tasks)
        assert (
            stack.orchestrator.store.list_assignments(
                owner_id="owner-b", mission_id=theirs, limit=10
            )
            == []
        )

    async def test_stale_assignment_is_requeued_and_redispatched(self, stack: Stack) -> None:
        mission_id = _seed_mission(stack)
        stack.progression.tick(owner_id=OWNER)
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with stack.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_agent_assignments SET updated_at=? WHERE mission_id=?",
                (stale, mission_id),
            )
            connection.execute(
                "UPDATE eurus_agent_heartbeats SET last_beat=? WHERE assignment_id IN "
                "(SELECT assignment_id FROM eurus_agent_assignments WHERE mission_id=?)",
                (stale, mission_id),
            )
        report = stack.progression.tick(owner_id=OWNER)
        tasks = {
            task.task_key: task
            for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert tasks["ui"].status == "dispatched"
        assert tasks["ui"].attempt == 2
        assert report.dispatched

    async def test_tick_isolates_per_mission_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stack = Stack(tmp_path)
        good = _seed_mission(stack)
        broken = stack.supervisor.create_mission("Broken mission", owner_id=OWNER)
        stack.supervisor.plan_mission(
            broken.mission_id,
            owner_id=OWNER,
            tasks=[{"task_key": "x", "objective": "x"}],
        )
        stack.supervisor.start_mission(broken.mission_id, owner_id=OWNER)
        original = stack.progression.supervision.tick

        def failing_tick(mission_id: str, **kwargs: Any) -> Any:
            if mission_id == broken.mission_id:
                raise RuntimeError("supervision exploded")
            return original(mission_id, **kwargs)

        monkeypatch.setattr(stack.progression.supervision, "tick", failing_tick)
        report = stack.progression.tick(owner_id=OWNER)
        assert report.missions_checked == 2
        assert report.dispatched, "the healthy mission still progresses"
        assert any("progression failed" in note for note in report.notes)
        good_mission = stack.supervisor.missions.get(good, owner_id=OWNER)
        assert good_mission is not None
        assert good_mission.status == "running"

    def test_stats_reports_running_missions_and_active_tasks(self, stack: Stack) -> None:
        mission_id = _seed_mission(stack)
        stats = stack.progression.stats(owner_id=OWNER)
        assert stats == {"running": 1, "active_tasks": 2}
        stack.progression.tick(owner_id=OWNER)
        stats = stack.progression.stats(owner_id=OWNER)
        assert stats["running"] == 1
        assert stats["active_tasks"] == 2
        for task in stack.supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
            stack.supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=OWNER)
        stack.progression.tick(owner_id=OWNER)
        stats = stack.progression.stats(owner_id=OWNER)
        assert stats == {"running": 0, "active_tasks": 0}


class TestRuntimeSupervisorIntegration:
    def test_supervisor_runs_missions_subsystem(self, stack: Stack) -> None:
        from eurus.automation.supervision import SUBSYSTEM_NAMES, RuntimeSupervisor

        assert "missions" in SUBSYSTEM_NAMES
        _seed_mission(stack)
        supervisor = RuntimeSupervisor(
            stack.automation,
            assignments=stack.orchestrator,
            missions=stack.progression,
            owner_id=OWNER,
        )
        try:
            report = supervisor.supervise()
            assert report.missions_ticked == 1
            assert report.mission_tasks_dispatched == 1
            assert report.missions_finalized == 0
            assert "missions" not in report.subsystem_errors
            health = supervisor.health()
            assert health["subsystems"]["missions"]["healthy"] is True
            assert health["missions"] == {"running": 1, "active_tasks": 2}
        finally:
            supervisor.close()

    def test_missions_subsystem_backs_off_after_failure(self, stack: Stack) -> None:
        from eurus.automation.supervision import RuntimeSupervisor

        class ExplodingTicker:
            def __init__(self) -> None:
                self.calls = 0

            def tick(self, **kwargs: Any) -> Any:
                self.calls += 1
                raise RuntimeError("mission tick exploded")

            def stats(self, **kwargs: Any) -> dict[str, int]:
                return {"running": 0, "active_tasks": 0}

        ticker = ExplodingTicker()
        supervisor = RuntimeSupervisor(
            stack.automation,
            missions=ticker,
            owner_id=OWNER,
        )
        try:
            first = supervisor.supervise()
            assert "missions" in first.subsystem_errors
            assert "mission tick exploded" in first.subsystem_errors["missions"]
            second = supervisor.supervise()
            assert "missions" not in second.subsystem_errors
            assert ticker.calls == 1
            assert any("missions skipped" in note for note in second.notes)
        finally:
            supervisor.close()

    async def test_supervise_async_runs_missions_subsystem_off_loop(self, stack: Stack) -> None:
        from eurus.automation.supervision import RuntimeSupervisor

        _seed_mission(stack)
        supervisor = RuntimeSupervisor(
            stack.automation,
            assignments=stack.orchestrator,
            missions=stack.progression,
            owner_id=OWNER,
        )
        try:
            report = await supervisor.supervise_async()
            assert report.missions_ticked == 1
            assert report.mission_tasks_dispatched == 1
            assert "missions" not in report.subsystem_errors
        finally:
            supervisor.close()

    def test_disabled_missions_subsystem_is_skipped(self, stack: Stack) -> None:
        from eurus.automation.supervision import RuntimeSupervisor

        _seed_mission(stack)
        supervisor = RuntimeSupervisor(
            stack.automation,
            missions=None,
            owner_id=OWNER,
        )
        try:
            report = supervisor.supervise()
            assert report.missions_ticked == 0
            health = supervisor.health()
            assert "missions" not in health
        finally:
            supervisor.close()


class TestDeadlineParsing:
    def test_parses_iso_string(self) -> None:
        value = datetime.now(UTC) + timedelta(hours=1)
        parsed = deadline_from_budget({"deadline": value.isoformat()})
        assert parsed is not None
        assert parsed.tzinfo is not None

    def test_parses_datetime_object(self) -> None:
        value = datetime.now(UTC) + timedelta(hours=1)
        assert deadline_from_budget({"deadline": value}) == value

    def test_accepts_deadline_at_alias(self) -> None:
        value = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert deadline_from_budget({"deadline_at": value}) is not None

    def test_naive_datetime_is_assumed_utc(self) -> None:
        value = datetime(2026, 1, 1, 12, 0, 0)
        parsed = deadline_from_budget({"deadline": value})
        assert parsed is not None
        assert parsed.tzinfo is not None

    def test_invalid_values_are_ignored(self) -> None:
        assert deadline_from_budget({}) is None
        assert deadline_from_budget({"deadline": ""}) is None
        assert deadline_from_budget({"deadline": "not-a-date"}) is None
        assert deadline_from_budget({"deadline": 12345}) is None


class TestMissionProgressionContract:
    def test_constructor_rejects_out_of_range_bounds(self, tmp_path: Path) -> None:
        stack = Stack(tmp_path)
        with pytest.raises(ValueError, match="dispatch_batch_limit"):
            MissionProgression(stack.supervisor, dispatch_batch_limit=0)
        with pytest.raises(ValueError, match="max_missions_per_tick"):
            MissionProgression(stack.supervisor, max_missions_per_tick=0)
        with pytest.raises(ValueError, match="max_task_attempts"):
            MissionProgression(stack.supervisor, max_task_attempts=0)

    def test_report_to_dict_roundtrip(self) -> None:
        from eurus.agent.mission_runtime import MissionProgressionReport

        report = MissionProgressionReport(
            missions_checked=2,
            dispatched=["a"],
            tasks_succeeded=["t1"],
            tasks_blocked=["t2"],
            finalized=[{"mission_id": "m", "decision": "completed", "verified": True}],
            expired=["m2"],
            exhausted=["t3"],
            notes=["note"],
        )
        view = report.to_dict()
        assert view["missions_checked"] == 2
        assert view["dispatched"] == ["a"]
        assert view["finalized"][0]["decision"] == "completed"
        assert view["expired"] == ["m2"]
        assert view["exhausted"] == ["t3"]


class TestBuildStackWiring:
    def test_build_stack_wires_mission_progression(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        from eurus.cli.autonomy import build_stack
        from eurus.config import Config

        stack = build_stack(Config(), workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert stack.missions is not None
            assert stack.supervisor.missions is stack.missions
            assert stack.missions.hierarchical_dispatch is True
            assert stack.missions.dispatch_batch_limit == 5
        finally:
            stack.close()

    def test_build_stack_skips_progression_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        from eurus.cli.autonomy import build_stack
        from eurus.config import AgenticEngineeringConfig, Config

        config = Config()
        disabled = replace(
            config,
            agentic_engineering=replace(AgenticEngineeringConfig(), autonomous_progression=False),
        )
        stack = build_stack(disabled, workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert stack.missions is None
            assert stack.supervisor.missions is None
        finally:
            stack.close()

    def test_build_stack_respects_dispatch_kill_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        from eurus.cli.autonomy import build_stack
        from eurus.config import AgenticEngineeringConfig, Config

        config = Config()
        gated = replace(
            config,
            agentic_engineering=replace(AgenticEngineeringConfig(), hierarchical_dispatch=False),
        )
        stack = build_stack(gated, workers=1, poll_seconds=0.05, owner_id="alice")
        try:
            assert stack.missions is not None
            assert stack.missions.hierarchical_dispatch is False
        finally:
            stack.close()


class TestAutonomyServiceToggle:
    def test_specialists_toggle_controls_assignments_and_missions(self) -> None:
        from eurus.autonomy_service import SUPERVISOR_SUBSYSTEM_MAP

        assert SUPERVISOR_SUBSYSTEM_MAP["specialists"] == ("assignments", "missions")
        assert "missions" in SUPERVISOR_SUBSYSTEM_MAP["specialists"]
