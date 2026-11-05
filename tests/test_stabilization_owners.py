from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.supervision import RuntimeSupervisor
from eurus.events.bus import EventBus
from eurus.goals.engine import GoalEngine
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.jobs import Job
from eurus.workflows.engine import WorkflowEngine
from eurus.workflows.states import NodeStatus, WorkflowStatus


class ExecutorStub(JobExecutor):
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        return f"done {job.objective}"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database(tmp_path / "owner.sqlite3")
        self.executor = ExecutorStub()
        self.schedule_store = ScheduleStore(tmp_path / "owner.sqlite3")
        self.engine_a = AutomationEngine(
            self.database,
            self.executor,
            owner_id="alice",
            poll_seconds=0.05,
            scheduler=self.schedule_store,
            schedule_runs=self.schedule_store,
            occurrence_finalizer=self.schedule_store.finalize_occurrence,
        )
        self.engine_b = AutomationEngine(
            self.database,
            self.executor,
            owner_id="bob",
            poll_seconds=0.05,
            scheduler=self.schedule_store,
            schedule_runs=self.schedule_store,
            occurrence_finalizer=self.schedule_store.finalize_occurrence,
        )
        self.admin_engine = AutomationEngine(
            self.database, self.executor, owner_id=None, poll_seconds=0.05
        )

    def close(self) -> None:
        self.database.close()


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_owner_a_cannot_claim_owner_b_jobs(world: World) -> None:
    bob_job = world.engine_b.submit("bob", "bob objective")
    claimed = world.engine_a.governor.claim("alice-worker", lease_seconds=60)
    assert claimed is None
    claimed_by_bob = world.engine_b.governor.claim("bob-worker", lease_seconds=60)
    assert claimed_by_bob is not None
    assert claimed_by_bob.job_id == bob_job.job_id
    world.close()


def test_owner_scoped_recovery_never_touches_other_owners(world: World) -> None:
    alice_job = world.engine_a.submit("alice", "alice objective")
    world.engine_a.jobs.claim("alice-worker", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=120)
    recovered = world.engine_b.jobs.recover_expired(now=past, owner_id="bob")
    assert recovered == {"requeued": 0, "unknown_outcome": 0, "failed": 0}
    stored = world.engine_a.jobs.get(alice_job.job_id)
    assert stored is not None
    assert stored.status == "running"
    recovered_admin = world.admin_engine.jobs.recover_expired(now=past)
    assert recovered_admin["requeued"] == 1
    world.close()


def test_unscoped_admin_recovers_everything(world: World) -> None:
    for engine, owner in ((world.engine_a, "alice"), (world.engine_b, "bob")):
        engine.submit(owner, f"{owner} objective")
        engine.jobs.claim("shared-worker", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=120)
    recovered = world.admin_engine.jobs.recover_expired(now=past)
    assert recovered["requeued"] == 2
    world.close()


def test_supervisor_forwards_owner_to_goal_and_workflow_ticks(tmp_path: Path) -> None:
    database = Database(tmp_path / "sup.sqlite3")
    executor = ExecutorStub()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    goals = GoalEngine(database, engine)
    workflows = WorkflowEngine(database, engine)
    events = EventBus(database, automation=engine)
    supervisor = RuntimeSupervisor(
        engine,
        goals=goals,
        workflows=workflows,
        events=events,
        owner_id="alice",
    )
    alice_goal = goals.create_goal("alice", "alice objective")
    goals.capture_plan(
        alice_goal.goal_id,
        {"tasks": [{"title": "alice task", "success_criteria": "done"}]},
    )
    bob_goal = goals.create_goal("bob", "bob objective")
    goals.capture_plan(
        bob_goal.goal_id,
        {"tasks": [{"title": "bob task", "success_criteria": "done"}]},
    )
    report = supervisor.supervise()
    alice_jobs = engine.jobs.list_jobs(owner_id="alice")
    assert alice_jobs, "alice goal plan jobs must be submitted"
    assert engine.jobs.list_jobs(owner_id="bob") == []
    for job in alice_jobs:
        assert job.owner_id == "alice"
        assert job.payload.get("goal_id") in (alice_goal.goal_id,)
    _ = report
    database.close()


def test_cross_owner_goal_cancellation_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "goal.sqlite3")
    executor = ExecutorStub()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    goals = GoalEngine(database, engine)
    bob_goal = goals.create_goal("bob", "bob objective")
    result = goals.cancel(bob_goal.goal_id, "alice")
    assert result is None
    stored = goals.get(bob_goal.goal_id)
    assert stored is not None
    assert stored.status.value != "cancelled"
    database.close()


