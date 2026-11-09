from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.agent.loop import Agent, IterationBudget
from eurus.memory.manager import MemoryManager
from eurus.models import ToolCall
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import Tool, ToolContext, ToolRegistry

OWNER = "owner-integration"


def _registry(tools: list[Tool]) -> ToolRegistry:
    return ToolRegistry(tools)


def _agent(
    router_streams: list[list[str]],
    tools: list[Tool],
    *,
    completion_verifier: Any = None,
    planner: Any = None,
) -> Agent:
    router, _ = scripted_router(streams=router_streams)
    agent = Agent(
        router,
        _registry(tools),
        ToolContext(workspace=Path(".")),
        system_prompt="test",
        iteration_budget=IterationBudget(limit=12),
        completion_verifier=completion_verifier,
        planner=planner,
    )
    return agent


async def test_agent_changes_strategy_after_tool_failure() -> None:
    executed: list[tuple[str, dict[str, Any]]] = []

    async def flaky(arguments: dict[str, Any], context: ToolContext) -> str:
        executed.append(("lookup", dict(arguments)))
        if arguments.get("source") == "cache":
            raise RuntimeError("cache backend refused the connection")
        return "primary source answered"

    async def fallback(arguments: dict[str, Any], context: ToolContext) -> str:
        executed.append(("fetch_origin", dict(arguments)))
        return "origin answered with the record"

    tools = [
        Tool("lookup", "Lookup a record", {"type": "object"}, flaky),
        Tool("fetch_origin", "Fetch from origin", {"type": "object"}, fallback),
    ]
    streams = [
        openai_stream(calls=[("c1", "lookup", {"source": "cache", "key": "user-1"})]),
        openai_stream(calls=[("c2", "fetch_origin", {"key": "user-1"})]),
        openai_stream("the record was fetched from the origin"),
    ]
    agent = _agent(streams, tools)
    events = [event async for event in agent.run("look up user-1")]
    finishes = [event for event in events if event.kind == "done"]
    assert finishes, "agent must terminate"
    calls = [name for name, _ in executed]
    assert "lookup" in calls and "fetch_origin" in calls
    assert executed[0] == ("lookup", {"source": "cache", "key": "user-1"})
    assert executed[1][0] == "fetch_origin", "agent must switch strategy after failure"
    assert executed[1][1] != executed[0][1], "the second attempt must not repeat the failed call"


async def test_agent_rejects_repeated_unsuccessful_calls() -> None:
    from eurus.tools.boundary import call_key

    attempts: list[dict[str, Any]] = []

    async def failing(arguments: dict[str, Any], context: ToolContext) -> str:
        attempts.append(dict(arguments))
        raise RuntimeError("the endpoint always refuses this exact request")

    tool = Tool("deploy", "Deploy", {"type": "object"}, failing)
    identical = ("c1", "deploy", {"target": "prod", "flag": True})
    streams = [openai_stream(calls=[identical]) for _ in range(4)]
    streams.append(openai_stream("the deployment endpoint keeps refusing; stopping"))
    agent = _agent(streams, [tool])
    events = [event async for event in agent.run("deploy to prod")]
    tool_errors = [
        event.tool_result
        for event in events
        if event.kind == "tool_result" and event.tool_result is not None
    ]
    tool_errors = [item for item in tool_errors if item.is_error]
    rejections = [item.output for item in tool_errors if "already failed" in item.output]
    assert rejections, "the boundary must reject the repeated identical failing call"
    key = call_key(ToolCall(id="c1", name="deploy", arguments={"target": "prod", "flag": True}))
    assert agent.failure_counts.get(key, (0, ""))[0] >= 2
    assert len(attempts) <= 2, "the tool itself must not execute after the failure budget"


async def test_agent_verifies_actual_task_completion() -> None:
    class EvidenceVerifier:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, objective: str, content: str) -> Any:
            self.calls += 1

            class Outcome:
                def __init__(self, passed: bool, applies: bool) -> None:
                    self.passed = passed
                    self.applies = applies
                    self.summary = "file evidence checked" if passed else "no file evidence"

            return Outcome(True, True) if "report.txt" in content else Outcome(False, True)

    created: list[str] = []

    async def create_file(arguments: dict[str, Any], context: ToolContext) -> str:
        created.append(str(arguments.get("path", "")))
        return f"created {arguments.get('path')}"

    tools = [Tool("create_file", "Create a file", {"type": "object"}, create_file)]
    streams = [
        openai_stream(calls=[("c1", "create_file", {"path": "report.txt", "body": "data"})]),
        openai_stream("report.txt was created"),
    ]
    verifier = EvidenceVerifier()
    agent = _agent(streams, tools, completion_verifier=verifier)
    events = [event async for event in agent.run("create report.txt with the results")]
    finishes = [event for event in events if event.kind == "done"]
    assert finishes[-1].stop_reason == "completed"
    assert created == ["report.txt"]
    assert verifier.calls >= 1, "completion must be verified, not trusted"


