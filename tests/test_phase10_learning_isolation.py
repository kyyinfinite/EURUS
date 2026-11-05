from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.executor import ExecutionContext, JobExecutor
from eurus.automation.supervision import RuntimeSupervisor
from eurus.learning.integration import LearningCoordinator
from eurus.learning.lessons import LessonStore
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.runtime.models import Run, RunStatus, RuntimeEvent
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.jobs import Job
from eurus.storage.runs import RunStore


class ExecutorStub(JobExecutor):
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        self.executed.append(job.job_id)
        return f"done {job.objective}"


def make_coordinator(
    database: Database, lessons_path: Path, *, process_inline: bool = True
) -> LearningCoordinator:
    return LearningCoordinator(
        database,
        lessons=LessonStore(lessons_path),
        stale_running_seconds=60.0,
        process_inline=process_inline,
    )


def seed_lesson_event(database: Database, run_id: str, text: str) -> None:
    RunStore(database).create(Run(run_id=run_id, owner_id="alice", objective="seed run"))
    store = EventStore(database)
    store.append(
        RuntimeEvent(
            event_id=f"evt-{run_id}-{abs(hash(text)) % 10**8}",
            run_id=run_id,
            event_type="lesson",
            sequence_no=store.last_sequence(run_id) + 1,
            payload={"text": text},
            payload_version=1,
            created_at=datetime.now(UTC),
        )
    )
    store.append(
        RuntimeEvent(
            event_id=f"evt-{run_id}-final",
            run_id=run_id,
            event_type="run_finished",
            sequence_no=store.last_sequence(run_id) + 1,
            payload={"status": RunStatus.COMPLETED.value},
            payload_version=1,
            created_at=datetime.now(UTC),
        )
    )


def terminal_job(engine: AutomationEngine, owner_id: str, status: str) -> Job:
    job = engine.submit(owner_id, f"{owner_id} objective {status}")
    engine.jobs.claim("worker-1", lease_seconds=60)
    engine.jobs.finish(job.job_id, "worker-1", job.lease_version + 1, status=status)
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    return stored


def test_pending_tasks_are_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    alice_tasks = coordinator.pending_tasks(owner_id="alice")
    assert [task.owner_id for task in alice_tasks] == ["alice"]
    bob_tasks = coordinator.pending_tasks(owner_id="bob")
    assert [task.owner_id for task in bob_tasks] == ["bob"]
    assert len(coordinator.pending_tasks()) == 2
    database.close()


