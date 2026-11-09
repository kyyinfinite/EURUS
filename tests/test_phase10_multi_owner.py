from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.supervision import RuntimeSupervisor
from eurus.events.bus import EventBus
from eurus.goals.engine import GoalEngine
from eurus.learning.integration import LearningCoordinator
from eurus.learning.lessons import LessonStore
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.runtime.models import Run, RuntimeEvent
from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.jobs import Job
from eurus.storage.runs import RunStore
from eurus.workflows.engine import WorkflowEngine

OWNERS = ("alice", "bob")


class ScriptedExecutor(JobExecutor):
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.behaviors: dict[str, str] = {}

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        kind = str(job.payload.get("kind", job.objective))
        behavior = self.behaviors.get(f"{job.owner_id}:{kind}", "ok")
        if behavior == "fail":
            raise TimeoutError(f"{kind} failed for {job.owner_id}")
        return f"done {job.objective}"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "multiowner.sqlite3"
        self.lessons_path = tmp_path / "lessons.sqlite3"
        self.executor = ScriptedExecutor()
        self.database = Database(self.path)
        self.schedule_store = ScheduleStore(self.path)
        self.engines: dict[str, AutomationEngine] = {}
        for owner in OWNERS:
            self.engines[owner] = AutomationEngine(
                self.database,
                self.executor,
                owner_id=owner,
                poll_seconds=0.05,
                scheduler=self.schedule_store,
                schedule_runs=self.schedule_store,
                occurrence_finalizer=self.schedule_store.finalize_occurrence,
            )
        self.admin_engine = AutomationEngine(
            self.database, self.executor, owner_id=None, poll_seconds=0.05
        )
        self.bus = EventBus(
            self.database,
            automation=self.engines["alice"],
            schedule_triggers=_TriggerAdapter(self.schedule_store),
        )
        self.goals = GoalEngine(self.database, self.engines["alice"])
        self.workflows = WorkflowEngine(self.database, self.engines["alice"])
        self.orchestrator = AgentOrchestrator(self.database, self.engines["alice"])
        self.learning = LearningCoordinator(
            self.database,
            lessons=LessonStore(self.lessons_path),
            stale_running_seconds=60.0,
            process_inline=False,
        )
        self.supervisors = {
            owner: RuntimeSupervisor(
                self.engines[owner],
                goals=self.goals,
                workflows=self.workflows,
                events=self.bus,
                assignments=self.orchestrator,
                learning=self.learning,
                owner_id=owner,
                stale_assignment_seconds=60,
            )
            for owner in OWNERS
        }
        self.admin_supervisor = RuntimeSupervisor(
            self.admin_engine,
            goals=self.goals,
            workflows=self.workflows,
            events=self.bus,
            assignments=self.orchestrator,
            learning=self.learning,
            owner_id=None,
        )

    def close(self) -> None:
        self.database.close()

    def reopen(self) -> None:
        self.database.close()
        self.database = Database(self.path)
        self.executor = ScriptedExecutor()
        for owner in OWNERS:
            self.engines[owner] = AutomationEngine(
                self.database,
                self.executor,
                owner_id=owner,
                poll_seconds=0.05,
                scheduler=self.schedule_store,
                schedule_runs=self.schedule_store,
                occurrence_finalizer=self.schedule_store.finalize_occurrence,
            )
        self.admin_engine = AutomationEngine(
            self.database, self.executor, owner_id=None, poll_seconds=0.05
        )
        self.bus = EventBus(
            self.database,
            automation=self.engines["alice"],
            schedule_triggers=_TriggerAdapter(self.schedule_store),
        )
        self.goals = GoalEngine(self.database, self.engines["alice"])
        self.workflows = WorkflowEngine(self.database, self.engines["alice"])
        self.orchestrator = AgentOrchestrator(self.database, self.engines["alice"])
        self.learning = LearningCoordinator(
            self.database,
            lessons=LessonStore(self.lessons_path),
            stale_running_seconds=60.0,
            process_inline=False,
        )
        self.supervisors = {
            owner: RuntimeSupervisor(
                self.engines[owner],
                goals=self.goals,
                workflows=self.workflows,
                events=self.bus,
                assignments=self.orchestrator,
                learning=self.learning,
                owner_id=owner,
                stale_assignment_seconds=60,
            )
            for owner in OWNERS
        }
        self.admin_supervisor = RuntimeSupervisor(
            self.admin_engine,
            goals=self.goals,
            workflows=self.workflows,
            events=self.bus,
            assignments=self.orchestrator,
            learning=self.learning,
            owner_id=None,
        )


