from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.agent.collaboration import CollaborationError, CollaborationHub
from eurus.agent.engineering_manager import EngineeringManager
from eurus.agent.task_decomposer import TaskDecomposer
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.coding.project_understanding import understand_project
from eurus.coding.security_review import review_diff, review_text, review_workspace
from eurus.coding.test_strategy import build_test_strategy, is_test_path
from eurus.multiagent.contracts import TaskSpec
from eurus.multiagent.orchestrator import AgentOrchestrator, Assignment
from eurus.multiagent.supervision import MissionSupervision
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database

OWNER = "owner-a"


class RecordingExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job, context) -> str:
        payload = job.payload or {}
        self.executed.append(str(payload.get("role", "unknown")))
        return f"done by {payload.get('role', 'unknown')}"


@pytest.fixture()
def stack(tmp_path: Path):
    database = Database(tmp_path / "agents.sqlite3")
    executor = RecordingExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, automation)
    return database, orchestrator, tmp_path


@pytest.fixture()
def hub(tmp_path: Path) -> CollaborationHub:
    database = Database(tmp_path / "collab.sqlite3")
    return CollaborationHub(database, tmp_path / "artifacts")


class TestProjectUnderstanding:
    def _repo(self, tmp_path: Path) -> Path:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# demo", encoding="utf-8")
        (tmp_path / "src" / "demo").mkdir(parents=True)
        (tmp_path / "src" / "demo" / "core.py").write_text(
            "def run():\n    return 1\n", encoding="utf-8"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_core.py").write_text(
            "def test_run():\n    assert run()\n", encoding="utf-8"
        )
        return tmp_path

    def test_detects_manifests_tests_and_docs(self, tmp_path: Path) -> None:
        understanding = understand_project(self._repo(tmp_path))
        assert "pyproject.toml" in understanding.manifests
        assert "tests" in understanding.test_dirs
        assert "README.md" in understanding.docs
        assert "py" in understanding.languages
        assert understanding.module_count == 2
        assert "core.py" in understanding.summary

    def test_missing_workspace_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="existing directory"):
            understand_project(tmp_path / "nope")

    def test_notes_flag_missing_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        understanding = understand_project(tmp_path)
        assert understanding.manifests == ()
        assert any("manifest" in note for note in understanding.notes)

    def test_summary_is_bounded(self, tmp_path: Path) -> None:
        understanding = understand_project(self._repo(tmp_path), max_chars=400)
        assert len(understanding.summary) <= 400


