from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.events.bus import EventBus
from eurus.storage.database import Database


class SimpleExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.fail_pattern: str | None = None

    async def execute(self, job: Any, context: Any) -> str:
        if self.fail_pattern and self.fail_pattern in job.objective:
            raise TimeoutError("event job failed")
        self.executed.append(job.job_id)
        return f"handled {job.objective}"


class RecordingTriggers:
    def __init__(self) -> None:
        self.triggered: list[tuple[str, dict[str, Any]]] = []

    def trigger_event_schedules(self, event: Any, *, owner_id: str | None = None) -> list[str]:
        self.triggered.append((event.event_type, dict(event.payload)))
        return [f"schedule-{len(self.triggered)}"]


class RecordingSink:
    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def deliver(self, notification: Any) -> None:
        self.delivered.append(notification.body)


def build(tmp_path: Path):
    database = Database(tmp_path / "events.sqlite3")
    executor = SimpleExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    triggers = RecordingTriggers()
    sink = RecordingSink()
    bus = EventBus(
        database,
        automation=automation,
        schedule_triggers=triggers,
        notifications=sink,
    )
    return bus, automation, executor, triggers, sink, database


async def drain(automation: AutomationEngine, bus: EventBus, *, rounds: int = 6) -> None:
    for _ in range(rounds):
        bus.dispatch_tick()
        worked = False
        for _ in range(5):
            if await automation.run_worker():
                worked = True
            else:
                break
        if not worked:
            await asyncio.sleep(0.01)
            return


async def test_publish_creates_event_and_outbox(tmp_path: Path) -> None:
    bus, _, _, _, _, _ = build(tmp_path)
    event = bus.publish(
        "TASK_CREATED",
        subject_type="job",
        subject_id="job-1",
        payload={"objective": "write docs"},
        correlation_id="corr-1",
    )
    assert event is not None
    stored = bus.store.get_event(event.event_id)
    assert stored is not None
    assert stored.payload == {"objective": "write docs"}
    assert bus.store.stats()["pending_outbox"] == 1


async def test_publish_deduplication(tmp_path: Path) -> None:
    bus, _, _, _, _, _ = build(tmp_path)
    first = bus.publish("FILE_CREATED", subject_id="file-a", dedup_key="file-a-v1")
    duplicate = bus.publish("FILE_CREATED", subject_id="file-a", dedup_key="file-a-v1")
    assert first is not None
    assert duplicate is None
    other = bus.publish("FILE_CREATED", subject_id="file-a", dedup_key="file-a-v2")
    assert other is not None
    assert bus.store.stats()["events"] == 2


async def test_subscription_fan_out_and_job_submission(tmp_path: Path) -> None:
    bus, automation, executor, _, _, _ = build(tmp_path)
    bus.subscribe(
        "analyze-new-files",
        event_types=["FILE_CREATED"],
        handler_kind="submit_job",
        handler_config={
            "objective": "Analyze the new file {{payload.path}}",
            "owner_id": "owner",
            "payload": {"source_event": "{{event_type}}"},
        },
    )
    bus.publish(
        "FILE_CREATED",
        subject_type="file",
        subject_id="file-9",
        payload={"path": "/tmp/report.md"},
    )
    await drain(automation, bus)
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1
    assert jobs[0].objective == "Analyze the new file /tmp/report.md"
    assert jobs[0].owner_id == "owner"
    assert jobs[0].source == "event"
    assert jobs[0].payload["event_type"] == "FILE_CREATED"
    assert jobs[0].payload["source_event"] == "FILE_CREATED"
    assert jobs[0].job_id.startswith("evt-")
    assert executor.executed


async def test_event_job_submission_is_idempotent(tmp_path: Path) -> None:
    bus, automation, _, _, _, _ = build(tmp_path)
    bus.subscribe(
        "webhook-handler",
        event_types=["WEBHOOK_RECEIVED"],
        handler_config={"objective": "Handle webhook"},
    )
    bus.publish("WEBHOOK_RECEIVED", payload={"url": "https://example.test"})
    bus.dispatch_tick()
    bus.dispatch_tick()
    bus.dispatch_tick()
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1


