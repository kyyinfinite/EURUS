from __future__ import annotations

from pathlib import Path

import pytest

from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.multiagent.artifacts import ArtifactError, ArtifactPermissionError, ArtifactStore
from eurus.multiagent.contracts import ReviewFinding, TaskSpec, VerificationEvidence
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.multiagent.task_graph import TaskGraphError, TaskGraphStore
from eurus.storage.database import Database


class RecordingExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job, context) -> str:
        payload = job.payload or {}
        self.executed.append(str(payload.get("role", "unknown")))
        return f"done by {payload.get('role', 'unknown')}"


@pytest.fixture()
def env(tmp_path: Path):
    database = Database(tmp_path / "agents.sqlite3")
    executor = RecordingExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, automation)
    registry = SpecialistRegistry(database)
    registry.register("frontend", allowed_tools=["read_file", "create_file"])
    registry.register("backend", allowed_tools=["read_file"])
    registry.register("qa", allowed_tools=["read_file"])
    supervisor = MainSupervisor(database, orchestrator)
    return supervisor, orchestrator, database, automation, executor


OWNER = "owner-a"


def _plan_two_task_dag(supervisor: MainSupervisor, *, reverse: bool = False) -> str:
    mission = supervisor.create_mission(
        "Build a settings page and its API",
        owner_id=OWNER,
        acceptance_criteria=["tests pass"],
    )
    supervisor.plan_mission(
        mission.mission_id,
        owner_id=OWNER,
        tasks=[
            {
                "task_key": "ui",
                "title": "Settings page",
                "objective": "implement the settings page",
                "preferred_role": "frontend",
                "acceptance_criteria": ["page renders"],
            },
            {
                "task_key": "api",
                "title": "Settings API",
                "objective": "implement the settings API",
                "preferred_role": "backend",
            },
            {
                "task_key": "verify",
                "title": "Verify integration",
                "objective": "verify the page against the API",
                "preferred_role": "qa",
                "depends_on": ["ui", "api"],
            },
        ],
    )
    return mission.mission_id