async def test_completion_claim_without_evidence_is_rejected() -> None:
    class StrictVerifier:
        async def __call__(self, objective: str, content: str) -> Any:
            class Outcome:
                passed = False
                applies = True
                summary = "the file was never created"

            return Outcome()

    async def read_file(arguments: dict[str, Any], context: ToolContext) -> str:
        return "no such file"

    tools = [Tool("read_file", "Read", {"type": "object"}, read_file)]
    streams = [openai_stream("I created report.txt successfully.") for _ in range(3)]
    agent = _agent(streams, tools, completion_verifier=StrictVerifier())
    events = [event async for event in agent.run("create report.txt with the results")]
    finishes = [event for event in events if event.kind == "done"]
    assert finishes[-1].stop_reason in {"verification_failed", "completed"}
    if finishes[-1].stop_reason == "completed":
        pytest.fail("an unevidenced completion claim must not be accepted")


@dataclass
class ExecutionWindow:
    name: str
    started: float
    finished: float


class TimingExecutor:
    def __init__(self, *, fail_roles: frozenset[str] = frozenset(), delay: float = 0.15) -> None:
        self.windows: list[ExecutionWindow] = []
        self.fail_roles = fail_roles
        self.delay = delay

    async def execute(self, job: Any, context: Any) -> str:
        import time

        role = str((job.payload or {}).get("role", "unknown"))
        started = time.monotonic()
        try:
            if role in self.fail_roles and not any(window.name == role for window in self.windows):
                raise RuntimeError(f"{role} executor failed on first attempt")
            await asyncio.sleep(self.delay)
            return f"{role} completed"
        finally:
            self.windows.append(ExecutionWindow(role, started, time.monotonic()))


def _mission_env(tmp_path: Path, *, workers: int = 4) -> tuple[Any, Any, Any, Any, TimingExecutor]:
    from eurus.agent.supervisor import MainSupervisor
    from eurus.automation.engine import AutomationEngine
    from eurus.automation.retry import RetryManager, RetryPolicy
    from eurus.multiagent.orchestrator import AgentOrchestrator
    from eurus.multiagent.registry import SpecialistRegistry
    from eurus.storage.database import Database

    database = Database(tmp_path / "mission.sqlite3")
    executor = TimingExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(
        database, executor, retry_manager=retry, poll_seconds=0.05, workers=workers
    )
    orchestrator = AgentOrchestrator(database, automation)
    registry = SpecialistRegistry(database)
    registry.register("frontend", allowed_tools=["create_file"])
    registry.register("backend", allowed_tools=["create_file"])
    registry.register("qa", allowed_tools=["read_file"])
    supervisor = MainSupervisor(database, orchestrator)
    return supervisor, orchestrator, automation, database, executor


async def test_independent_mission_tasks_execute_concurrently(tmp_path: Path) -> None:
    supervisor, _orchestrator, automation, _db, executor = _mission_env(tmp_path)
    mission = supervisor.create_mission("two independent jobs", owner_id=OWNER)
    supervisor.plan_mission(
        mission.mission_id,
        owner_id=OWNER,
        tasks=[
            {
                "task_key": "left",
                "title": "Left",
                "objective": "left job",
                "preferred_role": "frontend",
            },
            {
                "task_key": "right",
                "title": "Right",
                "objective": "right job",
                "preferred_role": "backend",
            },
        ],
    )
    supervisor.start_mission(mission.mission_id, owner_id=OWNER)
    dispatched = supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER)
    assert len(dispatched) == 2, "both independent tasks must dispatch together"

    async def drive() -> None:
        workers = asyncio.create_task(automation.run_workers())
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while len(executor.windows) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
        finally:
            await automation.stop_workers()
            workers.cancel()
            with pytest.raises(asyncio.CancelledError):
                await workers

    await asyncio.wait_for(drive(), timeout=10)
    windows = sorted(executor.windows, key=lambda item: item.started)
    assert len(windows) == 2
    overlap = windows[0].finished > windows[1].started
    assert overlap, (
        f"independent tasks must run concurrently (windows: {windows[0]} vs {windows[1]})"
    )


