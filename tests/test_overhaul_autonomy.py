from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.events.store import DomainEventStore
from eurus.goals.engine import GoalEngine
from eurus.goals.states import GoalTaskStatus
from eurus.learning.skills import SkillDraftStore
from eurus.memory.manager import MemoryManager
from eurus.storage.database import Database


def _automation(tmp_path: Path) -> Any:
    from eurus.automation.engine import AutomationEngine

    class NoopExecutor:
        async def execute(self, job: object, context: object) -> str:
            return "noop"

    return AutomationEngine(Database(tmp_path / "jobs.sqlite3"), NoopExecutor())


def _goal_engine(tmp_path: Path, **kwargs: Any) -> GoalEngine:
    automation = _automation(tmp_path)
    return GoalEngine(automation.database, automation, **kwargs)


def test_goal_completes_after_replan_with_cancelled_tasks(tmp_path: Path) -> None:
    engine = _goal_engine(tmp_path)
    goal = engine.create_goal("owner", "ship the feature")
    goal_id = goal.goal_id
    store = engine.store
    first = store.add_task(goal_id, "first attempt")
    store.update_task(first.task_id, status=GoalTaskStatus.CANCELLED)
    second = store.add_task(goal_id, "second attempt")
    store.update_task(second.task_id, status=GoalTaskStatus.SUCCEEDED)

    evaluation = engine.evaluate(goal_id)
    assert evaluation.all_tasks_succeeded is True
    assert evaluation.any_task_failed is False


def test_goal_fails_when_only_failed_tasks_remain(tmp_path: Path) -> None:
    engine = _goal_engine(tmp_path)
    goal = engine.create_goal("owner", "ship it")
    store = engine.store
    task = store.add_task(goal.goal_id, "attempt")
    store.update_task(task.task_id, status=GoalTaskStatus.FAILED)
    evaluation = engine.evaluate(goal.goal_id)
    assert evaluation.all_tasks_succeeded is False
    assert evaluation.any_task_failed is True


def test_goal_active_limit_is_enforced(tmp_path: Path) -> None:
    engine = _goal_engine(tmp_path, max_active_goals=1)
    engine.create_goal("owner", "first goal")
    with pytest.raises(ValueError, match="active goals"):
        engine.create_goal("owner", "second goal")


def test_goal_progress_write_is_cas_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eurus.goals.store import ConcurrentGoalUpdate, GoalStore

    store = GoalStore(Database(tmp_path / "goals.sqlite3"))
    goal = store.create("owner", "objective")
    stale = store.get(goal.goal_id)
    assert stale is not None
    store.set_progress(goal.goal_id, 0.5)
    monkeypatch.setattr(store, "get", lambda _goal_id, **_: stale)
    with pytest.raises(ConcurrentGoalUpdate):
        store.set_progress(goal.goal_id, 0.9)


def test_event_purge_keeps_pending_deliveries(tmp_path: Path) -> None:
    store = DomainEventStore(Database(tmp_path / "events.sqlite3"))
    old = datetime.now(UTC) - timedelta(days=60)
    event = store.publish(
        "TASK_COMPLETED",
        subject_type="task",
        subject_id="t-1",
        payload={},
        owner_id="",
    )
    assert event is not None
    store.create_subscription(
        "sub",
        event_types=["TASK_COMPLETED"],
        subject_type="task",
        handler_kind="notify",
        handler_config={"message": "m"},
        owner_id="",
    )
    store.fan_out(event, now=old)
    store.mark_outbox_dispatched(event.event_id)
    with store.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_domain_events SET occurred_at=? WHERE event_id=?",
            (old.isoformat(), event.event_id),
        )
    purged = store.purge(older_than_days=30)
    assert purged["events"] == 0
    assert store.get_event(event.event_id) is not None

    due = store.due_deliveries(owner_id=None)
    assert due
    store.ack_delivery(store.mark_delivering(due[0].delivery_id).delivery_id)
    acked = store.deliveries_for_event(event.event_id)[0]
    assert acked.status == "delivered"
    purged = store.purge(older_than_days=30)
    assert purged["events"] == 1
    assert store.get_event(event.event_id) is None


def test_workflow_approval_timeout_rejects_stale_requests(tmp_path: Path) -> None:
    from eurus.workflows.definition import WorkflowDefinition
    from eurus.workflows.engine import WorkflowEngine
    from eurus.workflows.store import WorkflowStore

    automation = _automation(tmp_path)
    store = WorkflowStore(automation.database)
    from eurus.workflows.definition import NodeSpec

    definition = WorkflowDefinition(
        workflow_id="wf-timeout",
        name="timeout",
        nodes=[
            NodeSpec(
                node_id="approve",
                node_type="approval",
                config={"prompt": "ok?", "timeout_seconds": 1},
            )
        ],
    )
    store.save_definition(definition)
    engine = WorkflowEngine(automation.database, automation=automation)
    instance = engine.start("wf-timeout", "local")
    engine.advance_instance(instance.instance_id, owner_id="local")
    approvals = store.list_approvals(instance_id=instance.instance_id, pending=True)
    assert approvals
    with automation.database.connection() as connection:
        connection.execute(
            "UPDATE eurus_workflow_approvals SET requested_at=? WHERE approval_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=600)).isoformat(),
                approvals[0].approval_id,
            ),
        )
    engine.advance_instance(instance.instance_id, owner_id="local")
    node = store.get_node(instance.instance_id, "approve")
    assert node is not None and node.status.value == "failed"
    decided = store.get_approval(approvals[0].approval_id)
    assert decided is not None and decided.decision == "rejected"
    assert decided.decided_by == "approval_timeout"


