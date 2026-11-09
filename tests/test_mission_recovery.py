from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.multiagent.artifacts import ArtifactStore
from eurus.multiagent.contracts import ReviewFinding, VerificationEvidence
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.multiagent.supervision import MissionSupervision
from eurus.storage.database import Database

OWNER = "owner-a"


class RecordingExecutor:
    async def execute(self, job, context) -> str:
        return "ok"


def _build_stack(database_path: Path) -> tuple[MainSupervisor, AgentOrchestrator, Database]:
    database = Database(database_path)
    automation = AutomationEngine(
        database,
        RecordingExecutor(),
        retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
        poll_seconds=0.05,
    )
    orchestrator = AgentOrchestrator(database, automation)
    supervisor = MainSupervisor(database, orchestrator)
    return supervisor, orchestrator, database


def _seed_mission(database_path: Path) -> tuple[str, MainSupervisor, AgentOrchestrator]:
    supervisor, orchestrator, database = _build_stack(database_path)
    registry = SpecialistRegistry(database)
    registry.register("frontend", allowed_tools=["read_file"])
    registry.register("qa", allowed_tools=["read_file"])
    mission = supervisor.create_mission(
        "Recoverable feature",
        owner_id=OWNER,
        acceptance_criteria=["evidence recorded"],
    )
    supervisor.plan_mission(
        mission.mission_id,
        owner_id=OWNER,
        tasks=[
            {"task_key": "ui", "objective": "settings page", "preferred_role": "frontend"},
            {
                "task_key": "verify",
                "objective": "verify",
                "depends_on": ["ui"],
                "preferred_role": "qa",
            },
        ],
    )
    supervisor.start_mission(mission.mission_id, owner_id=OWNER)
    return mission.mission_id, supervisor, orchestrator