def test_cross_owner_assignment_cancel_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "assign.sqlite3")
    executor = ExecutorStub()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    registry = orchestrator.registry
    registry.register("researcher", description="r", max_concurrent=2)
    assignment = orchestrator.dispatch("study the topic", role="researcher", owner_id="bob")
    cancelled = orchestrator.cancel(assignment.assignment_id, owner_id="alice")
    assert cancelled is None
    stored = orchestrator.store.get(assignment.assignment_id)
    assert stored is not None
    assert stored.status != "cancelled"
    cancelled_by_owner = orchestrator.cancel(assignment.assignment_id, owner_id="bob")
    assert cancelled_by_owner is not None
    assert cancelled_by_owner.status == "cancelled"
    database.close()


def test_assignment_listing_and_capacity_are_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "cap.sqlite3")
    executor = ExecutorStub()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register("writer", description="w", max_concurrent=1)
    orchestrator.dispatch("write a", role="writer", owner_id="alice")
    bob_view = orchestrator.store.list_assignments(owner_id="bob")
    assert bob_view == []
    with pytest.raises(RuntimeError, match="no free capacity"):
        orchestrator.dispatch("write b", role="writer", owner_id="alice")
    bob_dispatch = orchestrator.dispatch("write c", role="writer", owner_id="bob")
    assert bob_dispatch.owner_id == "bob"
    database.close()


def test_cross_owner_workflow_recovery_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "wf.sqlite3")
    executor = ExecutorStub()
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    workflows = WorkflowEngine(database, engine)
    workflows.register_definition(
        "wf-iso",
        "Isolation",
        [
            {
                "node_id": "only",
                "node_type": "agent_task",
                "config": {"objective": "do the thing"},
            }
        ],
        owner_id="bob",
    )
    instance = workflows.start("wf-iso", "bob")
    workflows.advance_instance(instance.instance_id, owner_id="bob")
    workflow_job = workflows.automation.jobs.list_jobs(owner_id="bob")[0]
    with database.connection() as connection:
        connection.execute(
            "UPDATE eurus_jobs SET status='running', worker_id=?, lease_expires_at=?, "
            "lease_version=lease_version+1, attempts=attempts+1 WHERE job_id=?",
            (
                "bob-worker",
                (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                workflow_job.job_id,
            ),
        )
    node = workflows.store.get_node(instance.instance_id, "only")
    assert node is not None and node.status == NodeStatus.RUNNING
    with database.connection() as connection:
        connection.execute(
            "UPDATE eurus_jobs SET status='completed', result='workflow output' WHERE job_id=?",
            (workflow_job.job_id,),
        )
    alice_engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    recovered = workflows.recover(owner_id="alice")
    assert recovered == 0
    stored_node = workflows.store.get_node(instance.instance_id, "only")
    assert stored_node is not None and stored_node.status == NodeStatus.RUNNING
    bob_supervisor = RuntimeSupervisor(alice_engine, workflows=workflows, owner_id="bob")
    bob_supervisor.supervise()
    stored_node = workflows.store.get_node(instance.instance_id, "only")
    assert stored_node is not None and stored_node.status == NodeStatus.SUCCEEDED
    _ = WorkflowStatus
    database.close()


def test_owner_scope_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    executor = ExecutorStub()
    database = Database(path)
    engine = AutomationEngine(database, executor, owner_id="alice", poll_seconds=0.05)
    engine.submit("bob", "bob objective")
    engine.submit("alice", "alice objective")
    database.close()

    reopened = Database(path)
    restarted = AutomationEngine(reopened, executor, owner_id="alice", poll_seconds=0.05)
    claimed = restarted.governor.claim("alice-worker", lease_seconds=60)
    assert claimed is not None
    assert claimed.owner_id == "alice"
    assert restarted.jobs.list_jobs(owner_id="bob")[0].status == "queued"
    reopened.close()


def test_cross_owner_lease_renewal_is_refused(world: World) -> None:
    job = world.engine_a.submit("alice", "alice objective")
    claim = world.engine_a.jobs.claim("alice-worker", lease_seconds=60)
    assert claim is not None
    renewed = world.engine_b.jobs.heartbeat(
        job.job_id, "bob-worker", claim.lease_version, lease_seconds=60
    )
    assert renewed is False
    renewed_wrong_version = world.engine_a.jobs.heartbeat(
        job.job_id, "alice-worker", claim.lease_version + 5, lease_seconds=60
    )
    assert renewed_wrong_version is False
    renewed_ok = world.engine_a.jobs.heartbeat(
        job.job_id, "alice-worker", claim.lease_version, lease_seconds=60
    )
    assert renewed_ok is True
    world.close()


def test_event_subscriptions_do_not_leak_jobs_across_owners(world: World) -> None:
    bus = EventBus(world.database, automation=world.engine_b)
    bus.subscribe(
        "bob-only",
        event_types=("PING",),
        handler_kind="submit_job",
        handler_config={"owner_id": "bob"},
    )
    bus.publish("PING", subject_type="test", subject_id="1")
    bus.dispatch_tick()
    jobs = world.engine_b.jobs.list_jobs(owner_id="bob")
    assert len(jobs) == 1
    assert world.engine_a.jobs.list_jobs(owner_id="alice") == []
    world.close()