class TestTestStrategy:
    def _repo(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / "test_math.py").write_text(
            "def test_one():\n    assert 1\n", encoding="utf-8"
        )
        return tmp_path

    def test_mirror_mapping_to_existing_test(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        plan = build_test_strategy(base, ["src/math.py"])
        targeted = plan.targeted
        assert len(targeted) == 1
        assert "tests/test_math.py" in targeted[0].argv

    def test_test_paths_are_targeted_directly(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        plan = build_test_strategy(base, ["tests/test_math.py"])
        assert "tests/test_math.py" in plan.targeted[0].argv

    def test_unmapped_paths_fall_back_to_regression(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        plan = build_test_strategy(base, ["docs/readme.md"])
        assert plan.targeted == ()
        assert any("mirror" in note for note in plan.notes)
        assert plan.regression[0].argv[:3] == ("python", "-m", "pytest")

    def test_static_lint_covers_changed_python(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        plan = build_test_strategy(base, ["src/math.py"])
        static = [command.argv for command in plan.static]
        assert any(argv[:2] == ("ruff", "check") and "src/math.py" in argv for argv in static)

    def test_all_commands_are_allowlisted(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        plan = build_test_strategy(base, ["src/math.py", "tests/test_math.py"])
        assert plan.commands, "a valid plan emits commands"
        for command in plan.commands:
            assert command.argv[0] in {"python", "ruff"}

    def test_is_test_path(self) -> None:
        assert is_test_path("tests/test_x.py")
        assert is_test_path("pkg/thing_test.py")
        assert is_test_path("src/test_other.py")
        assert not is_test_path("src/other.py")

    def test_max_targeted_cap(self, tmp_path: Path) -> None:
        base = self._repo(tmp_path)
        for index in range(5):
            (base / "tests" / f"test_{index}.py").write_text(
                "def test_x():\n    pass\n", encoding="utf-8"
            )
        plan = build_test_strategy(
            base,
            [f"tests/test_{index}.py" for index in range(5)],
            max_targeted=2,
        )
        assert len(plan.targeted) == 1
        assert any("capped" in note for note in plan.notes)


class TestSecurityReview:
    def test_hardcoded_secret_is_blocking(self) -> None:
        report = review_text('api_key = "sk-live-1234567890abcdef"', location="cfg.py")
        assert report.has_blocking
        assert report.findings[0].severity == "blocker"
        finding = report.findings[0].to_review_finding()
        assert finding.category == "security"
        assert finding.location.startswith("cfg.py:")

    def test_example_placeholders_are_not_flagged(self) -> None:
        report = review_text('api_key = "YOUR_EXAMPLE_KEY_HERE"', location="cfg.py")
        assert report.findings == ()

    def test_tls_and_shell_rules(self) -> None:
        report = review_text(
            "requests.get(url, verify=False)\nsubprocess.run(cmd, shell=True)\n",
            location="net.py",
        )
        severities = {finding.title: finding.severity for finding in report.findings}
        assert severities["TLS certificate verification disabled"] == "critical"
        assert severities["subprocess with shell=True"] == "critical"

    def test_yaml_and_pickle_rules(self) -> None:
        report = review_text(
            "data = yaml.load(blob)\nobj = pickle.loads(raw)\n", location="load.py"
        )
        titles = {finding.title for finding in report.findings}
        assert "yaml.load without an explicit safe Loader" in titles
        assert "pickle deserialization" in titles

    def test_diff_scan_only_added_lines(self) -> None:
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+token = 'supersecretvalue123'\n"
            " def main():\n"
            "-    pass\n"
            "+    return os.environ.get('TOKEN')\n"
        )
        report = review_diff(diff)
        assert report.files_scanned == 0
        assert any(
            finding.severity == "blocker" and finding.location.startswith("app.py:")
            for finding in report.findings
        ), "the added secret line is flagged with its diff line number"

    def test_workspace_scan_is_defensive_and_bounded(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("password = 'hunter2hunter2'\n", encoding="utf-8")
        (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")
        report = review_workspace(tmp_path)
        assert report.files_scanned == 2
        assert report.has_blocking
        assert "scanned 2 file(s)" in report.summary

    def test_workspace_scan_requires_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="existing directory"):
            review_workspace(tmp_path / "missing")


class TestCollaborationHub:
    def test_publish_and_scoped_read(self, hub: CollaborationHub) -> None:
        record = hub.publish_artifact(
            "patch text",
            owner_id=OWNER,
            mission_id="m1",
            task_id="t1",
            assignment_id="producer",
            artifact_type="patch",
        )
        assert record.version == 1
        assert hub.read_artifact(record.artifact_id, owner_id=OWNER, assignment_id="producer")
        with pytest.raises(PermissionError):
            hub.read_artifact(record.artifact_id, owner_id=OWNER, assignment_id="stranger")

    def test_share_grants_recipient_access(self, hub: CollaborationHub) -> None:
        record = hub.publish_artifact(
            "report", owner_id=OWNER, mission_id="m1", assignment_id="a1", artifact_type="report"
        )
        assert hub.share_artifact(record.artifact_id, "a2", owner_id=OWNER)
        assert hub.read_artifact(record.artifact_id, owner_id=OWNER, assignment_id="a2")

    def test_unknown_message_type_rejected(self, hub: CollaborationHub) -> None:
        with pytest.raises(CollaborationError, match="unknown collaboration message type"):
            hub.send(
                assignment_id="a1",
                sender="lead",
                message_type="gossip",
                body={"x": 1},
                owner_id=OWNER,
                mission_id="m1",
            )

    def test_artifact_refs_must_belong_to_mission(self, hub: CollaborationHub) -> None:
        record = hub.publish_artifact("data", owner_id=OWNER, mission_id="m1", artifact_type="data")
        with pytest.raises(CollaborationError, match="belongs to mission"):
            hub.send(
                assignment_id="a1",
                sender="lead",
                message_type="result_delivery",
                body={"summary": "done"},
                owner_id=OWNER,
                mission_id="other-mission",
                artifact_refs=(record.to_ref(),),
            )

    def test_request_review_carries_artifact_ref(self, hub: CollaborationHub) -> None:
        record = hub.publish_artifact(
            "patch v1",
            owner_id=OWNER,
            mission_id="m1",
            assignment_id="coder",
            artifact_type="patch",
        )
        message = hub.request_review(
            assignment_id="coder",
            sender="team-lead",
            owner_id=OWNER,
            mission_id="m1",
            artifact_id=record.artifact_id,
            scope="correctness",
        )
        assert message.message_type == "review_request"
        assert message.artifact_refs[0].artifact_id == record.artifact_id
        conversation = hub.conversation("coder")
        assert conversation[0].message_type == "review_request"

    def test_decision_record_links_source_artifacts(self, hub: CollaborationHub) -> None:
        record = hub.publish_artifact(
            "design", owner_id=OWNER, mission_id="m1", artifact_type="design"
        )
        message = hub.record_decision(
            assignment_id="lead",
            sender="eng-manager",
            owner_id=OWNER,
            mission_id="m1",
            decision="use sqlite",
            rationale="durable and local",
            source_artifact_ids=[record.artifact_id],
        )
        assert message.artifact_refs[0].artifact_id == record.artifact_id
        assert message.body["decision"] == "use sqlite"

    def test_publish_announces_to_recipients(self, hub: CollaborationHub) -> None:
        hub.publish_artifact(
            "shared design",
            owner_id=OWNER,
            mission_id="m1",
            assignment_id="architect",
            artifact_type="design",
            announce_to=("frontend-dev",),
        )
        assert hub.read_artifact(
            hub.artifacts.list_for_mission("m1", owner_id=OWNER)[0].artifact_id,
            owner_id=OWNER,
            assignment_id="frontend-dev",
        )
        messages = hub.conversation("frontend-dev")
        assert messages[0].message_type == "artifact_published"


def _plan(tasks: list[dict]) -> object:
    return TaskDecomposer().decompose("build the feature", mission_id="m1", tasks=tasks)


class TestEngineeringManager:
    def test_valid_plan_is_approved(self) -> None:
        plan = _plan(
            [
                {"task_key": "api", "objective": "api", "preferred_role": "backend"},
                {
                    "task_key": "verify",
                    "objective": "verify",
                    "depends_on": ["api"],
                    "preferred_role": "qa",
                },
            ]
        )
        decision = EngineeringManager(enabled_roles=("backend", "qa")).review_plan(plan)
        assert decision.approved
        assert decision.issues == ()

    def test_unknown_role_rejected(self) -> None:
        plan = _plan([{"task_key": "api", "objective": "api", "preferred_role": "wizard"}])
        decision = EngineeringManager(enabled_roles=("backend",)).review_plan(plan)
        assert not decision.approved
        assert any("wizard" in issue for issue in decision.issues)

    def test_missing_verification_warns(self) -> None:
        plan = _plan([{"task_key": "api", "objective": "api"}])
        decision = EngineeringManager().review_plan(plan)
        assert decision.approved
        assert any("QA/verification" in warning for warning in decision.warnings)

    def test_deep_fan_in_warns(self) -> None:
        plan = _plan(
            [{"task_key": f"leaf-{index}", "objective": "leaf"} for index in range(5)]
            + [
                {
                    "task_key": "join",
                    "objective": "join",
                    "depends_on": [f"leaf-{index}" for index in range(5)],
                }
            ]
        )
        decision = EngineeringManager().review_plan(plan)
        assert any("waits on 5 dependencies" in warning for warning in decision.warnings)

    def test_empty_plan_rejected(self) -> None:
        from eurus.agent.task_decomposer import TaskPlan

        plan = TaskPlan(objective="empty", route="hierarchical", tasks=())
        decision = EngineeringManager().review_plan(plan)
        assert not decision.approved

    def test_overlapping_paths_are_grouped(self) -> None:
        manager = EngineeringManager()
        conflict = manager.route_conflicts(
            {
                "asg-1": ("src/app.py", "src/util.py"),
                "asg-2": ("src/app.py",),
                "asg-3": ("src/other.py",),
            }
        )
        assert conflict.has_conflicts
        assert len(conflict.groups) == 1
        assert conflict.groups[0].assignments == ("asg-1", "asg-2")
        assert conflict.groups[0].paths == ("src/app.py",)
        assert conflict.independent == ("asg-3",)

    def test_disjoint_paths_have_no_conflict(self) -> None:
        manager = EngineeringManager()
        conflict = manager.route_conflicts({"asg-1": ("src/a.py",), "asg-2": ("src/b.py",)})
        assert not conflict.has_conflicts
        assert conflict.independent == ("asg-1", "asg-2")


def _make_assignment(
    assignment_id: str,
    *,
    mission_id: str,
    task_id: str,
    status: str = "running",
    age_seconds: int = 0,
) -> Assignment:
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return Assignment(
        assignment_id=assignment_id,
        specialist_id="sp-1",
        role="coder",
        objective="do work",
        status=status,
        created_at=moment,
        updated_at=moment,
        owner_id=OWNER,
        mission_id=mission_id,
        task_id=task_id,
    )


class TestMissionSupervision:
    def _mission(self, database: Database, tasks: TaskGraphStore) -> tuple[str, dict[str, str]]:
        from eurus.agent.supervisor import MissionStore

        missions = MissionStore(database)
        mission = missions.create("supervised work", owner_id=OWNER)
        spec = TaskSpec(
            task_id="task-1",
            mission_id=mission.mission_id,
            task_key="work",
            objective="work",
        )
        tasks.add_task(spec, owner_id=OWNER)
        tasks.update_status("task-1", "running", owner_id=OWNER, assignment_id="asg-1")
        return mission.mission_id, {"work": "task-1"}

    def test_vanished_assignment_is_resynced(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        supervision = MissionSupervision(database, orchestrator)
        report = supervision.tick(mission_id, owner_id=OWNER)
        assert "task-1" in report.resynced_tasks
        assert any("vanished" in note for note in report.notes)
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "ready"
        assert task.assignment_id is None

    def test_finished_assignment_moves_to_review(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        orchestrator.store.create(
            _make_assignment("asg-1", mission_id=mission_id, task_id="task-1", status="succeeded")
        )
        report = MissionSupervision(database, orchestrator).tick(mission_id, owner_id=OWNER)
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "succeeded"
        assert "task-1" in report.resynced_tasks
        assert any("review approved" in note for note in report.notes)

    def test_review_pending_with_failed_evidence_requests_revision(self, stack) -> None:
        from eurus.agent.acceptance import AcceptanceService

        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        tasks.update_status("task-1", "review_pending", owner_id=OWNER, clear_assignment=True)
        acceptance = AcceptanceService(database)
        acceptance.record_verification(
            check_name="pytest",
            status="failed",
            category="test",
            exit_code=1,
            mission_id=mission_id,
            task_id="task-1",
            owner_id=OWNER,
        )
        MissionSupervision(database, orchestrator).tick(mission_id, owner_id=OWNER)
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "revision_required"
        revisions = [
            item
            for item in tasks.list_tasks(mission_id, owner_id=OWNER)
            if item.parent_task_id == "task-1"
        ]
        assert revisions

    def test_review_pending_without_criteria_evidence_blocks(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        tasks.update_status("task-1", "review_pending", owner_id=OWNER, clear_assignment=True)
        blocked = TaskSpec(
            task_id="task-2",
            mission_id=mission_id,
            task_key="checked-work",
            objective="checked work",
            acceptance_criteria=("tests pass",),
        )
        tasks.add_task(blocked, owner_id=OWNER)
        tasks.update_status("task-2", "review_pending", owner_id=OWNER, clear_assignment=True)
        MissionSupervision(database, orchestrator).tick(mission_id, owner_id=OWNER)
        task = tasks.get("task-2", owner_id=OWNER)
        assert task is not None and task.status == "revision_required"

    def test_stale_heartbeat_is_cancelled_and_requeued(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        orchestrator.store.create(
            _make_assignment(
                "asg-1",
                mission_id=mission_id,
                task_id="task-1",
                status="running",
                age_seconds=3600,
            )
        )
        supervision = MissionSupervision(database, orchestrator, stale_after_seconds=900)
        report = supervision.tick(mission_id, owner_id=OWNER)
        assert report.stale_assignments == ("asg-1",)
        assignment = orchestrator.store.get("asg-1")
        assert assignment is not None and assignment.status == "cancelled"
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "ready"
        assert task.assignment_id is None

    def test_fresh_heartbeat_is_left_alone(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        orchestrator.store.create(
            _make_assignment("asg-1", mission_id=mission_id, task_id="task-1")
        )
        orchestrator.store.heartbeat("asg-1", "working", owner_id=OWNER)
        report = MissionSupervision(database, orchestrator).tick(mission_id, owner_id=OWNER)
        assert report.stale_assignments == ()
        assert report.resynced_tasks == ()
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "running"

    def test_deadline_expiry_cancels_descendants(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        orchestrator.store.create(
            _make_assignment("asg-1", mission_id=mission_id, task_id="task-1")
        )
        report = MissionSupervision(database, orchestrator).tick(
            mission_id, owner_id=OWNER, deadline=datetime.now(UTC) - timedelta(minutes=1)
        )
        assert report.deadline_expired
        assignment = orchestrator.store.get("asg-1")
        assert assignment is not None and assignment.status == "cancelled"
        task = tasks.get("task-1", owner_id=OWNER)
        assert task is not None and task.status == "cancelled"

    def test_owner_scope_enforced(self, stack) -> None:
        database, orchestrator, _ = stack
        tasks = TaskGraphStore(database)
        mission_id, _ = self._mission(database, tasks)
        with pytest.raises(KeyError):
            MissionSupervision(database, orchestrator).tick(mission_id, owner_id="owner-b")

    def test_invalid_stale_window_rejected(self, stack) -> None:
        database, orchestrator, _ = stack
        with pytest.raises(ValueError, match="stale_after_seconds"):
            MissionSupervision(database, orchestrator, stale_after_seconds=1)


_ = json
