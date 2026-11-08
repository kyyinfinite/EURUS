from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.agent.loop import Agent, IterationBudget
from eurus.gateway.manager import RETRY_BACKOFF_SECONDS, BackgroundTaskManager
from eurus.gateway.tasks import DurableTask, TaskStore
from eurus.providers.testing import openai_message, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus, RuntimeEvent
from eurus.runtime.recovery import FailureKind, classify_failure_kind, is_retryable_failure
from eurus.runtime.resume import RecoveryManager
from eurus.runtime.state_machine import transition_run
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import ToolContext, ToolRegistry


async def noop_notify(chat_id, text):
    return None


def make_manager(store: TaskStore, runner, **overrides) -> BackgroundTaskManager:
    options = {
        "max_concurrent": 2,
        "shutdown_timeout": 2.0,
    }
    options.update(overrides)
    return BackgroundTaskManager(store, runner, noop_notify, **options)


def test_failure_classifier_separates_categories():
    assert (
        classify_failure_kind(error="Connection reset by peer while streaming")
        is FailureKind.TRANSIENT
    )
    assert classify_failure_kind(error="429 rate limit exceeded") is FailureKind.TRANSIENT
    assert classify_failure_kind(error="invalid api key provided") is FailureKind.PERMANENT
    assert classify_failure_kind(stop_reason="cancelled") is FailureKind.INTERRUPTED
    assert classify_failure_kind(stop_reason="interrupted") is FailureKind.INTERRUPTED
    assert classify_failure_kind(error="the user denied this action") is FailureKind.POLICY
    assert classify_failure_kind(error="tool crashed: ValueError") is FailureKind.PERMANENT
    assert is_retryable_failure(FailureKind.TRANSIENT) is True
    assert is_retryable_failure(FailureKind.PERMANENT) is False
    assert is_retryable_failure(FailureKind.POLICY) is False
    assert is_retryable_failure(FailureKind.INTERRUPTED) is False


def test_task_store_persists_budget_and_run_linkage(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    deadline = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    task = store.create(
        "owner",
        "session",
        "do the thing",
        {"notify_chat_id": "owner"},
        max_attempts=3,
        deadline_at=deadline,
    )
    assert task.max_attempts == 3
    assert task.deadline_at == deadline
    assert store.link_run(task.task_id, "run-123") is True
    loaded = store.get(task.task_id)
    assert loaded is not None
    assert loaded.run_id == "run-123"
    assert loaded.max_attempts == 3
    assert loaded.deadline_at == deadline
    assert loaded.attempts_used == 0


def test_task_store_migrates_old_database_additively(tmp_path: Path):
    path = tmp_path / "tasks.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE gateway_tasks (
            task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL, objective TEXT NOT NULL,
            status TEXT NOT NULL, state_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        "INSERT INTO gateway_tasks VALUES ('legacy', 'owner', 's', 'objective', "
        "'completed', '{}', '2024-01-01T00:00:00', '2024-01-01T00:00:00', '')"
    )
    connection.commit()
    connection.close()
    store = TaskStore(path)
    task = store.get("legacy")
    assert task is not None
    assert task.status == "completed"
    assert task.run_id == ""
    assert task.max_attempts == 1
    assert task.deadline_at is None


