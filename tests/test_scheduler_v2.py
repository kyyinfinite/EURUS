from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from eurus.scheduler import ScheduleStore, TaskProfile
from eurus.storage.jobs import JobStore

JAKARTA = ZoneInfo("Asia/Jakarta")


def prepare(tmp_path: Path, name: str = "schedules.sqlite3") -> tuple[ScheduleStore, JobStore]:
    path = tmp_path / name
    jobs = JobStore(path)
    return ScheduleStore(path), jobs


def test_cron_schedule_creates_and_dispatches(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "hourly digest",
        kind="cron",
        cron_expr="0 * * * *",
        timezone="UTC",
        now=now,
    )
    assert schedule.kind == "cron"
    assert schedule.next_run_at == datetime(2026, 11, 2, 11, 0, tzinfo=UTC).isoformat()
    first = store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    assert len(first) == 1
    updated = store.get(schedule.schedule_id)
    assert updated.next_run_at == datetime(2026, 11, 2, 12, 0, tzinfo=UTC).isoformat()
    assert store.dispatch_due(now=datetime(2026, 11, 2, 11, 30, tzinfo=UTC)) == []
    second = store.dispatch_due(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    assert len(second) == 1
    occurrences = store.occurrences(schedule.schedule_id)
    assert len(occurrences) == 2
    assert all(item.status == "dispatched" for item in occurrences)


def test_cron_schedule_respects_timezone(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "jakarta morning brief",
        kind="cron",
        cron_expr="0 9 * * *",
        timezone="Asia/Jakarta",
        now=now,
    )
    local_due = datetime(2026, 11, 2, 9, 0, tzinfo=JAKARTA)
    assert schedule.next_run_at == local_due.astimezone(UTC).isoformat()
    dispatched = store.dispatch_due(now=local_due.astimezone(UTC))
    assert len(dispatched) == 1


def test_invalid_cron_expression_rejected(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    with pytest.raises(ValueError, match="cron"):
        store.create("owner", "broken", kind="cron", cron_expr="99 * * * *")
    with pytest.raises(ValueError):
        store.create("owner", "broken", kind="cron")


def test_occurrence_dedup_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "dedup.sqlite3"
    JobStore(path)
    store = ScheduleStore(path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create("owner", "daily", kind="cron", cron_expr="0 12 * * *", now=now)
    due = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    first = store.dispatch_due(now=due)
    assert len(first) == 1
    again = ScheduleStore(path).dispatch_due(now=due)
    assert again == []
    occurrences = store.occurrences(store.list_owner("owner")[0].schedule_id)
    assert len(occurrences) == 1


def test_concurrent_scheduler_instances_do_not_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    JobStore(path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store_a = ScheduleStore(path)
    store_a.create("owner", "once only", kind="once", run_at="2026-11-02T10:05:00+00:00", now=now)
    due = datetime(2026, 11, 2, 10, 5, tzinfo=UTC)
    store_b = ScheduleStore(path)
    jobs_a = store_a.dispatch_due(now=due)
    jobs_b = store_b.dispatch_due(now=due)
    assert len(jobs_a) + len(jobs_b) == 1


def test_misfire_skip_policy(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "careful task",
        kind="cron",
        cron_expr="0 * * * *",
        misfire_policy="skip",
        now=now,
    )
    late = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    dispatched = store.dispatch_due(now=late, misfire_grace_seconds=300)
    assert len(dispatched) == 1
    occurrences = store.occurrences(schedule.schedule_id)
    statuses = {item.status for item in occurrences}
    assert "skipped" in statuses
    assert sum(1 for item in occurrences if item.status == "dispatched") == 1


def test_misfire_run_once_policy(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create(
        "owner",
        "run once when late",
        kind="cron",
        cron_expr="0 * * * *",
        misfire_policy="run_once",
        now=now,
    )
    late = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    dispatched = store.dispatch_due(now=late, misfire_grace_seconds=300)
    assert len(dispatched) == 2
    occurrences = store.occurrences(store.list_owner("owner")[0].schedule_id)
    dispatched_count = sum(1 for item in occurrences if item.status == "dispatched")
    coalesced_count = sum(1 for item in occurrences if item.status == "coalesced")
    assert dispatched_count == 2
    assert coalesced_count >= 1


def test_misfire_catch_up_policy_respects_limit(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create(
        "owner",
        "catch up work",
        kind="cron",
        cron_expr="0 * * * *",
        misfire_policy="catch_up",
        catch_up_limit=3,
        now=now,
    )
    late = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    dispatched = store.dispatch_due(now=late, misfire_grace_seconds=300)
    assert len(dispatched) == 4


def test_misfire_coalesce_policy_dispatches_single_job(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create(
        "owner",
        "coalesced work",
        kind="cron",
        cron_expr="0 * * * *",
        misfire_policy="coalesce",
        now=now,
    )
    late = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    dispatched = store.dispatch_due(now=late, misfire_grace_seconds=300)
    assert len(dispatched) == 1


def test_per_schedule_concurrency_limits_dispatch(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "serial task",
        kind="cron",
        cron_expr="0 * * * *",
        concurrency_limit=1,
        now=now,
    )
    first = store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    assert len(first) == 1
    claimed = jobs.claim(
        "worker-1", lease_seconds=120, now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
    )
    assert claimed is not None
    assert claimed.schedule_id == schedule.schedule_id
    second = store.dispatch_due(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    assert second == []
    jobs.finish(
        claimed.job_id,
        "worker-1",
        claimed.lease_version,
        status="completed",
        result="ok",
        now=datetime(2026, 11, 2, 11, 1, tzinfo=UTC),
    )
    third = store.dispatch_due(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    assert len(third) == 1


def test_execution_window_defers_dispatch(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    store.create(
        "owner",
        "business hours",
        kind="cron",
        cron_expr="0 * * * *",
        execution_window_start="09:00",
        execution_window_end="17:00",
        timezone="UTC",
        now=now,
    )
    outside = store.dispatch_due(now=datetime(2026, 11, 2, 18, 0, tzinfo=UTC))
    assert outside == []
    inside = store.dispatch_due(now=datetime(2026, 11, 2, 16, 0, tzinfo=UTC))
    assert len(inside) == 1


def test_execution_window_overnight(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    store.create(
        "owner",
        "night shift",
        kind="cron",
        cron_expr="0 * * * *",
        execution_window_start="22:00",
        execution_window_end="06:00",
        timezone="UTC",
        now=now,
    )
    assert store.dispatch_due(now=datetime(2026, 11, 2, 10, 0, tzinfo=UTC)) == []
    assert len(store.dispatch_due(now=datetime(2026, 11, 2, 23, 0, tzinfo=UTC))) == 1


def test_schedule_expiry_disables_and_skips(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "expiring",
        kind="cron",
        cron_expr="0 * * * *",
        expires_at="2026-11-02T12:00:00+00:00",
        now=now,
    )
    before = store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    assert len(before) == 1
    after = store.dispatch_due(now=datetime(2026, 11, 2, 12, 30, tzinfo=UTC))
    assert after == []
    updated = store.get(schedule.schedule_id)
    assert updated.enabled is False


def test_max_occurrences_disables_schedule(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "twice only",
        kind="cron",
        cron_expr="0 * * * *",
        max_occurrences=2,
        now=now,
    )
    assert len(store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))) == 1
    assert len(store.dispatch_due(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))) == 1
    assert store.get(schedule.schedule_id).enabled is False
    assert store.dispatch_due(now=datetime(2026, 11, 2, 13, 0, tzinfo=UTC)) == []


def test_manual_trigger_creates_job_immediately(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "manual run",
        kind="cron",
        cron_expr="0 0 1 1 *",
        now=now,
    )
    job_id = store.trigger_now(schedule.schedule_id, "owner", now=now)
    assert job_id is not None
    job = jobs.get(job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.schedule_id == schedule.schedule_id
    occurrences = store.occurrences(schedule.schedule_id)
    assert len(occurrences) == 1
    assert occurrences[0].status == "dispatched"
    assert store.trigger_now("missing", "owner") is None


def test_schedule_edit_creates_revision(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "original objective",
        kind="cron",
        cron_expr="0 9 * * *",
        now=now,
    )
    updated = store.update(
        schedule.schedule_id,
        "owner",
        objective="revised objective",
        cron_expr="30 10 * * *",
        priority=2,
    )
    assert updated is not None
    assert updated.revision == 2
    assert updated.objective == "revised objective"
    assert updated.cron_expr == "30 10 * * *"
    assert updated.priority == 2
    history = store.revisions(schedule.schedule_id)
    assert [item.revision for item in history] == [2, 1]
    import json

    original = json.loads(history[-1].definition_json)
    assert original["objective"] == "original objective"
    assert store.update("missing", "owner", objective="x") is None
    with pytest.raises(ValueError):
        store.update(schedule.schedule_id, "owner", cron_expr="bad expr")


def test_event_triggered_schedule(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create(
        "owner",
        "react to webhook",
        kind="event",
        trigger_event="WEBHOOK_RECEIVED",
        now=now,
    )
    other = store.trigger_event_schedules("GOAL_COMPLETED", now=now)
    assert other == []
    matched = store.trigger_event_schedules("WEBHOOK_RECEIVED", now=now)
    assert len(matched) == 1
    job = jobs.get(matched[0])
    assert job is not None
    assert job.objective == "react to webhook"


def test_event_schedule_subject_filter(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create(
        "owner",
        "watch inbox",
        kind="event",
        trigger_event="FILE_DROPPED",
        trigger_subject="reports",
        now=now,
    )
    assert store.trigger_event_schedules("FILE_DROPPED", subject_id="other", now=now) == []
    assert len(store.trigger_event_schedules("FILE_DROPPED", subject_id="reports", now=now)) == 1


def test_finalize_occurrence_from_job_status(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "tracked",
        kind="cron",
        cron_expr="0 * * * *",
        now=now,
    )
    job_ids = store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    assert len(job_ids) == 1
    assert store.finalize_occurrence(job_ids[0], "completed", "all good") is True
    occurrences = store.occurrences(schedule.schedule_id)
    assert occurrences[0].status == "completed"
    assert occurrences[0].finished_at is not None
    assert store.finalize_occurrence(job_ids[0], "completed", "again") is False
    assert store.finalize_occurrence(job_ids[0], "bogus", "") is False


def test_priority_orders_dispatch(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create("owner", "low", kind="cron", cron_expr="0 12 * * *", priority=9, now=now)
    store.create("owner", "high", kind="cron", cron_expr="0 12 * * *", priority=1, now=now)
    dispatched = store.dispatch_due(now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC), limit=1)
    assert len(dispatched) == 1
    job = jobs.get(dispatched[0])
    assert job is not None
    assert job.objective == "high"
    assert job.priority == 1


def test_dispatch_during_restart_no_duplicate_jobs(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    jobs = JobStore(path)
    store = ScheduleStore(path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    store.create("owner", "once", kind="once", run_at="2026-11-02T10:01:00+00:00", now=now)
    due = datetime(2026, 11, 2, 10, 1, tzinfo=UTC)
    partial = store.dispatch_due(now=due)
    assert len(partial) == 1
    reopened = ScheduleStore(path)
    assert reopened.dispatch_due(now=due) == []
    all_jobs = jobs.list_jobs(limit=10)
    assert len(all_jobs) == 1


def test_failed_scheduled_execution_finalizes_occurrence(tmp_path: Path) -> None:
    store, jobs = prepare(tmp_path)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "failing task",
        kind="cron",
        cron_expr="0 * * * *",
        now=now,
    )
    store.dispatch_due(now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    claimed = jobs.claim("worker", lease_seconds=60, now=datetime(2026, 11, 2, 11, 0, tzinfo=UTC))
    assert claimed is not None
    jobs.finish(
        claimed.job_id,
        "worker",
        claimed.lease_version,
        status="failed",
        error="TimeoutError",
        now=datetime(2026, 11, 2, 11, 1, tzinfo=UTC),
    )
    assert store.finalize_occurrence(claimed.job_id, "failed", "TimeoutError") is True
    occurrences = store.occurrences(schedule.schedule_id)
    assert occurrences[0].status == "failed"
    assert occurrences[0].error == "TimeoutError"


def test_interval_schedule_legacy_fields_preserved(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    profile = TaskProfile("focused", max_steps=9, allowed_tools=("read_file",))
    store.save_profile("owner", profile)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    schedule = store.create(
        "owner",
        "legacy interval",
        kind="interval",
        interval_seconds=120,
        profile=profile,
        now=now,
    )
    assert schedule.profile["max_steps"] == 9
    assert schedule.misfire_policy == "coalesce"
    assert schedule.concurrency_limit == 1
    assert schedule.revision == 1
    restored = ScheduleStore(store.path).get(schedule.schedule_id)
    assert restored is not None
    assert restored.profile["allowed_tools"] == ["read_file"]


def test_schedule_validation_errors(tmp_path: Path) -> None:
    store, _ = prepare(tmp_path)
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="cron", cron_expr="0 * * * *", catch_up_limit=0)
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="cron", cron_expr="0 * * * *", concurrency_limit=0)
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="cron", cron_expr="0 * * * *", priority=0)
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="cron", cron_expr="0 * * * *", max_occurrences=0)
    with pytest.raises(ValueError):
        store.create(
            "owner", "x", kind="cron", cron_expr="0 * * * *", execution_window_start="09:00"
        )
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="cron", cron_expr="0 * * * *", execution_window_start="9am")
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="event")
    with pytest.raises(ValueError):
        store.create("owner", "x", kind="unknown")