async def test_dependent_mission_tasks_execute_in_order(tmp_path: Path) -> None:
    from eurus.multiagent.supervision import MissionSupervision

    supervisor, _orchestrator, automation, db, executor = _mission_env(tmp_path)
    mission = supervisor.create_mission("ordered jobs", owner_id=OWNER)
    supervisor.plan_mission(
        mission.mission_id,
        owner_id=OWNER,
        tasks=[
            {
                "task_key": "first",
                "title": "First",
                "objective": "first job",
                "preferred_role": "frontend",
            },
            {
                "task_key": "second",
                "title": "Second",
                "objective": "second job",
                "preferred_role": "backend",
                "depends_on": ["first"],
            },
        ],
    )
    supervisor.start_mission(mission.mission_id, owner_id=OWNER)
    assert len(supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER)) == 1

    async def drive() -> None:
        workers = asyncio.create_task(automation.run_workers())
        try:
            supervision = MissionSupervision(db, _orchestrator, stale_after_seconds=5.0)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 8.0
            while loop.time() < deadline:
                supervision.tick(mission.mission_id, owner_id=OWNER)
                supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER)
                frontend = [w for w in executor.windows if w.name == "frontend"]
                backend = [w for w in executor.windows if w.name == "backend"]
                if frontend and backend:
                    return
                await asyncio.sleep(0.02)
            pytest.fail("the dependent task never executed")
        finally:
            await automation.stop_workers()
            workers.cancel()
            with pytest.raises(asyncio.CancelledError):
                await workers

    await asyncio.wait_for(drive(), timeout=15)
    frontend = next(window for window in executor.windows if window.name == "frontend")
    backend = next(window for window in executor.windows if window.name == "backend")
    assert backend.started >= frontend.finished - 0.05, (
        "the dependent task must not start before its prerequisite finishes"
    )


async def test_subagent_failure_triggers_recovery(tmp_path: Path) -> None:
    from eurus.multiagent.supervision import MissionSupervision

    supervisor, _orchestrator, automation, _db, executor = _mission_env(tmp_path)
    executor.fail_roles = frozenset({"backend"})
    mission = supervisor.create_mission("recovering job", owner_id=OWNER)
    supervisor.plan_mission(
        mission.mission_id,
        owner_id=OWNER,
        tasks=[
            {
                "task_key": "work",
                "title": "Work",
                "objective": "do the work",
                "preferred_role": "backend",
            }
        ],
    )
    supervisor.start_mission(mission.mission_id, owner_id=OWNER)
    assert len(supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER)) == 1

    async def drive() -> None:
        workers = asyncio.create_task(automation.run_workers())
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 6.0
            supervision = MissionSupervision(_db, _orchestrator, stale_after_seconds=5.0)
            while loop.time() < deadline:
                supervision.tick(mission.mission_id, owner_id=OWNER)
                successes = [
                    task
                    for task in supervisor.tasks.list_tasks(mission.mission_id, owner_id=OWNER)
                    if task.status in {"succeeded", "review_pending"}
                ]
                backend_windows = [
                    window for window in executor.windows if window.name == "backend"
                ]
                if backend_windows and successes:
                    return
                supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER)
                await asyncio.sleep(0.05)
            pytest.fail("the failed assignment never recovered")
        finally:
            await automation.stop_workers()
            workers.cancel()
            with pytest.raises(asyncio.CancelledError):
                await workers

    await asyncio.wait_for(drive(), timeout=15)
    attempts = [window for window in executor.windows if window.name == "backend"]
    assert len(attempts) >= 2, "the failing sub-agent must be retried by supervision"