async def test_subscription_filtering_by_type_and_subject(tmp_path: Path) -> None:
    bus, automation, _, _, _, _ = build(tmp_path)
    bus.subscribe(
        "goal-watcher",
        event_types=["GOAL_COMPLETED"],
        subject_type="goal",
        handler_config={"objective": "Record goal completion"},
    )
    bus.subscribe(
        "all-watcher",
        event_types=["*"],
        handler_config={"objective": "Log everything"},
    )
    bus.publish("GOAL_COMPLETED", subject_type="goal", subject_id="goal-1")
    bus.publish("TASK_FAILED", subject_type="job", subject_id="job-1")
    bus.publish("GOAL_COMPLETED", subject_type="workflow_instance", subject_id="wf-1")
    bus.dispatch_tick()
    deliveries = []
    for event in bus.store.list_events(limit=10):
        deliveries.extend(bus.store.deliveries_for_event(event.event_id))
    assert len(deliveries) == 4
    job_objectives = [
        job.objective
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert job_objectives.count("Record goal completion") == 1
    assert job_objectives.count("Log everything") == 2


async def test_task_events_require_explicit_opt_in(tmp_path: Path) -> None:
    bus, automation, _, _, _, _ = build(tmp_path)
    bus.subscribe(
        "all-watcher",
        event_types=["*"],
        handler_config={"objective": "Log everything"},
    )
    bus.subscribe(
        "task-watcher",
        event_types=["TASK_FAILED"],
        handler_config={
            "objective": "React to task failure",
            "allow_task_events": True,
        },
    )
    bus.publish("TASK_FAILED", subject_type="job", subject_id="job-1")
    bus.dispatch_tick()
    job_objectives = [
        job.objective
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert job_objectives.count("Log everything") == 0
    assert job_objectives.count("React to task failure") == 1


async def test_disabled_subscription_is_skipped(tmp_path: Path) -> None:
    bus, automation, _, _, _, _ = build(tmp_path)
    subscription = bus.subscribe(
        "paused-watcher",
        event_types=["TASK_CREATED"],
        handler_config={"objective": "React"},
    )
    bus.set_subscription_enabled(subscription.subscription_id, False)
    bus.publish("TASK_CREATED")
    bus.dispatch_tick()
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert jobs == []
    assert bus.unsubscribe(subscription.subscription_id) is True


async def test_trigger_schedules_handler(tmp_path: Path) -> None:
    bus, automation, _, triggers, _, _ = build(tmp_path)
    bus.subscribe(
        "schedule-on-webhook",
        event_types=["WEBHOOK_RECEIVED"],
        handler_kind="trigger_schedules",
        handler_config={"owner_id": "owner"},
    )
    bus.publish("WEBHOOK_RECEIVED", payload={"event": "push"})
    bus.dispatch_tick()
    assert len(triggers.triggered) == 1
    assert triggers.triggered[0] == ("WEBHOOK_RECEIVED", {"event": "push"})


async def test_notify_handler(tmp_path: Path) -> None:
    bus, automation, _, _, sink, _ = build(tmp_path)
    bus.subscribe(
        "notify-completions",
        event_types=["WORKFLOW_COMPLETED"],
        handler_kind="notify",
        handler_config={"message": "Workflow {{subject_id}} finished"},
    )
    bus.publish("WORKFLOW_COMPLETED", subject_type="workflow_instance", subject_id="wf-42")
    bus.dispatch_tick()
    await asyncio.sleep(0.05)
    assert any("wf-42 finished" in body for body in sink.delivered)


async def test_delivery_retry_then_success(tmp_path: Path) -> None:
    bus, automation, executor, _, _, _ = build(tmp_path)
    executor.fail_pattern = "Flaky"
    bus.subscribe(
        "flaky-handler",
        event_types=["WEBHOOK_RECEIVED"],
        handler_config={"objective": "Flaky handling of {{payload.id}}"},
    )
    bus.publish("WEBHOOK_RECEIVED", payload={"id": "wh-1"})
    bus.dispatch_tick()
    await automation.run_worker()
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    event_id = jobs[0].payload["event_id"]
    deliveries = bus.store.deliveries_for_event(event_id)
    assert len(deliveries) == 1
    assert deliveries[0].status == "delivered"


async def test_handler_failure_retries_with_backoff(tmp_path: Path) -> None:
    bus, _, _, _, _, database = build(tmp_path)

    class FailingBus(EventBus):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.attempts = 0

        def _deliver(self, delivery: Any, report: Any, owner_id: str | None = None) -> str:
            self.attempts += 1
            if self.attempts <= 2:
                return "handler unavailable"
            return "delivered"

    failing = FailingBus(database, max_delivery_attempts=5)
    subscription = failing.subscribe(
        "retry-me",
        event_types=["TASK_FAILED"],
        handler_config={"objective": "Recover"},
    )
    event = failing.publish("TASK_FAILED")
    assert event is not None
    failing.store.fan_out(event)
    failing.store.mark_outbox_dispatched(event.event_id)
    now = datetime.now(UTC)
    report = failing.dispatch_tick(now=now)
    assert report.deliveries_retried == 1
    delivery = failing.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    assert delivery.next_attempt_at > now
    later = now + timedelta(seconds=10)
    report = failing.dispatch_tick(now=later)
    assert report.deliveries_retried == 1
    final = failing.store.deliveries_for_event(event.event_id)[0]
    assert final.attempts == 2
    assert final.status == "pending"
    even_later = later + timedelta(seconds=60)
    report = failing.dispatch_tick(now=even_later)
    assert report.deliveries_delivered == 1
    done = failing.store.deliveries_for_event(event.event_id)[0]
    assert done.status == "delivered"
    assert done.attempts == 3
    _ = subscription


async def test_delivery_dead_letter_and_retry(tmp_path: Path) -> None:
    bus, _, _, _, _, database = build(tmp_path)

    class AlwaysFailingBus(EventBus):
        def _deliver(self, delivery: Any, report: Any, owner_id: str | None = None) -> str:
            return "handler permanently broken"

    failing = AlwaysFailingBus(database, max_delivery_attempts=2)
    failing.subscribe(
        "doomed",
        event_types=["TASK_CANCELLED"],
        handler_config={"objective": "React"},
    )
    event = failing.publish("TASK_CANCELLED")
    assert event is not None
    now = datetime.now(UTC)
    failing.dispatch_tick(now=now)
    delivery = failing.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "pending"
    failing.dispatch_tick(now=now + timedelta(seconds=60))
    failing.dispatch_tick(now=now + timedelta(seconds=120))
    delivery = failing.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "dead"
    assert delivery.attempts == 2
    assert delivery.last_error is not None
    dead = failing.store.dead_deliveries()
    assert len(dead) == 1
    assert failing.store.retry_dead_delivery(delivery.delivery_id) is True
    revived = failing.store.deliveries_for_event(event.event_id)[0]
    assert revived.status == "pending"
    assert revived.attempts == 0


async def test_outbox_survives_restart(tmp_path: Path) -> None:
    bus, automation, _, _, _, database = build(tmp_path)
    bus.subscribe(
        "restart-watcher",
        event_types=["APPROVAL_GRANTED"],
        handler_config={"objective": "Continue approved work"},
    )
    bus.publish("APPROVAL_GRANTED", subject_id="approval-1")
    database.close()
    reopened = Database(tmp_path / "events.sqlite3")
    executor2 = SimpleExecutor()
    automation2 = AutomationEngine(
        reopened,
        executor2,
        retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
        poll_seconds=0.05,
    )
    bus2 = EventBus(reopened, automation=automation2)
    bus2.dispatch_tick()
    jobs = [
        job
        for job in automation2.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1
    assert jobs[0].payload["event_type"] == "APPROVAL_GRANTED"


async def test_event_retention_purge(tmp_path: Path) -> None:
    bus, _, _, _, _, database = build(tmp_path)
    old = bus.publish("TASK_CREATED", subject_id="old-1")
    assert old is not None
    recent = bus.publish("TASK_COMPLETED", subject_id="new-1")
    assert recent is not None
    bus.dispatch_tick()
    with database.connection() as connection:
        connection.execute(
            "UPDATE eurus_domain_events SET occurred_at=? WHERE event_id=?",
            ((datetime.now(UTC) - timedelta(days=40)).isoformat(), old.event_id),
        )
    purged = bus.store.purge(older_than_days=30)
    assert purged["events"] >= 1
    assert bus.store.get_event(old.event_id) is None
    assert bus.store.get_event(recent.event_id) is not None


async def test_stats_and_listing(tmp_path: Path) -> None:
    bus, _, _, _, _, _ = build(tmp_path)
    bus.subscribe("lister", event_types=["GOAL_CREATED"], handler_config={"objective": "x"})
    bus.publish("GOAL_CREATED", subject_type="goal", subject_id="g1")
    bus.publish("GOAL_PROGRESS_CHANGED", subject_type="goal", subject_id="g1")
    bus.dispatch_tick()
    stats = bus.store.stats()
    assert stats["events"] == 2
    assert stats["active_subscriptions"] == 1
    assert stats["pending_outbox"] == 0
    goal_events = bus.store.list_events(event_type="GOAL_CREATED")
    assert len(goal_events) == 1
    assert bus.store.list_events(subject_type="goal", limit=10).__len__() == 2
    with pytest.raises(ValueError):
        bus.store.publish("  ")


async def test_full_event_to_execution_flow(tmp_path: Path) -> None:
    bus, automation, executor, _, _, _ = build(tmp_path)
    bus.subscribe(
        "file-analysis",
        event_types=["FILE_CREATED"],
        subject_type="file",
        handler_config={
            "objective": "Analyze file {{payload.path}} and summarize it",
            "owner_id": "analyst",
        },
    )
    event = bus.publish(
        "FILE_CREATED",
        subject_type="file",
        subject_id="file-77",
        payload={"path": "/data/notes.txt"},
        correlation_id="corr-full",
    )
    assert event is not None
    await drain(automation, bus)
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert "notes.txt" in jobs[0].result
    delivery = bus.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "delivered"