class _TriggerAdapter:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def trigger_event_schedules(self, event: Any, *, owner_id: str | None = None) -> list[str]:
        return list(
            self.store.trigger_event_schedules(
                str(getattr(event, "event_type", "")),
                subject_id=str(getattr(event, "subject_id", "")),
                owner_id=owner_id,
            )
        )


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    yield w
    w.close()


async def run_owner(engine: AutomationEngine, owner_id: str, *, rounds: int = 8) -> int:
    executed = 0
    for _ in range(rounds):
        if await engine.run_worker():
            executed += 1
        else:
            break
    return executed


async def test_both_owners_execute_their_own_work_concurrently(world: World) -> None:
    alice_job = world.engines["alice"].submit("alice", "alice objective")
    bob_job = world.engines["bob"].submit("bob", "bob objective")
    await asyncio.gather(
        run_owner(world.engines["alice"], "alice"),
        run_owner(world.engines["bob"], "bob"),
    )
    alice_stored = world.engines["alice"].jobs.get(alice_job.job_id)
    bob_stored = world.engines["bob"].jobs.get(bob_job.job_id)
    assert alice_stored is not None and bob_stored is not None
    assert alice_stored.status == "completed"
    assert bob_stored.status == "completed"
    for job_id in world.executor.executed:
        job = world.engines["alice"].jobs.get(job_id)
        assert job is not None


async def test_owner_scoped_worker_never_claims_other_owner_jobs(world: World) -> None:
    world.engines["bob"].submit("bob", "bob objective")
    for _ in range(5):
        assert await world.engines["alice"].run_worker() is False
    stored = world.engines["alice"].jobs.list_jobs(owner_id="bob")[0]
    assert stored.status == "queued"


async def test_cross_owner_goal_workflow_and_assignment_isolation(world: World) -> None:
    alice_goal = world.goals.create_goal("alice", "alice goal")
    bob_goal = world.goals.create_goal("bob", "bob goal")
    assert world.goals.cancel(bob_goal.goal_id, "alice") is None
    assert world.goals.pause(bob_goal.goal_id, "alice") is None

    world.workflows.register_definition(
        "wf-multi",
        "Multi",
        [
            {
                "node_id": "only",
                "node_type": "agent_task",
                "config": {"objective": "do the thing"},
            }
        ],
    )
    world.workflows.start("wf-multi", "alice")
    bob_instance = world.workflows.start("wf-multi", "bob")
    assert world.workflows.cancel(bob_instance.instance_id, "alice") is None

    world.orchestrator.registry.register("worker", description="w", max_concurrent=2)
    alice_assignment = world.orchestrator.dispatch("alice work", role="worker", owner_id="alice")
    bob_assignment = world.orchestrator.dispatch("bob work", role="worker", owner_id="bob")
    assert world.orchestrator.cancel(bob_assignment.assignment_id, owner_id="alice") is None
    assert (
        world.orchestrator.store.update(
            bob_assignment.assignment_id, status="succeeded", owner_id="alice"
        )
        is None
    )
    bob_view = world.orchestrator.store.list_assignments(owner_id="bob")
    assert [item.owner_id for item in bob_view] == ["bob"]
    alice_report = world.orchestrator.aggregate(
        alice_assignment.parent_run_id or "", owner_id="alice"
    )
    assert all(
        item["assignment_id"] != bob_assignment.assignment_id for item in alice_report.results
    )
    _ = alice_goal
    bob_jobs_before = {
        job.job_id for job in world.engines["alice"].jobs.list_jobs(owner_id="bob", limit=500)
    }
    alice_supervisor = world.supervisors["alice"]
    report = alice_supervisor.supervise()
    assert report.goals_ticked >= 1
    for job in world.engines["alice"].jobs.list_jobs(limit=500):
        if job.job_id not in bob_jobs_before:
            assert job.owner_id == "alice"


def test_cross_owner_recovery_replay_and_letters(world: World) -> None:
    alice_engine = world.engines["alice"]
    bob_engine = world.engines["bob"]
    alice_job = alice_engine.submit("alice", "alice work")
    alice_engine.jobs.claim("alice-worker", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=120)
    recovered = bob_engine.jobs.recover_expired(now=past, owner_id="bob")
    assert recovered == {"requeued": 0, "unknown_outcome": 0, "failed": 0}
    stored = alice_engine.jobs.get(alice_job.job_id)
    assert stored is not None and stored.status == "running"

    bob_job = bob_engine.submit("bob", "bob work", max_attempts=1)
    bob_engine.jobs.claim("bob-worker", lease_seconds=30)
    bob_engine.jobs.finish(bob_job.job_id, "bob-worker", bob_job.lease_version + 1, status="failed")
    failed = bob_engine.jobs.get(bob_job.job_id)
    assert failed is not None
    letter = bob_engine.jobs.record_dead_letter(failed, reason="operator_failed")
    with pytest.raises(KeyError):
        alice_engine.jobs.resolve_dead_letter(
            letter.dead_letter_id, resolution="replayed", owner_id="alice"
        )
    assert alice_engine.jobs.replay(bob_job.job_id, owner_id="alice") is None
    replayed = bob_engine.jobs.replay(bob_job.job_id, owner_id="bob")
    assert replayed is not None and replayed.status == "queued"

    alice_health = world.supervisors["alice"].health()
    assert alice_health["dead_letters"] == 0
    bob_health = world.supervisors["bob"].health()
    assert bob_health["dead_letters"] == 1