@pytest.mark.asyncio
async def test_transient_failure_retries_within_attempt_cap(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    attempts: list[str] = []

    async def flaky_runner(task: DurableTask) -> str:
        attempts.append(task.task_id)
        if len(attempts) < 2:
            raise ConnectionError("connection reset while calling provider")
        return "recovered result"

    manager = make_manager(store, flaky_runner)
    task = await manager.submit(
        "owner",
        "session",
        "retry me",
        max_attempts=2,
    )
    await asyncio.sleep(RETRY_BACKOFF_SECONDS + 1.5)
    await manager.shutdown()
    final = store.get(task.task_id)
    assert final is not None
    assert final.status == "completed"
    assert len(attempts) == 2
    assert final.state["result"] == "recovered result"


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    calls: list[str] = []

    async def broken_runner(task: DurableTask) -> str:
        calls.append(task.task_id)
        raise ValueError("invalid api key")

    manager = make_manager(store, broken_runner)
    task = await manager.submit(
        "owner",
        "session",
        "fail permanently",
        max_attempts=3,
    )
    await asyncio.sleep(0.5)
    await manager.shutdown()
    final = store.get(task.task_id)
    assert final is not None
    assert final.status == "failed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resume_pending_redispatches_queued_tasks_after_restart(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    executed: list[str] = []

    async def runner(task: DurableTask) -> str:
        executed.append(task.task_id)
        return "done"

    manager = make_manager(store, runner)
    await manager.shutdown()
    store.create("owner", "session-a", "task a", {})
    store.create("owner", "session-b", "task b", {})
    restarted = make_manager(store, runner)
    dispatched = await restarted.resume_pending()
    for _ in range(50):
        if len(executed) >= 2:
            break
        await asyncio.sleep(0.05)
    await restarted.shutdown()
    assert dispatched == 2
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_pause_and_resume_user_control(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    release = asyncio.Event()

    async def slow_runner(task: DurableTask) -> str:
        await release.wait()
        return "finished"

    manager = make_manager(store, slow_runner)
    task = await manager.submit("owner", "session", "long task")
    await asyncio.sleep(0.1)
    assert await manager.pause(task.task_id, "owner") is True
    paused = store.get(task.task_id)
    assert paused is not None
    assert paused.status == "interrupted"
    release.set()
    await asyncio.sleep(0.1)
    resumed = await manager.resume(task.task_id, "owner", acknowledge_side_effects=True)
    assert resumed is not None
    release.set()
    await asyncio.sleep(0.3)
    await manager.shutdown()
    final = store.get(task.task_id)
    assert final is not None
    assert final.status == "completed"


@pytest.mark.asyncio
async def test_task_deadline_limits_execution_time(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.sqlite3")

    async def sleepy_runner(task: DurableTask) -> str:
        await asyncio.sleep(10)
        return "never"

    manager = make_manager(store, sleepy_runner, task_timeout=5.0)
    deadline = (datetime.now(UTC) + timedelta(seconds=0.3)).isoformat()
    task = await manager.submit("owner", "session", "deadline task", deadline_at=deadline)
    await asyncio.sleep(0.8)
    await manager.shutdown()
    final = store.get(task.task_id)
    assert final is not None
    assert final.status == "failed"


@pytest.mark.asyncio
async def test_interrupted_run_resume_injects_prior_progress(tmp_path: Path):
    router, _ = scripted_router(json_responses=[openai_message("resumed answer")])
    run_store = RunStore(tmp_path / "runs.sqlite3")
    event_store = EventStore(run_store.database)
    attempt_store = ToolAttemptStore(run_store.database)
    run = run_store.create(Run(owner_id="owner", objective="complete the report"))
    run_store.save_checkpoint(
        run.run_id,
        {"objective": "complete the report", "iteration": 3, "side_effect_status": "settled"},
        checkpoint_type="runtime",
    )
    stored = run_store.get(run.run_id)
    transition_run(stored, RunStatus.RUNNING)
    run_store.save(stored, expected_status_version=stored.status_version - 1)
    event_store.append(
        RuntimeEvent(
            run_id=run.run_id,
            event_type="tool.call.finished",
            sequence_no=1,
            payload={"tool": "read_file", "is_error": False},
        )
    )
    stored = run_store.get(run.run_id)
    transition_run(stored, RunStatus.INTERRUPTED)
    run_store.save(stored, expected_status_version=stored.status_version - 1)
    manager = RecoveryManager(run_store, attempt_store, event_store=event_store)
    plan = manager.inspect(run.run_id)
    assert plan.action.value == "resume_checkpoint"
    directive = manager.resumption_directive(run.run_id)
    assert directive is not None
    assert "iteration 3" in directive
    assert "read_file" in directive

    received: list[str] = []

    async def make_engine(resumed_run):
        agent = Agent(
            router,
            ToolRegistry([]),
            ToolContext(workspace=tmp_path),
            system_prompt="test",
            iteration_budget=IterationBudget(limit=10),
            stream=False,
        )
        original_run = agent.run

        async def recording_run(text):
            received.append(text)
            async for event in original_run(text):
                yield event

        agent.run = recording_run
        return AgentEngine(agent, run_store=run_store)

    result = await manager.resume(run.run_id, make_engine)
    assert result.run.status is RunStatus.COMPLETED
    objective_text = received[0]
    assert "[Resumption context from the durable runtime]" in objective_text
    assert "complete the report" in objective_text
    assert "read_file" in objective_text


@pytest.mark.asyncio
async def test_uncertain_side_effect_blocks_resume_and_repeat(tmp_path: Path):
    run_store = RunStore(tmp_path / "runs.sqlite3")
    attempt_store = ToolAttemptStore(run_store.database)
    run = run_store.create(Run(owner_id="owner", objective="deploy the service"))
    run_store.save_checkpoint(
        run.run_id,
        {"objective": "deploy the service", "iteration": 2, "side_effect_status": "settled"},
        checkpoint_type="runtime",
    )
    stored = run_store.get(run.run_id)
    transition_run(stored, RunStatus.RUNNING)
    run_store.save(stored, expected_status_version=stored.status_version - 1)
    from eurus.runtime.contracts import ToolExecutionOutcome, ToolExecutionStatus

    outcome = ToolExecutionOutcome(
        call_id="c1",
        tool_name="deploy",
        status=ToolExecutionStatus.TIMEOUT,
        output="error: timed out",
        is_error=True,
        uncertain=True,
        call_key="deploy:{}",
        run_id=run.run_id,
        risk_level="high",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        detail="uncertain deployment outcome",
    )
    attempt_store.record(outcome)
    stored = run_store.get(run.run_id)
    transition_run(stored, RunStatus.INTERRUPTED)
    run_store.save(stored, expected_status_version=stored.status_version - 1)
    manager = RecoveryManager(run_store, attempt_store)
    from eurus.runtime.resume import RecoveryBlocked

    with pytest.raises(RecoveryBlocked, match="reconciliation"):
        await manager.resume(run.run_id, lambda resumed_run: None)


@pytest.mark.asyncio
async def test_engine_resumption_note_reaches_agent_objective(tmp_path: Path):
    router, _ = scripted_router(json_responses=[openai_message("final")])
    run_store = RunStore(tmp_path / "runs.sqlite3")
    agent = Agent(
        router,
        ToolRegistry([]),
        ToolContext(workspace=tmp_path),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=10),
        stream=False,
    )
    seen: list[str] = []
    original_run = agent.run

    async def recording_run(text):
        seen.append(text)
        async for event in original_run(text):
            yield event

    agent.run = recording_run
    engine = AgentEngine(agent, run_store=run_store)
    result = await engine.run(
        "original objective",
        resumption_note="[Resumption context from the durable runtime]\nprior steps done",
    )
    assert result.run.status is RunStatus.COMPLETED
    assert "original objective" in seen[0]
    assert "prior steps done" in seen[0]
    stored = run_store.get(result.run.run_id)
    assert stored is not None
    assert stored.objective == "original objective"