async def test_memory_persists_across_restart_and_stays_owner_scoped(tmp_path: Path) -> None:
    database = tmp_path / "agent.sqlite3"
    alice = MemoryManager(database, owner_id="alice")
    alice.remember(
        "the release checklist requires a changelog entry",
        category="project_fact",
        scope="global",
    )
    del alice

    restarted = MemoryManager(database, owner_id="alice")
    results = restarted.recall("release checklist changelog")
    assert any("changelog" in str(row.get("content", "")) for row in results), (
        "memory written before the restart must remain retrievable"
    )

    other = MemoryManager(database, owner_id="mallory")
    foreign = other.recall("release checklist changelog")
    assert not any("changelog" in str(row.get("content", "")) for row in foreign), (
        "a different owner must never retrieve another owner's memories"
    )


async def test_untrusted_memory_never_enters_trusted_retrieval(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path / "agent.sqlite3", owner_id="alice")
    quarantined_id = manager.remember_untrusted(
        "the remote page claims the maintainer moved to a cave",
        provenance={"source": "https://untrusted.example", "run_id": "run-9"},
    )
    hits = manager.recall("maintainer moved cave")
    assert all(int(row["id"]) != quarantined_id for row in hits), (
        "quarantined content must never surface in trusted retrieval"
    )
    assert all(str(row.get("trust", "")) != "untrusted" for row in hits)

    with pytest.raises(PermissionError):
        manager.promote(quarantined_id, authority="agent")
    assert manager.promote(quarantined_id, authority="user") is True
    promoted = manager.recall("maintainer moved cave")
    assert any(int(row["id"]) == quarantined_id for row in promoted), (
        "after explicit user promotion the memory becomes retrievable"
    )


async def test_memory_consolidation_preserves_provenance(tmp_path: Path) -> None:
    database = tmp_path / "agent.sqlite3"
    manager = MemoryManager(database, owner_id="alice")
    first = manager.remember("deploys happen on tuesdays", category="project_fact", scope="global")
    second = manager.store.add_memory(
        "Deploys happen on Tuesdays.",
        memory_type="project_fact",
        category="project_fact",
        confidence=0.9,
        source="schedule_run",
        owner_id="alice",
        provenance={"source": "schedule_run", "recorded_at": "2026-11-01T00:00:00+00:00"},
    )
    result = manager.consolidate()
    assert result["merged"] == 1
    assert result["deleted"] == 1
    survivors = {first, second} - set(result["groups"][0]["merged_ids"])
    survivor_id = result["groups"][0]["survivor_id"]
    assert survivor_id in survivors
    survivor = manager.store.get_memory(survivor_id)
    assert survivor is not None
    provenance = json.loads(survivor["provenance"])
    merged_from = provenance.get("consolidated_from", [])
    assert len(merged_from) == 1, "the merged duplicate must be recorded in provenance"
    merged_sources = {str(entry["source"]) for entry in merged_from}
    assert merged_sources == {"explicit_memory_write"}
    assert "schedule_run" in provenance.get("consolidated_sources", [])
    assert "explicit_memory_write" in provenance.get("consolidated_sources", [])
    remaining = manager.store.list_memories(limit=100, owner_id="alice")
    contents = [str(row["content"]).casefold().strip(".!? ") for row in remaining]
    assert contents.count("deploys happen on tuesdays") == 1, "duplicates must be gone"


async def test_interrupted_job_recovers_after_lease_expiry(tmp_path: Path) -> None:
    from eurus.automation.worker import AutomationWorkerPool
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    database = Database(tmp_path / "jobs.sqlite3")
    store = JobStore(database)
    job = store.enqueue("owner", "long running objective", max_attempts=2)

    claimed = store.claim("w-crashed", lease_seconds=5, owner_id="owner")
    assert claimed is not None and claimed.job_id == job.job_id
    assert store.get(job.job_id) is not None and store.get(job.job_id).status == "running"

    expired = datetime.now(UTC) + timedelta(seconds=6)
    recovered_counts = store.recover_expired(now=expired, owner_id="owner")
    assert recovered_counts["requeued"] >= 1
    recovered = store.get(job.job_id)
    assert recovered is not None and recovered.status == "queued", (
        "the expired lease must return the job to the queue without duplicating it"
    )

    async def healthy_handler(job_arg: Any) -> str:
        return "recovered result"

    completed: list[tuple[str, str]] = []

    def on_finish(job_arg: Any, status: str, result: str, error: str | None) -> None:
        completed.append((status, result))

    healing_pool = AutomationWorkerPool(
        store,
        healthy_handler,
        on_finish=on_finish,
        poll_seconds=0.05,
        lease_seconds=5,
        recover_expired_on_poll=False,
    )
    worked = await asyncio.wait_for(
        healing_pool.run_once("w-heal", now=expired + timedelta(seconds=1)), timeout=10
    )
    assert worked is True
    assert completed == [("completed", "recovered result")]
    final = store.get(job.job_id)
    assert final is not None and final.status == "completed"