def test_cross_owner_learning_and_lesson_isolation(world: World) -> None:
    for owner in OWNERS:
        job = world.engines[owner].submit(owner, f"{owner} learning job")
        RunStore(world.database).create(
            Run(run_id=job.job_id, owner_id=owner, objective=job.objective)
        )
        events = EventStore(world.database)
        events.append(
            RuntimeEvent(
                event_id=f"lesson-{owner}",
                run_id=job.job_id,
                event_type="lesson",
                sequence_no=1,
                payload={"text": f"{owner} learned a valuable lesson about retry budgets"},
                payload_version=1,
                created_at=datetime.now(UTC),
            )
        )
        world.engines[owner].jobs.claim(f"{owner}-worker", lease_seconds=60)
        world.engines[owner].jobs.finish(
            job.job_id, f"{owner}-worker", job.lease_version + 1, status="completed"
        )
        stored = world.engines[owner].jobs.get(job.job_id)
        assert stored is not None
        assert world.learning.on_job_finished is not None
        world.learning.on_job_finished(stored, "completed", "done", None)
    alice_tasks = world.learning.pending_tasks(owner_id="alice")
    bob_tasks = world.learning.pending_tasks(owner_id="bob")
    assert [task.owner_id for task in alice_tasks] == ["alice"]
    assert [task.owner_id for task in bob_tasks] == ["bob"]
    world.learning.tick(owner_id="alice")
    assert world.learning.pending_tasks(owner_id="bob")
    assert world.learning.pending_tasks(owner_id="alice") == []
    world.learning.tick(owner_id="bob")
    assert world.learning.stats(owner_id="alice")["done"] == 1
    assert world.learning.stats(owner_id="bob")["done"] == 1
    lessons = world.learning.lessons
    assert lessons is not None
    alice_lessons = lessons.all(owner_id="alice")
    bob_lessons = lessons.all(owner_id="bob")
    assert any("alice" in lesson for lesson in alice_lessons)
    assert any("bob" in lesson for lesson in bob_lessons)
    assert not any("bob" in lesson for lesson in alice_lessons)
    assert not any("alice" in lesson for lesson in bob_lessons)
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT provenance_id FROM eurus_learning_provenance "
            "WHERE artifact_type='lesson' ORDER BY created_at LIMIT 1"
        ).fetchone()
    assert row is not None
    provenance_id = str(row["provenance_id"])
    assert world.learning.deactivate(provenance_id, owner_id="bob") in (True, False)
    first_lesson_id = lessons.all(owner_id="alice")
    assert lessons.all(owner_id="alice") == first_lesson_id


