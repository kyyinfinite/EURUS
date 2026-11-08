from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eurus.autonomy_hooks import HookPublisher
from eurus.config import build_config
from eurus.events.store import DomainEventStore
from eurus.gateway.manager import BackgroundTaskManager
from eurus.learning.skills import ScheduledRunSkillHook, SkillDraftStore
from eurus.owner import OWNER_ID
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def test_hook_publisher_maps_lifecycle_to_event_bus(home: Path) -> None:
    database = Database(home / "data" / "eurus.sqlite3")
    publisher = HookPublisher(database, owner_id=OWNER_ID)
    publisher.startup(gateway="web", pid=1234)
    publisher.agent_start(run_id="run-1", gateway="web", session_id="s1")
    publisher.agent_step(run_id="run-1", gateway="web", step=1)
    publisher.agent_step(run_id="run-1", gateway="web", step=1)
    publisher.agent_end(run_id="run-1", gateway="web", status="completed")
    events = DomainEventStore(database)
    rows = events.list_events(limit=50)
    types = [row.event_type for row in rows]
    assert "gateway:startup" in types
    assert "agent:start" in types
    assert "agent:end" in types
    assert types.count("agent:step") == 1


def test_scheduled_run_skill_hook_writes_quarantine_draft(home: Path) -> None:
    store = SkillDraftStore(home / "skills" / "quarantine")

    class FakeLearnerResponse:
        def __init__(self, content: str) -> None:
            self.message = content

    hook = ScheduledRunSkillHook(
        build_config({"agent": {"model": ""}}), store=store, min_result_chars=10
    )

    async def fake_draft(objective: str, result: str):
        return store.draft(
            "run-abc12345",
            "Reusable run workflow",
            "Step one: inspect the objective. Step two: run checks. "
            "Step three: summarize the outcome with evidence.",
        )

    hook._draft = fake_draft
    jobs = JobStore(Database(home / "data" / "eurus.sqlite3"))
    job = jobs.enqueue(
        OWNER_ID,
        "summarize the queue",
        payload={"run_mode": "scheduled"},
        source="schedule",
        schedule_id="abc123456789",
        scheduled_at="2026-01-01T00:00:00+00:00",
    )
    asyncio.run(hook(job, "completed", "long result body", None))
    asyncio.run(_drain(hook))
    assert (home / "skills" / "quarantine" / "run-abc12345.SKILL.md").exists()


async def _drain(hook: ScheduledRunSkillHook) -> None:
    for _ in range(100):
        if not hook._pending:
            return
        await asyncio.sleep(0.02)


def test_scheduled_run_skill_hook_ignores_non_schedule_jobs(home: Path) -> None:
    store = SkillDraftStore(home / "skills" / "quarantine")
    hook = ScheduledRunSkillHook(build_config({"agent": {"model": ""}}), store=store)
    jobs = JobStore(Database(home / "data" / "eurus.sqlite3"))
    job = jobs.enqueue(OWNER_ID, "manual work", source="manual")
    asyncio.run(hook(job, "completed", "x" * 500, None))
    assert list(store.root.glob("*.SKILL.md")) == []


def test_promote_flow_moves_draft_into_workspace(home: Path, tmp_path: Path) -> None:
    store = SkillDraftStore(home / "skills" / "quarantine")
    store.draft(
        "tidy-report",
        "Produce a tidy report",
        "Collect inputs, verify them, write the report, cite sources, and summarize clearly.",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = store.promote("tidy-report", workspace)
    assert target.exists()
    assert "tidy-report" in target.read_text(encoding="utf-8")


class _FakeStore:
    def claim(self, task_id: str, **kwargs: object):
        from types import SimpleNamespace

        self.transition(task_id, "running")
        for row in self.rows:
            if row["task_id"] == task_id:
                return SimpleNamespace(
                    **row, state={}, error="", run_id="", max_attempts=1, deadline_at=None
                )
        raise ValueError("missing")

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def create(self, owner_id: str, session_id: str, objective: str, state, **kwargs: object):
        task_id = f"task-{len(self.rows) + 1:04d}"
        self.rows.append(
            {
                "task_id": task_id,
                "owner_id": owner_id,
                "session_id": session_id,
                "objective": objective,
                "status": "pending",
            }
        )
        from types import SimpleNamespace

        return SimpleNamespace(
            task_id=task_id,
            owner_id=owner_id,
            session_id=session_id,
            objective=objective,
            status="pending",
            state=dict(state),
            error="",
            run_id="",
            max_attempts=1,
            deadline_at=None,
        )

    def get(self, task_id: str, *, owner_id: str | None = None):
        from types import SimpleNamespace

        for row in self.rows:
            if row["task_id"] == task_id:
                return SimpleNamespace(**row, state={}, error="", run_id="", max_attempts=1)
        return None

    def list_owner(self, owner_id: str, limit: int = 20):
        from types import SimpleNamespace

        return [
            SimpleNamespace(**row, state={}, error="", run_id="", max_attempts=1)
            for row in self.rows
            if row["owner_id"] == owner_id
        ][:limit]

    def transition(self, task_id: str, status: str, *, owner_id: str | None = None):
        for row in self.rows:
            if row["task_id"] == task_id:
                row["status"] = status
                return None
        raise ValueError("missing")

    def current_session(self, user_id: str, chat_id: str, platform: str = "telegram"):
        return f"session-{user_id}"

    def interrupt(self, task_id: str, error: str):
        return self.transition(task_id, "interrupted")


async def _sleeper(task) -> str:
    await asyncio.sleep(5)
    return "never"


def test_manager_interrupt_running_cancels_only_running_session_tasks(home: Path) -> None:
    async def scenario() -> None:
        store = _FakeStore()
        manager = BackgroundTaskManager(store, _sleeper, lambda chat, text: asyncio.sleep(0))
        first = await manager.submit("owner-1", "session-1", "long work", notify_chat_id="1")
        for _ in range(50):
            await asyncio.sleep(0.01)
            current = store.get(first.task_id)
            if current is not None and current.status == "running":
                break
        second = await manager.submit(
            "owner-1", "session-1", "interrupting message", notify_chat_id="1"
        )
        interrupted = await manager.interrupt_running(
            "owner-1", "session-1", exclude={second.task_id}
        )
        assert interrupted == 1
        cancelled = store.get(first.task_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        await manager.shutdown()

    asyncio.run(scenario())