class TestRestartRecovery:
    def test_completed_work_survives_restart_without_duplicates(self, tmp_path: Path) -> None:
        database_path = tmp_path / "eurus.sqlite3"
        mission_id, supervisor, orchestrator = _seed_mission(database_path)
        supervisor.dispatch_ready(mission_id, owner_id=OWNER, limit=5)
        tasks = {
            task.task_key: task for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        ui_task = tasks["ui"]
        orchestrator.store.update(
            ui_task.assignment_id, status="succeeded", result="patched", owner_id=OWNER
        )
        supervisor.tasks.update_status(ui_task.task_id, "succeeded", owner_id=OWNER)

        restarted, _, _ = _build_stack(database_path)
        resumed = restarted.resume_mission(mission_id, owner_id=OWNER)
        assert resumed.status == "running"

        after = {
            task.task_key: task for task in restarted.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert after["ui"].status == "succeeded", "completed work is never re-dispatched"
        assert after["verify"].status == "ready", "unlocked dependent work is queued again"

        assignments_before = len(
            orchestrator.store.list_assignments(owner_id=OWNER, mission_id=mission_id, limit=100)
        )
        restarted.dispatch_ready(mission_id, owner_id=OWNER)
        assignments_after = len(
            orchestrator.store.list_assignments(owner_id=OWNER, mission_id=mission_id, limit=100)
        )
        assert assignments_after == assignments_before + 1, (
            "exactly one new assignment (verify); no duplicate ui work"
        )

    def test_artifacts_and_evidence_survive_restart(self, tmp_path: Path) -> None:
        database_path = tmp_path / "eurus.sqlite3"
        mission_id, supervisor, _ = _seed_mission(database_path)
        artifacts = ArtifactStore(database_path, tmp_path / "artifacts")
        record = artifacts.publish(
            "patch payload",
            owner_id=OWNER,
            mission_id=mission_id,
            artifact_type="patch",
        )
        task = supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)[0]
        supervisor.record_verification(
            mission_id,
            task.task_id,
            owner_id=OWNER,
            check_name="pytest",
            status="passed",
            exit_code=0,
        )

        restarted, _, _ = _build_stack(database_path)
        assert restarted.missions.get(mission_id, owner_id=OWNER) is not None
        assert artifacts.read(record.artifact_id, owner_id=OWNER) == b"patch payload"
        evidence = [
            item
            for item in _verifications(database_path, mission_id)
            if item.check_name == "pytest"
        ]
        assert evidence and evidence[0].passed

    def test_supervision_after_restart_requeues_orphaned_work(self, tmp_path: Path) -> None:
        database_path = tmp_path / "eurus.sqlite3"
        mission_id, _, _ = _seed_mission(database_path)

        first_stack, _, _ = _build_stack(database_path)
        first_stack.dispatch_ready(mission_id, owner_id=OWNER)
        tasks = first_stack.tasks.list_tasks(mission_id, owner_id=OWNER)
        assert any(task.status == "dispatched" for task in tasks)
        stale_timestamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with Database(database_path).connection() as connection:
            connection.execute(
                "UPDATE eurus_agent_assignments SET updated_at=? WHERE mission_id=?",
                (stale_timestamp, mission_id),
            )
            connection.execute(
                "UPDATE eurus_agent_heartbeats SET last_beat=? WHERE assignment_id IN "
                "(SELECT assignment_id FROM eurus_agent_assignments WHERE mission_id=?)",
                (stale_timestamp, mission_id),
            )

        restarted, restarted_orchestrator, _ = _build_stack(database_path)
        report = MissionSupervision(
            database_path, restarted_orchestrator, stale_after_seconds=900
        ).tick(mission_id, owner_id=OWNER)
        assert report.resynced_tasks, "orphaned dispatched tasks are requeued"
        states = {
            task.task_key: task.status
            for task in restarted.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert states["ui"] == "ready"
        assert states["verify"] == "pending"

    def test_review_cycle_state_survives_restart(self, tmp_path: Path) -> None:
        database_path = tmp_path / "eurus.sqlite3"
        mission_id, supervisor, _ = _seed_mission(database_path)
        task = supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)[0]
        supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=OWNER)
        supervisor.record_verification(
            mission_id, task.task_id, owner_id=OWNER, check_name="pytest", status="passed"
        )
        loop = ReviewLoop(supervisor.database, max_cycles=3)
        outcome = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task.task_id,
            reviewer_assignment_id="rev",
            status="changes_requested",
            findings=[ReviewFinding(finding_id="f1", severity="major", category="correctness")],
        )
        assert outcome.revision_task_id is not None

        restarted, _, _ = _build_stack(database_path)
        decision = restarted.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "blocked"
        revision = restarted.tasks.get(outcome.revision_task_id, owner_id=OWNER)
        assert revision is not None and revision.status == "pending"

    def test_deadline_expiry_cancels_after_restart(self, tmp_path: Path) -> None:
        database_path = tmp_path / "eurus.sqlite3"
        mission_id, _, orchestrator = _seed_mission(database_path)

        restarted, restarted_orchestrator, _ = _build_stack(database_path)
        report = MissionSupervision(database_path, restarted_orchestrator).tick(
            mission_id,
            owner_id=OWNER,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert report.deadline_expired
        mission = restarted.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None
        states = {
            task.task_key: task.status
            for task in restarted.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert set(states.values()) == {"cancelled"}


def _verifications(database_path: Path, mission_id: str) -> list:
    from eurus.agent.acceptance import VerificationStore

    store = VerificationStore(Database(database_path))
    evidence = store.list_for_mission(mission_id, owner_id=OWNER)
    for item in evidence:
        assert isinstance(item, VerificationEvidence)
    return evidence


@pytest.mark.parametrize(
    ("objective", "expected_route"),
    [
        ("fix a typo", "direct"),
        ("build a multi-tenant dashboard feature", "hierarchical"),
    ],
)
def test_route_survives_restart_parametrized(
    tmp_path: Path, objective: str, expected_route: str
) -> None:
    database_path = tmp_path / "eurus.sqlite3"
    supervisor, _, _ = _build_stack(database_path)
    mission = supervisor.create_mission(objective, owner_id=OWNER)
    restarted, _, _ = _build_stack(database_path)
    assert restarted.decide_route(mission.objective) == expected_route