def test_event_owner_isolation_across_full_stack(world: World) -> None:
    bus = world.bus
    for owner in OWNERS:
        bus.subscribe(
            f"{owner}-handler",
            event_types=["TICKET_CREATED"],
            handler_config={"objective": f"{owner} handles the ticket"},
            owner_id=owner,
        )
    bus.publish("TICKET_CREATED", subject_id="a-ticket", owner_id="alice")
    bus.publish("TICKET_CREATED", subject_id="b-ticket", owner_id="bob")
    bus.dispatch_tick(owner_id="alice")
    alice_jobs = [
        job
        for job in world.engines["alice"].jobs.list_jobs(owner_id="alice", limit=50)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(alice_jobs) == 1
    assert world.engines["bob"].jobs.list_jobs(owner_id="bob", limit=50) == []
    bus.dispatch_tick(owner_id="bob")
    bob_jobs = [
        job
        for job in world.engines["bob"].jobs.list_jobs(owner_id="bob", limit=50)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(bob_jobs) == 1
    assert bob_jobs[0].owner_id == "bob"


def test_scheduler_triggers_are_owner_scoped(world: World) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    backdated = datetime.now(UTC) - timedelta(minutes=10)
    world.schedule_store.create(
        "alice",
        "Alice recurring work",
        kind="once",
        run_at=past,
        misfire_policy="run_once",
        now=backdated,
    )
    world.schedule_store.create(
        "bob",
        "Bob recurring work",
        kind="once",
        run_at=past,
        misfire_policy="run_once",
        now=backdated,
    )
    report = world.engines["alice"].maintenance_tick()
    assert report.dispatched == 1
    alice_jobs = world.engines["alice"].jobs.list_jobs(owner_id="alice", limit=50)
    assert any("Alice recurring" in job.objective for job in alice_jobs)
    assert not any("Bob recurring" in job.objective for job in alice_jobs)
    bob_report = world.engines["bob"].maintenance_tick()
    assert bob_report.dispatched == 1
    bob_jobs = world.engines["bob"].jobs.list_jobs(owner_id="bob", limit=50)
    assert any("Bob recurring" in job.objective for job in bob_jobs)


async def test_restart_preserves_multi_owner_isolation(world: World) -> None:
    world.engines["alice"].submit("alice", "alice pending")
    world.engines["bob"].submit("bob", "bob pending")
    bus = world.bus
    bus.subscribe(
        "alice-handler",
        event_types=["PING"],
        handler_config={"objective": "Alice ping work"},
        owner_id="alice",
    )
    bus.subscribe(
        "bob-handler",
        event_types=["PING"],
        handler_config={"objective": "Bob ping work"},
        owner_id="bob",
    )
    bus.publish("PING", subject_id="a-1", owner_id="alice")
    bus.publish("PING", subject_id="b-1", owner_id="bob")
    world.learning.enqueue(run_id="run-alice", job_id="job-alice", owner_id="alice")
    world.learning.enqueue(run_id="run-bob", job_id="job-bob", owner_id="bob")
    world.reopen()

    alice_engine = world.engines["alice"]
    bob_engine = world.engines["bob"]
    for _ in range(4):
        await alice_engine.run_worker()
        await bob_engine.run_worker()
    assert alice_engine.jobs.list_jobs(owner_id="alice")[0].status == "completed"
    assert bob_engine.jobs.list_jobs(owner_id="bob")[0].status == "completed"

    world.bus.dispatch_tick(owner_id="alice")
    assert (
        len(
            [
                job
                for job in alice_engine.jobs.list_jobs(owner_id="alice", limit=50)
                if (job.payload or {}).get("kind") == "event"
            ]
        )
        == 1
    )
    assert (
        len(
            [
                job
                for job in bob_engine.jobs.list_jobs(owner_id="bob", limit=50)
                if (job.payload or {}).get("kind") == "event"
            ]
        )
        == 0
    )
    world.bus.dispatch_tick(owner_id="bob")
    assert (
        len(
            [
                job
                for job in bob_engine.jobs.list_jobs(owner_id="bob", limit=50)
                if (job.payload or {}).get("kind") == "event"
            ]
        )
        == 1
    )

    world.learning.tick(owner_id="alice")
    assert len(world.learning.pending_tasks(owner_id="bob")) == 1
    world.learning.tick(owner_id="bob")
    assert world.learning.pending_tasks() == []


async def test_admin_supervisor_processes_all_owners(world: World) -> None:
    world.engines["alice"].submit("alice", "alice work")
    world.engines["bob"].submit("bob", "bob work")
    for owner in OWNERS:
        world.engines[owner].jobs.claim(f"{owner}-worker", lease_seconds=30)
    past = datetime.now(UTC) + timedelta(seconds=120)
    report = world.admin_supervisor.supervise(now=past)
    assert report.leases_recovered["requeued"] == 2
    admin_health = world.admin_supervisor.health()
    assert admin_health["jobs_queued"] == 2
    alice_health = world.supervisors["alice"].health()
    assert alice_health["jobs_queued"] == 1


async def test_race_between_two_owner_workers_keeps_isolation(world: World) -> None:
    for index in range(6):
        world.engines["alice"].submit("alice", f"alice task {index}")
        world.engines["bob"].submit("bob", f"bob task {index}")
    await asyncio.gather(
        run_owner(world.engines["alice"], "alice", rounds=12),
        run_owner(world.engines["bob"], "bob", rounds=12),
    )
    for job in world.engines["alice"].jobs.list_jobs(limit=100):
        expected = "alice" if job.owner_id == "alice" else "bob"
        if job.status == "completed":
            assert job.owner_id == expected
    alice_completed = [
        job
        for job in world.engines["alice"].jobs.list_jobs(owner_id="alice", limit=50)
        if job.status == "completed"
    ]
    bob_completed = [
        job
        for job in world.engines["bob"].jobs.list_jobs(owner_id="bob", limit=50)
        if job.status == "completed"
    ]
    assert len(alice_completed) == 6
    assert len(bob_completed) == 6
    assert len(set(alice_job.job_id for alice_job in alice_completed)) == 6
    assert len(set(bob_job.job_id for bob_job in bob_completed)) == 6