def test_scheduled_jobs_get_retry_budget(tmp_path: Path) -> None:
    from eurus.scheduler import SCHEDULED_JOB_MAX_ATTEMPTS, ScheduleStore
    from eurus.storage.jobs import JobStore

    database = Database(tmp_path / "sched.sqlite3")
    store = ScheduleStore(tmp_path / "sched.sqlite3")
    run_at = datetime.now(UTC) + timedelta(minutes=1)
    store.create("owner", "do the thing", kind="once", run_at=run_at.isoformat())
    dispatched = store.dispatch_due(now=datetime.now(UTC) + timedelta(minutes=5))
    assert dispatched
    jobs = JobStore(database)
    job = jobs.get(dispatched[0])
    assert job is not None
    assert job.max_attempts == SCHEDULED_JOB_MAX_ATTEMPTS
    assert SCHEDULED_JOB_MAX_ATTEMPTS > 1


def test_remember_memory_tool_routes_through_manager(tmp_path: Path) -> None:
    import eurus.agent.capability_tools as capability_tools
    from eurus.agent.capability_tools import _remember
    from eurus.tools.base import ToolContext

    context = ToolContext(workspace=tmp_path)
    saved: tuple[str, dict[str, Any]] | None = None

    class RecordingManager:
        def remember(self, content: str, **kwargs: Any) -> int:
            nonlocal saved
            saved = (content, kwargs)
            return 7

    original = capability_tools._memory
    capability_tools._memory = lambda owner_id=None: RecordingManager()
    try:
        result = asyncio.run(_remember({"content": "important fact"}, context))
    finally:
        capability_tools._memory = original
    assert result == "Saved memory #7."
    assert saved is not None
    assert saved[0] == "important fact"
    assert saved[1]["trust"] == "user_approved"


def test_remember_observation_quarantines_external_content(tmp_path: Path) -> None:
    import eurus.agent.capability_tools as capability_tools
    from eurus.agent.capability_tools import _remember_observation
    from eurus.tools.base import ToolContext

    context = ToolContext(workspace=tmp_path, run_id="run-1")
    quarantined: tuple[str, dict[str, Any]] | None = None

    class RecordingManager:
        def remember_untrusted(self, content: str, **kwargs: Any) -> int:
            nonlocal quarantined
            quarantined = (content, kwargs)
            return 9

    original = capability_tools._memory
    capability_tools._memory = lambda owner_id=None: RecordingManager()
    try:
        result = asyncio.run(
            _remember_observation(
                {"content": "found on a webpage", "source": "https://example.test"},
                context,
            )
        )
    finally:
        capability_tools._memory = original
    assert "Quarantined" in result
    assert quarantined is not None
    assert quarantined[0] == "found on a webpage"
    assert quarantined[1]["provenance"]["source"] == "https://example.test"


def test_memory_ranking_prefers_recent_confident_used(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "mem.sqlite3")
    manager.remember("alpha deploy notes", scope="global", trust="derived")
    manager.remember("alpha deploy runbook", scope="global", trust="derived")
    store = manager.store
    rows = store.search_memories("alpha deploy", limit=2)
    assert len(rows) == 2
    loser_id = rows[1]["id"]
    with store._connection() as connection:
        connection.execute(
            "UPDATE memories SET confidence=0.95, use_count=10, updated_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), loser_id),
        )
    ranked = store.search_memories("alpha deploy", limit=2)
    assert ranked
    assert ranked[0]["id"] == loser_id
    assert ranked[0]["use_count"] >= 10


def test_expired_memories_are_excluded_from_search(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "mem.sqlite3")
    memory_id = manager.remember("beta secret notes", scope="global", trust="derived")
    store = manager.store
    with store._connection() as connection:
        connection.execute(
            "UPDATE memories SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), memory_id),
        )
    assert store.search_memories("beta secret notes") == []


def test_skill_rollback_and_retire(tmp_path: Path) -> None:
    store = SkillDraftStore()
    store.draft("rollback-skill", "d", "original content body")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    store.promote("rollback-skill", workspace)
    store.patch("rollback-skill", workspace, "d", "patched content body")
    restored = store.rollback("rollback-skill", workspace)
    assert restored.exists()
    from eurus.skills.loader import SkillError, load_skill

    skill = load_skill("rollback-skill", workspace)
    assert "original content body" in skill.content

    retired = store.retire("rollback-skill", workspace)
    assert retired.exists()
    assert "rollback-skill" in retired.name
    with pytest.raises(SkillError):
        load_skill("rollback-skill", workspace)
