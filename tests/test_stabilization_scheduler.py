from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore


def make_world(tmp_path: Path) -> tuple[ScheduleStore, JobStore, Database]:
    path = tmp_path / "sched.sqlite3"
    store = ScheduleStore(path)
    database = Database(path)
    return store, JobStore(database), database


def test_chain_schedule_requires_existing_same_owner_dependency(tmp_path: Path) -> None:
    store, _, database = make_world(tmp_path)
    parent = store.create(
        "alice",
        "nightly report",
        kind="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    chained = store.create(
        "alice",
        "follow-up",
        kind="chain",
        trigger_schedule=parent.schedule_id,
    )
    assert chained.kind == "chain"
    assert chained.trigger_schedule == parent.schedule_id
    with pytest.raises(ValueError, match="same owner"):
        store.create("bob", "cross owner", kind="chain", trigger_schedule=parent.schedule_id)
    with pytest.raises(ValueError, match="existing schedule"):
        store.create("alice", "dangling", kind="chain", trigger_schedule="missing-id")
    database.close()


def test_chain_trigger_fires_only_on_successful_completion(tmp_path: Path) -> None:
    store, jobs, database = make_world(tmp_path)
    parent = store.create("alice", "nightly report", kind="interval", interval_seconds=3600)
    store.create("alice", "follow-up", kind="chain", trigger_schedule=parent.schedule_id)
    first_run = store.trigger_now(parent.schedule_id, "alice")
    assert first_run is not None
    store.finish_run(first_run, status="failed", result="boom")
    assert store.pending_occurrences(owner_id="alice") == []
    second_run = store.trigger_now(parent.schedule_id, "alice")
    assert second_run is not None
    store.finish_run(second_run, status="completed", result="ok")
    chained = [schedule for schedule in store.list_owner("alice") if schedule.kind == "chain"][0]
    occurrences = store.occurrences(chained.schedule_id)
    assert len(occurrences) == 1
    assert occurrences[0].status == "dispatched"
    chain_jobs = jobs.list_jobs(owner_id="alice", statuses=("queued",))
    assert any(job.schedule_id != parent.schedule_id for job in chain_jobs)
    database.close()


def test_chain_trigger_is_deduplicated_per_dependency_run(tmp_path: Path) -> None:
    store, _, database = make_world(tmp_path)
    parent = store.create("alice", "nightly report", kind="interval", interval_seconds=3600)
    chained = store.create("alice", "follow-up", kind="chain", trigger_schedule=parent.schedule_id)
    run_id = store.trigger_now(parent.schedule_id, "alice")
    assert run_id is not None
    assert store.finish_run(run_id, status="completed", result="ok") is True
    assert store.finish_run(run_id, status="completed", result="ok") is False
    occurrences = store.occurrences(chained.schedule_id)
    assert len(occurrences) == 1
    database.close()


def test_paused_chain_schedule_does_not_fire(tmp_path: Path) -> None:
    store, jobs, database = make_world(tmp_path)
    parent = store.create("alice", "nightly report", kind="interval", interval_seconds=3600)
    chained = store.create("alice", "follow-up", kind="chain", trigger_schedule=parent.schedule_id)
    assert store.pause(chained.schedule_id, "alice") is True
    run_id = store.trigger_now(parent.schedule_id, "alice")
    assert run_id is not None
    store.finish_run(run_id, status="completed", result="ok")
    assert jobs.list_jobs(owner_id="alice", statuses=("queued",)) != []
    chain_jobs = [
        job for job in jobs.list_jobs(owner_id="alice") if job.schedule_id == chained.schedule_id
    ]
    assert chain_jobs == []
    assert store.resume(chained.schedule_id, "alice") is True
    second_run = store.trigger_now(parent.schedule_id, "alice")
    assert second_run is not None
    store.finish_run(second_run, status="completed", result="ok")
    chain_jobs = [
        job for job in jobs.list_jobs(owner_id="alice") if job.schedule_id == chained.schedule_id
    ]
    assert len(chain_jobs) == 1
    database.close()


def test_chain_jobs_carry_the_owner_of_the_chain_schedule(tmp_path: Path) -> None:
    store, jobs, database = make_world(tmp_path)
    parent = store.create("alice", "parent work", kind="interval", interval_seconds=3600)
    with pytest.raises(ValueError, match="same owner"):
        store.create("bob", "bob follow-up", kind="chain", trigger_schedule=parent.schedule_id)
    bob_parent = store.create("bob", "bob root", kind="interval", interval_seconds=3600)
    bob_chain = store.create(
        "bob", "bob follow-up", kind="chain", trigger_schedule=bob_parent.schedule_id
    )
    run_id = store.trigger_now(bob_parent.schedule_id, "bob")
    assert run_id is not None
    store.finish_run(run_id, status="completed", result="ok")
    bob_jobs = [
        job
        for job in jobs.list_jobs(owner_id="bob", statuses=("queued",))
        if job.schedule_id == bob_chain.schedule_id
    ]
    assert len(bob_jobs) == 1
    assert bob_jobs[0].objective == "bob follow-up"
    database.close()


def test_finalize_occurrence_completes_dispatched_occurrences(tmp_path: Path) -> None:
    store, _, database = make_world(tmp_path)
    parent = store.create(
        "alice",
        "finalized work",
        kind="once",
        run_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    job_ids = store.dispatch_due(now=datetime.now(UTC) + timedelta(minutes=6))
    assert job_ids
    assert store.finalize_occurrence(job_ids[0], "completed", "done") is True
    occurrences = store.occurrences(parent.schedule_id)
    assert occurrences[0].status == "completed"
    assert occurrences[0].finished_at is not None
    assert store.finalize_occurrence(job_ids[0], "completed", "done") is False
    database.close()


def test_manual_trigger_creates_dispatched_job(tmp_path: Path) -> None:
    store, jobs, database = make_world(tmp_path)
    schedule = store.create(
        "alice",
        "manual target",
        kind="interval",
        interval_seconds=3600,
    )
    job_id = store.trigger_now(schedule.schedule_id, "alice")
    assert job_id is not None
    queued = jobs.list_jobs(owner_id="alice", statuses=("queued",))
    assert len(queued) == 1
    assert queued[0].schedule_id == schedule.schedule_id
    occurrences = store.occurrences(schedule.schedule_id)
    assert occurrences[0].status == "dispatched"
    database.close()


def test_revision_history_records_updates(tmp_path: Path) -> None:
    store, _, database = make_world(tmp_path)
    schedule = store.create("alice", "revised work", kind="interval", interval_seconds=3600)
    updated = store.update(schedule.schedule_id, "alice", priority=2)
    assert updated is not None
    assert updated.revision == 2
    revisions = store.revisions(schedule.schedule_id)
    assert len(revisions) == 2
    assert revisions[0].revision == 2
    assert revisions[0].definition_json
    database.close()