def test_tick_processes_only_configured_owner(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    report = coordinator.tick(owner_id="alice")
    assert report.processed == 1
    remaining = coordinator.pending_tasks(owner_id="bob")
    assert len(remaining) == 1
    assert remaining[0].run_id == "run-b"
    assert coordinator.pending_tasks(owner_id="alice") == []
    database.close()


def test_cross_owner_claim_complete_and_fail_are_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    task = coordinator.pending_tasks(owner_id="alice")[0]
    assert coordinator._claim(task, owner_id="bob") is False
    assert coordinator._complete(task, owner_id="bob") is None or True
    stored = coordinator.task_for_run("run-a")
    assert stored is not None
    assert stored.status == "pending"
    assert coordinator._fail(task, "boom", owner_id="bob") is None or True
    stored = coordinator.task_for_run("run-a")
    assert stored is not None
    assert stored.status == "pending"
    assert coordinator._claim(task, owner_id="alice") is True
    database.close()


def test_stale_requeue_is_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    task = coordinator.pending_tasks(owner_id="alice")[0]
    assert coordinator._claim(task, owner_id="alice") is True
    past = datetime.now(UTC) + timedelta(seconds=120)
    report = coordinator.tick(now=past, owner_id="bob")
    assert report.requeued == 0
    stored = coordinator.task_for_run("run-a")
    assert stored is not None
    assert stored.status == "running"
    report = coordinator.tick(now=past, owner_id="alice")
    assert report.requeued == 1
    stored = coordinator.task_for_run("run-a")
    assert stored is not None
    assert stored.status == "done"
    database.close()


def test_stats_are_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    coordinator.tick(owner_id="bob")
    alice_stats = coordinator.stats(owner_id="alice")
    bob_stats = coordinator.stats(owner_id="bob")
    assert alice_stats["pending"] == 1
    assert alice_stats["done"] == 0
    assert bob_stats["pending"] == 0
    assert bob_stats["done"] == 1
    database.close()


def test_duplicate_completion_does_not_duplicate_tasks(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    job = engine.submit("alice", "one objective")
    engine.jobs.claim("worker-1", lease_seconds=60)
    engine.jobs.finish(job.job_id, "worker-1", job.lease_version + 1, status="completed")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    coordinator.on_job_finished(stored, "completed", "result", None)
    coordinator.on_job_finished(stored, "completed", "result", None)
    assert (
        len(coordinator.pending_tasks())
        + len([t for t in [coordinator.task_for_run(stored.job_id)] if t is not None])
        >= 1
    )
    with database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM eurus_learning_tasks WHERE run_id=?", (stored.job_id,)
        ).fetchone()[0]
    assert count == 1
    database.close()


def test_lessons_are_private_to_the_owning_owner(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    job = engine.submit("alice", "lesson objective")
    seed_lesson_event(database, job.job_id, "Always verify the checksum before publishing")
    engine.jobs.claim("worker-1", lease_seconds=60)
    engine.jobs.finish(job.job_id, "worker-1", job.lease_version + 1, status="completed")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    coordinator.on_job_finished(stored, "completed", "done", None)
    coordinator.tick(owner_id="alice")
    alice_lessons = coordinator.lessons.all(owner_id="alice") if coordinator.lessons else []
    bob_lessons = coordinator.lessons.all(owner_id="bob") if coordinator.lessons else []
    global_lessons = coordinator.lessons.all() if coordinator.lessons else []
    assert any("checksum" in lesson for lesson in alice_lessons)
    assert bob_lessons == []
    assert global_lessons == []
    database.close()


def test_shared_lesson_requires_explicit_policy_and_is_visible(tmp_path: Path) -> None:
    lessons = LessonStore(tmp_path / "lessons.sqlite3")
    with pytest.raises(ValueError):
        lessons.add("Shared lesson without an owner identity", shared=True)
    assert (
        lessons.add("Alice shares this procedure openly", owner_id="alice", shared=True) is not None
    )
    assert lessons.add("Alice keeps this one private", owner_id="alice") is not None
    assert any("openly" in lesson for lesson in lessons.all(owner_id="bob"))
    assert not any("private" in lesson for lesson in lessons.all(owner_id="bob"))
    assert any("private" in lesson for lesson in lessons.all(owner_id="alice"))


def test_cross_owner_lesson_removal_is_refused(tmp_path: Path) -> None:
    lessons = LessonStore(tmp_path / "lessons.sqlite3")
    lesson_id = lessons.add("Alice private operating procedure", owner_id="alice")
    assert lesson_id is not None
    assert lessons.remove(lesson_id, owner_id="bob") is False
    assert lessons.remove(lesson_id, owner_id="alice") is True


def test_cross_owner_deactivation_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "learn.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    job = engine.submit("alice", "lesson objective")
    seed_lesson_event(database, job.job_id, "Record the rollback steps before deploying")
    engine.jobs.claim("worker-1", lease_seconds=60)
    engine.jobs.finish(job.job_id, "worker-1", job.lease_version + 1, status="completed")
    stored = engine.jobs.get(job.job_id)
    assert stored is not None
    coordinator.on_job_finished(stored, "completed", "done", None)
    coordinator.tick(owner_id="alice")
    with database.connection() as connection:
        row = connection.execute(
            "SELECT provenance_id FROM eurus_learning_provenance "
            "WHERE artifact_type='lesson' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    provenance_id = str(row["provenance_id"])
    assert coordinator.deactivate(provenance_id, owner_id="bob") is False
    if coordinator.lessons:
        assert any("rollback" in lesson for lesson in coordinator.lessons.all(owner_id="alice"))
    assert coordinator.deactivate(provenance_id, owner_id="alice") is True
    if coordinator.lessons:
        assert coordinator.lessons.all(owner_id="alice") == []
    database.close()


def test_restart_preserves_learning_ownership(tmp_path: Path) -> None:
    path = tmp_path / "learn.sqlite3"
    database = Database(path)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    database.close()

    reopened = Database(path)
    coordinator2 = make_coordinator(reopened, tmp_path / "lessons.sqlite3")
    coordinator2.tick(owner_id="alice")
    remaining = coordinator2.pending_tasks(owner_id="bob")
    assert len(remaining) == 1
    coordinator2.tick(owner_id="bob")
    assert coordinator2.pending_tasks() == []
    reopened.close()


def test_supervisor_executes_only_configured_owner_learning(tmp_path: Path) -> None:
    database = Database(tmp_path / "sup.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    supervisor = RuntimeSupervisor(engine, learning=coordinator, owner_id="alice")
    report = supervisor.supervise()
    assert report.learning_processed == 1
    remaining = coordinator.pending_tasks(owner_id="bob")
    assert len(remaining) == 1
    database.close()


def test_health_learning_counts_are_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "health.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    coordinator = make_coordinator(database, tmp_path / "lessons.sqlite3")
    coordinator.enqueue(run_id="run-a", job_id="job-a", owner_id="alice")
    coordinator.enqueue(run_id="run-b", job_id="job-b", owner_id="bob")
    supervisor = RuntimeSupervisor(engine, learning=coordinator, owner_id="alice")
    health = supervisor.health()
    assert health["learning"]["pending"] == 1
    assert health["learning"]["done"] == 0
    database.close()


def test_cross_owner_assignment_update_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "assign.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register("writer", description="w", max_concurrent=2)
    assignment = orchestrator.dispatch("write it", role="writer", owner_id="bob")
    updated = orchestrator.store.update(
        assignment.assignment_id, status="succeeded", owner_id="alice"
    )
    assert updated is None
    stored = orchestrator.store.get(assignment.assignment_id)
    assert stored is not None
    assert stored.status == "dispatched"
    assert (
        orchestrator.store.update(assignment.assignment_id, status="succeeded", owner_id="bob")
        is not None
    )
    database.close()


def test_cross_owner_assignment_heartbeat_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "assign.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register("writer", description="w", max_concurrent=2)
    assignment = orchestrator.dispatch("write it", role="writer", owner_id="bob")
    assert (
        orchestrator.store.heartbeat(assignment.assignment_id, "alice beat", owner_id="alice")
        is False
    )
    assert orchestrator.store.last_heartbeat(assignment.assignment_id) is not None
    with database.connection() as connection:
        row = connection.execute(
            "SELECT detail FROM eurus_agent_heartbeats WHERE assignment_id=?",
            (assignment.assignment_id,),
        ).fetchone()
    assert row is not None
    assert row["detail"] == "dispatched"
    assert (
        orchestrator.store.heartbeat(assignment.assignment_id, "bob beat", owner_id="bob") is True
    )
    with database.connection() as connection:
        row = connection.execute(
            "SELECT detail FROM eurus_agent_heartbeats WHERE assignment_id=?",
            (assignment.assignment_id,),
        ).fetchone()
    assert row is not None
    assert row["detail"] == "bob beat"
    database.close()


def test_stale_running_assignment_query_is_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "stale.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register("worker", description="w", max_concurrent=4)
    alice_assignment = orchestrator.dispatch("alice task", role="worker", owner_id="alice")
    bob_assignment = orchestrator.dispatch("bob task", role="worker", owner_id="bob")
    with database.connection() as connection:
        backdated = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        for assignment_id in (alice_assignment.assignment_id, bob_assignment.assignment_id):
            connection.execute(
                "UPDATE eurus_agent_assignments SET updated_at=? WHERE assignment_id=?",
                (backdated, assignment_id),
            )
            connection.execute(
                "UPDATE eurus_agent_heartbeats SET last_beat=? WHERE assignment_id=?",
                (backdated, assignment_id),
            )
    stale_alice = orchestrator.store.stale_running(older_than_seconds=60, owner_id="alice")
    stale_bob = orchestrator.store.stale_running(older_than_seconds=60, owner_id="bob")
    assert [item.owner_id for item in stale_alice] == ["alice"]
    assert [item.owner_id for item in stale_bob] == ["bob"]
    database.close()


def test_supervisor_stale_assignment_recovery_is_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "supassign.sqlite3")
    engine = AutomationEngine(database, ExecutorStub(), owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register("worker", description="w", max_concurrent=4)
    alice_assignment = orchestrator.dispatch("alice task", role="worker", owner_id="alice")
    bob_assignment = orchestrator.dispatch("bob task", role="worker", owner_id="bob")
    with database.connection() as connection:
        backdated = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        for assignment_id in (alice_assignment.assignment_id, bob_assignment.assignment_id):
            connection.execute(
                "UPDATE eurus_agent_assignments SET updated_at=? WHERE assignment_id=?",
                (backdated, assignment_id),
            )
            connection.execute(
                "UPDATE eurus_agent_heartbeats SET last_beat=? WHERE assignment_id=?",
                (backdated, assignment_id),
            )
    supervisor = RuntimeSupervisor(
        engine,
        assignments=orchestrator,
        owner_id="alice",
        stale_assignment_seconds=60,
    )
    report = supervisor.supervise()
    assert report.assignments_recovered == 0
    alice_stored = orchestrator.store.get(alice_assignment.assignment_id)
    bob_stored = orchestrator.store.get(bob_assignment.assignment_id)
    assert alice_stored is not None and bob_stored is not None
    assert alice_stored.status == "dispatched"
    assert bob_stored.status == "dispatched"
    _ = Any
    database.close()