class TestMissionLifecycle:
    def test_mission_create_persist_get(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Ship the feature", owner_id=OWNER)
        fetched = supervisor.missions.get(mission.mission_id, owner_id=OWNER)
        assert fetched is not None
        assert fetched.status == "created"
        assert fetched.objective == "Ship the feature"

    def test_owner_isolation(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Secret plan", owner_id=OWNER)
        assert supervisor.missions.get(mission.mission_id, owner_id="owner-b") is None
        with pytest.raises(KeyError):
            supervisor.tasks.list_tasks(mission.mission_id, owner_id="owner-b")
        with pytest.raises(KeyError):
            supervisor.cancel_mission(mission.mission_id, owner_id="owner-b")

    def test_plan_and_start_moves_status(self, env) -> None:
        supervisor, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        mission = supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "planned"
        tasks = supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        assert {task.task_key for task in tasks} == {"ui", "api", "verify"}
        started = supervisor.start_mission(mission_id, owner_id=OWNER)
        assert started.status == "running"
        ready = {task.task_key for task in supervisor.tasks.ready_tasks(mission_id, owner_id=OWNER)}
        assert ready == {"ui", "api"}, "independent tasks become ready; dependent waits"

    def test_plan_rejects_cycles(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Cyclic work", owner_id=OWNER)
        with pytest.raises(TaskGraphError, match="cycle|unknown tasks"):
            supervisor.plan_mission(
                mission.mission_id,
                owner_id=OWNER,
                tasks=[
                    {"task_key": "a", "objective": "a", "depends_on": ["b"]},
                    {"task_key": "b", "objective": "b", "depends_on": ["a"]},
                ],
            )

    def test_plan_rejects_missing_dependency(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Dangling dep", owner_id=OWNER)
        with pytest.raises(ValueError, match="unknown tasks"):
            supervisor.plan_mission(
                mission.mission_id,
                owner_id=OWNER,
                tasks=[{"task_key": "a", "objective": "a", "depends_on": ["ghost"]}],
            )

    def test_ready_queue_is_deterministic(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Determinism", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[
                {"task_key": "zulu", "objective": "z", "priority": 5},
                {"task_key": "alpha", "objective": "a", "priority": 5},
                {"task_key": "urgent", "objective": "u", "priority": 1},
            ],
        )
        supervisor.start_mission(mission.mission_id, owner_id=OWNER)
        ready = supervisor.tasks.ready_tasks(mission.mission_id, owner_id=OWNER)
        assert [task.task_key for task in ready] == ["urgent", "alpha", "zulu"]

    def test_cancel_propagates_to_tasks_and_assignments(self, env) -> None:
        supervisor, orchestrator, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        supervisor.start_mission(mission_id, owner_id=OWNER)
        supervisor.dispatch_ready(mission_id, owner_id=OWNER)
        tasks = {
            task.task_key: task for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert tasks["ui"].status == "dispatched"
        assert tasks["api"].status == "dispatched"
        assert tasks["verify"].status == "pending", "dependent task is not dispatched early"
        assert tasks["ui"].assignment_id and tasks["api"].assignment_id
        supervisor.cancel_mission(mission_id, owner_id=OWNER, reason="user stop")
        mission = supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "cancelled"
        for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
            assert task.status == "cancelled"
            if task.assignment_id:
                assignment = orchestrator.store.get(task.assignment_id)
                assert assignment is not None and assignment.status == "cancelled"

    def test_mission_report_shape(self, env) -> None:
        supervisor, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        report = supervisor.mission_report(mission_id, owner_id=OWNER)
        assert report["mission"]["mission_id"] == mission_id
        assert len(report["tasks"]) == 3
        assert report["aggregation"]["total"] == 0

    def test_route_decision(self, env) -> None:
        supervisor, *_ = env
        assert supervisor.decide_route("fix a typo in README") == "direct"
        assert supervisor.decide_route("build a multi-tenant dashboard feature") == "hierarchical"
        assert supervisor.decide_route("anything", explicit="direct") == "direct"
        with pytest.raises(ValueError):
            supervisor.decide_route("anything", explicit="chaos")


class TestDependencyScheduling:
    def test_dependent_task_waits_for_predecessors(self, env) -> None:
        supervisor, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        supervisor.start_mission(mission_id, owner_id=OWNER)
        tasks = {
            task.task_key: task for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        verify = tasks["verify"]
        assert verify.status == "pending"
        for key in ("ui", "api"):
            supervisor.tasks.update_status(tasks[key].task_id, "succeeded", owner_id=OWNER)
        supervisor.on_task_succeeded(mission_id, tasks["ui"].task_id, owner_id=OWNER)
        verify_after = supervisor.tasks.get(verify.task_id, owner_id=OWNER)
        assert verify_after is not None and verify_after.status == "ready"

    def test_predecessor_failure_blocks_dependents(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Failure blocks", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[
                {"task_key": "first", "objective": "first"},
                {"task_key": "second", "objective": "second", "depends_on": ["first"]},
            ],
        )
        supervisor.start_mission(mission.mission_id, owner_id=OWNER)
        tasks = {
            task.task_key: task
            for task in supervisor.tasks.list_tasks(mission.mission_id, owner_id=OWNER)
        }
        supervisor.tasks.update_status(tasks["first"].task_id, "failed", owner_id=OWNER)
        blocked = supervisor.on_task_failed(
            mission.mission_id, tasks["first"].task_id, owner_id=OWNER
        )
        assert blocked == 1
        second = supervisor.tasks.get(tasks["second"].task_id, owner_id=OWNER)
        assert second is not None and second.status == "blocked"

    def test_parallel_dispatch_of_ready_tasks(self, env) -> None:
        supervisor, orchestrator, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        supervisor.start_mission(mission_id, owner_id=OWNER)
        dispatched = supervisor.dispatch_ready(mission_id, owner_id=OWNER)
        assert len(dispatched) == 2, "independent ready tasks dispatch in one pass"
        verify = [
            task
            for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
            if task.task_key == "verify"
        ][0]
        assert verify.status == "pending", "dependent task is not dispatched early"

    def test_add_dependency_validates_mission_membership(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Membership", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[{"task_key": "solo", "objective": "solo"}],
        )
        task = supervisor.tasks.list_tasks(mission.mission_id, owner_id=OWNER)[0]
        other = supervisor.create_mission("Other", owner_id=OWNER)
        other_tasks = TaskGraphStore(supervisor.database)
        foreign = TaskSpec(
            task_id="foreign-task",
            mission_id=other.mission_id,
            task_key="foreign",
            objective="foreign",
        )
        other_tasks.add_task(foreign, owner_id=OWNER)
        with pytest.raises(TaskGraphError):
            supervisor.tasks.add_dependency(
                mission.mission_id, task.task_id, "foreign-task", owner_id=OWNER
            )


class TestArtifacts:
    def _store(self, tmp_path: Path) -> tuple[ArtifactStore, Database]:
        database = Database(tmp_path / "artifacts.sqlite3")
        return ArtifactStore(database, tmp_path / "artifacts"), database

    def test_publish_and_read_roundtrip(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        record = store.publish(
            "the patch content",
            owner_id=OWNER,
            mission_id="m1",
            task_id="t1",
            assignment_id="asg1",
            artifact_type="patch",
        )
        assert record.version == 1
        data = (
            store.read("x-nope", owner_id=OWNER)
            if False
            else store.read(record.artifact_id, owner_id=OWNER, assignment_id="asg1")
        )
        assert data.decode() == "the patch content"

    def test_owner_isolation(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        record = store.publish("secret", owner_id=OWNER, mission_id="m1", artifact_type="report")
        with pytest.raises(KeyError):
            store.read(record.artifact_id, owner_id="owner-b")
        with pytest.raises(KeyError):
            store.get_metadata(record.artifact_id, owner_id="owner-b")

    def test_version_increments_per_scope_and_type(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        first = store.publish(
            "v1", owner_id=OWNER, mission_id="m1", task_id="t1", artifact_type="patch"
        )
        second = store.publish(
            "v2", owner_id=OWNER, mission_id="m1", task_id="t1", artifact_type="patch"
        )
        other_type = store.publish(
            "v1b", owner_id=OWNER, mission_id="m1", task_id="t1", artifact_type="report"
        )
        assert (first.version, second.version, other_type.version) == (1, 2, 1)

    def test_sharing_scope_enforced(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        record = store.publish(
            "shared doc",
            owner_id=OWNER,
            mission_id="m1",
            task_id="t1",
            assignment_id="producer",
            artifact_type="report",
        )
        with pytest.raises(ArtifactPermissionError):
            store.read(record.artifact_id, owner_id=OWNER, assignment_id="consumer")
        store.grant_access(record.artifact_id, "consumer", owner_id=OWNER)
        shared = store.read(record.artifact_id, owner_id=OWNER, assignment_id="consumer")
        assert shared == b"shared doc"

    def test_integrity_verification_detects_tampering(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        record = store.publish("payload", owner_id=OWNER, mission_id="m1", artifact_type="data")
        assert store.verify_integrity(record.artifact_id, owner_id=OWNER) is True
        path = store.storage_root / record.storage_key
        path.write_bytes(b"tampered")
        assert store.verify_integrity(record.artifact_id, owner_id=OWNER) is False
        with pytest.raises(ArtifactError, match="integrity"):
            store.read(record.artifact_id, owner_id=OWNER)

    def test_size_limit_enforced(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        store.max_bytes = 10
        with pytest.raises(ArtifactError, match="byte limit"):
            store.publish("x" * 11, owner_id=OWNER, mission_id="m1", artifact_type="data")

    def test_empty_content_rejected(self, tmp_path: Path) -> None:
        store, _ = self._store(tmp_path)
        with pytest.raises(ArtifactError):
            store.publish("", owner_id=OWNER, mission_id="m1", artifact_type="data")


class TestReviewAndAcceptance:
    def _mission_with_succeeded_task(self, env) -> tuple[MainSupervisor, str, str]:
        supervisor, *_ = env
        mission = supervisor.create_mission("Reviewed work", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[{"task_key": "impl", "objective": "implement it", "preferred_role": "backend"}],
        )
        supervisor.start_mission(mission.mission_id, owner_id=OWNER)
        task = supervisor.tasks.list_tasks(mission.mission_id, owner_id=OWNER)[0]
        supervisor.tasks.update_status(task.task_id, "succeeded", owner_id=OWNER)
        return supervisor, mission.mission_id, task.task_id

    def test_review_without_evidence_is_unverified(self, env) -> None:
        supervisor, mission_id, _ = self._mission_with_succeeded_task(env)
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "completed_unverified"
        assert "no verification evidence" in decision.reasons[0]
        mission = supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "completed_unverified"

    def test_verification_evidence_yields_verified_completion(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id,
            task_id,
            owner_id=OWNER,
            check_name="pytest targeted",
            status="passed",
            command_argv=["python", "-m", "pytest", "-q"],
            exit_code=0,
            duration_ms=1200,
        )
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "completed"
        mission = supervisor.missions.get(mission_id, owner_id=OWNER)
        assert mission is not None and mission.status == "completed"

    def test_failing_check_blocks_acceptance(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id,
            task_id,
            owner_id=OWNER,
            check_name="pytest targeted",
            status="failed",
            exit_code=1,
        )
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "blocked"
        assert "pytest targeted" in decision.failed_check_names

    def test_required_check_never_ran_is_unverified(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id,
            task_id,
            owner_id=OWNER,
            check_name="lint",
            status="passed",
        )
        decision = supervisor.finalize_mission(
            mission_id, owner_id=OWNER, require_checks=["pytest targeted"]
        )
        assert decision.decision == "completed_unverified"
        assert "pytest targeted" in decision.unavailable_check_names

    def test_not_available_check_is_never_success(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id,
            task_id,
            owner_id=OWNER,
            check_name="build",
            status="not_available",
        )
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "completed_unverified"

    def test_blocking_review_finding_creates_bounded_revision(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        loop = ReviewLoop(supervisor.database, max_cycles=2)
        finding = ReviewFinding(
            finding_id="f1",
            severity="critical",
            category="correctness",
            location="app.py:42",
            evidence="off-by-one in retry loop",
        )
        outcome = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id="rev-1",
            status="changes_requested",
            findings=[finding],
        )
        assert outcome.revision_task_id is not None
        revision = supervisor.tasks.get(outcome.revision_task_id, owner_id=OWNER)
        assert revision is not None
        assert revision.task_key == "impl-revision-2"
        original = supervisor.tasks.get(task_id, owner_id=OWNER)
        assert original is not None and original.status == "revision_required"

        second = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=outcome.revision_task_id,
            reviewer_assignment_id="rev-2",
            status="changes_requested",
            findings=[
                ReviewFinding(
                    finding_id="f2",
                    severity="major",
                    category="maintainability",
                    evidence="still wrong",
                )
            ],
        )
        assert second.escalated is True, "max_cycles=2 exhausted after revision 2"
        assert second.blockers == ("f2",)
        assert second.revision_task_id is None

    def test_approved_review_with_no_findings_succeeds(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        loop = ReviewLoop(supervisor.database, max_cycles=3)
        outcome = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id="rev-1",
            status="approved",
            findings=[],
        )
        assert outcome.approved is True
        task = supervisor.tasks.get(task_id, owner_id=OWNER)
        assert task is not None and task.status == "succeeded"

    def test_approved_revision_closes_parent_and_resolves_findings(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id, task_id, owner_id=OWNER, check_name="pytest", status="passed", exit_code=0
        )
        loop = ReviewLoop(supervisor.database, max_cycles=3)
        outcome = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id="rev-1",
            status="changes_requested",
            findings=[
                ReviewFinding(
                    finding_id="f1",
                    severity="major",
                    category="maintainability",
                    evidence="needs cleanup",
                )
            ],
        )
        assert outcome.revision_task_id is not None
        revision_done = loop.submit_review(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=outcome.revision_task_id,
            reviewer_assignment_id="rev-2",
            status="approved",
            findings=[],
        )
        assert revision_done.approved is True
        parent = supervisor.tasks.get(task_id, owner_id=OWNER)
        assert parent is not None and parent.status == "succeeded"
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "completed"
        assert decision.blocking_finding_ids == ()

    def test_acceptance_ignores_reviewer_approval_over_failing_tests(self, env) -> None:
        supervisor, mission_id, task_id = self._mission_with_succeeded_task(env)
        supervisor.record_verification(
            mission_id, task_id, owner_id=OWNER, check_name="pytest", status="failed", exit_code=1
        )
        supervisor.acceptance.reviews.submit(
            owner_id=OWNER,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id="rev",
            status="approved",
            findings=[],
        )
        decision = supervisor.finalize_mission(mission_id, owner_id=OWNER)
        assert decision.decision == "blocked"

    def test_incomplete_tasks_block_finalization(self, env) -> None:
        supervisor, *_ = env
        mission = supervisor.create_mission("Not done yet", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[{"task_key": "impl", "objective": "work"}],
        )
        decision = supervisor.finalize_mission(mission.mission_id, owner_id=OWNER)
        assert decision.decision == "blocked"
        assert any("not successfully terminal" in reason for reason in decision.reasons)


class TestResume:
    def test_resume_requeues_stale_in_flight_tasks(self, env) -> None:
        supervisor, orchestrator, database, *_ = env
        mission_id = _plan_two_task_dag(supervisor)
        supervisor.start_mission(mission_id, owner_id=OWNER)
        supervisor.dispatch_ready(mission_id, owner_id=OWNER)
        for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER):
            orchestrator.store.update(task.assignment_id, status="cancelled", owner_id=OWNER)
        resumed = supervisor.resume_mission(mission_id, owner_id=OWNER)
        assert resumed.status == "running"
        states = {
            task.task_key: task.status
            for task in supervisor.tasks.list_tasks(mission_id, owner_id=OWNER)
        }
        assert states == {"ui": "ready", "api": "ready", "verify": "pending"}


def test_verification_evidence_contract_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        VerificationEvidence(check_name="x", status="agent_says_so")
    with pytest.raises(ValueError):
        VerificationEvidence(check_name="", status="passed")


def test_task_spec_rejects_self_dependency() -> None:
    with pytest.raises(ValueError, match="cannot depend on itself"):
        TaskSpec(task_id="t", mission_id="m", task_key="k", objective="o", depends_on=("t",))