async def test_cancellation_prevents_uncontrolled_background_execution(tmp_path: Path) -> None:
    from eurus.automation.worker import AutomationWorkerPool
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    database = Database(tmp_path / "cancel.sqlite3")
    store = JobStore(database)
    job = store.enqueue("owner", "background objective")
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    side_effects: list[str] = []

    async def handler(job_arg: Any) -> str:
        handler_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        side_effects.append("late side effect")
        return "done"

    pool = AutomationWorkerPool(store, handler, poll_seconds=0.05, lease_seconds=5)
    worker_task = asyncio.create_task(pool.run_once("w1"))
    await asyncio.wait_for(handler_started.wait(), timeout=10)
    store.request_cancel(job.job_id, owner_id="owner")
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task
    await asyncio.wait_for(handler_cancelled.wait(), timeout=10)
    await asyncio.sleep(0.2)
    assert side_effects == [], "no side effect may execute after cancellation"
    cancelled = store.get(job.job_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    again = await asyncio.wait_for(pool.run_once("w2"), timeout=10)
    assert again is False, "a cancelled job must never be re-claimed"


async def test_notification_delivery_has_no_duplicate_side_effects(tmp_path: Path) -> None:
    from eurus.automation.deliveries import DeliveryDispatcher, DeliveryStore

    store = DeliveryStore(tmp_path / "deliveries.sqlite3")
    store.enqueue(job_id="job-1", attempt=1, channel="local", subject="hello", body="world")
    first_claim = store.claim_due("local")[0]
    assert first_claim.claim_token, "a claim must carry a fencing token"

    store.recover_stale_sending(older_than_seconds=0.0)
    second_claim = store.claim_due("local")[0]
    assert second_claim.delivery_id == first_claim.delivery_id
    assert second_claim.claim_token != first_claim.claim_token, "re-claim must rotate the token"

    late_mark = store.mark(
        first_claim.delivery_id, status="delivered", claim_token=first_claim.claim_token
    )
    assert late_mark is False, "the stale claimer's mark must be fenced out"
    state_after_fence = store.list_deliveries()[0]
    assert state_after_fence.status == "sending"

    sends: list[str] = []

    async def sender(target: str, subject: str, body: str) -> None:
        sends.append(subject)

    dispatcher = DeliveryDispatcher(store, "local", sender)
    await dispatcher._deliver(second_claim)
    assert sends == ["hello"], "the message must be delivered exactly once"
    final = store.list_deliveries()[0]
    assert final.status == "delivered"
    assert final.delivered_at is not None


async def test_all_gateways_share_one_durable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    from eurus import paths
    from eurus.cli.autonomy import build_stack
    from eurus.config import Config
    from eurus.gateway.web.app import build_context
    from eurus.storage.events import EventStore
    from eurus.storage.runs import RunStore

    database_path = paths.database_path()
    config = Config()
    stack = build_stack(config, workers=1, poll_seconds=0.05, enable_learning=False)
    try:
        assert stack.database.path == str(database_path)
        assert stack.automation.jobs.database.path == str(database_path)
        assert stack.orchestrator.store.database.path == str(database_path)
        assert stack.events.store.database.path == str(database_path)
    finally:
        stack.close()

    security = type("Security", (), {"url": "http://127.0.0.1:1", "token": "t"})()
    ctx = build_context(config, security=security)
    assert str(ctx.run_store.database.path) == str(database_path), (
        "the web gateway run store must be the same durable database"
    )
    assert str(ctx.event_store.database.path) == str(database_path)

    run_store = RunStore(database_path)
    event_store = EventStore(database_path)
    from eurus.runtime.models import Run, RuntimeEvent

    run = run_store.create(Run(owner_id="owner-runtime", objective="objective via any gateway"))
    seen = run_store.get(run.run_id)
    assert seen is not None, "runs must be durable across every gateway process"
    event_store.append(RuntimeEvent(run_id=run.run_id, event_type="run.started", sequence_no=1))
    events = event_store.list_for_run(run.run_id)
    assert events and events[0].event_type == "run.started", (
        "events persisted by the engine must be visible to every gateway tailer"
    )
