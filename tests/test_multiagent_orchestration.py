from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.multiagent.communication import MessageStore
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.storage.database import Database

EVENTS: list[tuple[str, str]] = []


class SpecialistExecutor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []
        self.behaviors: dict[str, str] = {}

    async def execute(self, job: Any, context: Any) -> str:
        payload = job.payload or {}
        role = str(payload.get("role", "unknown"))
        self.executed.append((role, job.job_id))
        behavior = self.behaviors.get(role)
        if behavior == "fail":
            raise TimeoutError(f"specialist {role} failed")
        return f"report from {role}"


def build(tmp_path: Path, *, behaviors: dict[str, str] | None = None):
    database = Database(tmp_path / "agents.sqlite3")
    executor = SpecialistExecutor()
    if behaviors:
        executor.behaviors = behaviors
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    orchestrator = AgentOrchestrator(
        database,
        automation,
        event_publisher=lambda event_type, subject_type, subject_id, payload: EVENTS.append(
            (event_type, subject_id)
        ),
    )
    return orchestrator, automation, executor, database


def seed_specialists(registry: SpecialistRegistry) -> None:
    registry.register(
        "researcher",
        description="Finds and summarizes source material",
        capabilities=["research", "summarize"],
        allowed_tools=["web_search", "web_fetch"],
        priority=4,
    )
    registry.register(
        "writer",
        description="Produces polished prose",
        capabilities=["writing"],
        allowed_tools=["read_file", "create_file"],
        priority=5,
    )
    registry.register(
        "critic",
        description="Reviews drafts for defects",
        capabilities=["review"],
        allowed_tools=["read_file"],
        priority=6,
    )


async def drain(automation: AutomationEngine, *, rounds: int = 6) -> None:
    for _ in range(rounds):
        worked = False
        for _ in range(10):
            if await automation.run_worker():
                worked = True
            else:
                break
        if not worked:
            return
        await asyncio.sleep(0.01)


async def test_specialist_registry_crud(tmp_path: Path) -> None:
    orchestrator, _, _, _ = build(tmp_path)
    registry = orchestrator.registry
    seed_specialists(registry)
    assert len(registry.list_specialists()) == 3
    researcher = registry.get("researcher")
    assert researcher is not None
    assert researcher.allowed_tools == ("web_search", "web_fetch")
    assert registry.select(capability="review")[0].role == "critic"
    updated = registry.update("researcher", max_iterations=40)
    assert updated is not None
    assert updated.max_iterations == 40
    assert registry.set_enabled("writer", False) is True
    assert [item.role for item in registry.list_specialists()] == ["researcher", "critic"]
    assert registry.remove("critic") is True
    assert registry.get("critic") is None
    with pytest.raises(ValueError):
        registry.register("invalid role!")
    with pytest.raises(ValueError):
        registry.register("hacker", allowed_tools=["not a tool"])


async def test_specialist_registry_persists(tmp_path: Path) -> None:
    orchestrator, _, _, database = build(tmp_path)
    seed_specialists(orchestrator.registry)
    database.close()
    reopened = Database(tmp_path / "agents.sqlite3")
    registry2 = SpecialistRegistry(reopened)
    assert {item.role for item in registry2.list_specialists()} == {
        "researcher",
        "writer",
        "critic",
    }


async def test_dispatch_runs_assignment_through_automation(tmp_path: Path) -> None:
    orchestrator, automation, executor, _ = build(tmp_path)
    seed_specialists(orchestrator.registry)
    assignment = orchestrator.dispatch(
        "Research agent runtime architectures",
        role="researcher",
        owner_id="owner",
        parent_run_id="run-1",
    )
    assert assignment.status == "dispatched"
    assert assignment.job_id is not None
    job = automation.jobs.get(assignment.job_id)
    assert job is not None
    assert job.payload["kind"] == "specialist_assignment"
    assert job.payload["role"] == "researcher"
    assert job.payload["allowed_tools"] == ["web_search", "web_fetch"]
    assert job.source == "specialist"
    await drain(automation)
    final = orchestrator.store.get(assignment.assignment_id)
    assert final is not None
    assert final.status == "succeeded"
    assert final.result == "report from researcher"
    messages = orchestrator.messages.list_for_assignment(assignment.assignment_id)
    assert any(message.message_type == "task_request" for message in messages)
    assert any(message.message_type == "result_delivery" for message in messages)
    assert any(event == "AGENT_COMPLETED" for event, _ in EVENTS)


async def test_specialist_failure_reported_and_reassignable(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path, behaviors={"critic": "fail"})
    seed_specialists(orchestrator.registry)
    assignment = orchestrator.dispatch(
        "Review the draft",
        role="critic",
        owner_id="owner",
    )
    await drain(automation)
    failed = orchestrator.store.get(assignment.assignment_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error is not None
    messages = orchestrator.messages.list_for_assignment(assignment.assignment_id)
    assert any(message.message_type == "failure_report" for message in messages)
    replacement = orchestrator.reassign(assignment.assignment_id, role="writer")
    assert replacement.assignment_id != assignment.assignment_id
    assert replacement.role == "writer"
    await drain(automation)
    final = orchestrator.store.get(replacement.assignment_id)
    assert final is not None
    assert final.status == "succeeded"
    original = orchestrator.store.get(assignment.assignment_id)
    assert original is not None
    assert original.status == "reassigned"


async def test_parallel_dispatch_and_aggregation(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    seed_specialists(orchestrator.registry)
    assignments = orchestrator.dispatch_parallel(
        [
            {"objective": "Research topic", "role": "researcher"},
            {"objective": "Write draft", "role": "writer"},
            {"objective": "Critique draft", "role": "critic"},
        ],
        owner_id="owner",
        parent_run_id="run-parallel",
    )
    assert len(assignments) == 3
    roles = {assignment.role for assignment in assignments}
    assert roles == {"researcher", "writer", "critic"}
    await drain(automation)
    report = orchestrator.aggregate("run-parallel")
    assert report.total == 3
    assert report.succeeded == 3
    assert report.failed == 0
    assert report.complete is True
    assert len(report.results) == 3
    assert any("researcher" in item["result"] for item in report.results)


async def test_aggregation_reports_pending_and_failures(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    seed_specialists(orchestrator.registry)
    orchestrator.dispatch(
        "Research", role="researcher", owner_id="owner", parent_run_id="run-mixed"
    )
    orchestrator.dispatch("Write", role="writer", owner_id="owner", parent_run_id="run-mixed")
    partial = orchestrator.aggregate("run-mixed")
    assert partial.total == 2
    assert partial.pending == 2
    assert partial.complete is False
    await drain(automation)
    done = orchestrator.aggregate("run-mixed")
    assert done.succeeded == 2
    assert done.complete is True


async def test_multi_agent_acceptance_scenario(tmp_path: Path) -> None:
    behaviors = {"critic": "fail"}
    orchestrator, automation, executor, _ = build(tmp_path, behaviors=behaviors)
    seed_specialists(orchestrator.registry)
    orchestrator.dispatch_parallel(
        [
            {"objective": "Gather sources on topic X", "role": "researcher"},
            {"objective": "Draft section one", "role": "writer"},
            {"objective": "Critique the draft", "role": "critic"},
        ],
        owner_id="owner",
        parent_run_id="run-acceptance",
    )
    await drain(automation)
    report = orchestrator.aggregate("run-acceptance")
    assert report.succeeded == 2
    assert report.failed == 1
    assert report.complete is False
    replacement = None
    for assignment in orchestrator.store.list_assignments(parent_run_id="run-acceptance"):
        if assignment.status == "failed":
            replacement = orchestrator.reassign(assignment.assignment_id, role="writer")
    assert replacement is not None
    await drain(automation)
    final = orchestrator.aggregate("run-acceptance")
    assert final.succeeded == 3
    assert final.complete is True
    executed_roles = [role for role, _ in executor.executed]
    assert executed_roles.count("writer") >= 2
    assert any(event == "AGENT_STARTED" for event, _ in EVENTS)
    assert any(event == "AGENT_COMPLETED" for event, _ in EVENTS)
    assert any(event == "AGENT_FAILED" for event, _ in EVENTS)


async def test_nested_delegation_depth_limits(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    orchestrator.registry.register("lead", description="Lead", delegation_allowed=True)
    orchestrator.registry.register("helper", description="Helper", delegation_allowed=True)
    deep = orchestrator.dispatch(
        "Nested task",
        role="helper",
        owner_id="owner",
        parent_run_id="run-depth",
        delegation_depth=2,
    )
    assert deep.delegation_depth == 2
    with pytest.raises(PermissionError):
        orchestrator.dispatch(
            "Too deep",
            role="helper",
            owner_id="owner",
            delegation_depth=orchestrator.max_delegation_depth + 1,
        )
    orchestrator.registry.register("sealed", description="No delegation")
    with pytest.raises(PermissionError):
        orchestrator.dispatch(
            "Nested",
            role="sealed",
            owner_id="owner",
            delegation_depth=1,
        )


async def test_specialist_capacity_enforced(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    orchestrator.registry.register("solo", description="One at a time", max_concurrent=1)
    first = orchestrator.dispatch("First task", role="solo", owner_id="owner")
    assert first.status == "dispatched"
    with pytest.raises(RuntimeError, match="capacity"):
        orchestrator.dispatch("Second task", role="solo", owner_id="owner")
    await drain(automation)
    third = orchestrator.dispatch("Third task", role="solo", owner_id="owner")
    await drain(automation)
    final = orchestrator.store.get(third.assignment_id)
    assert final is not None
    assert final.status == "succeeded"


async def test_unknown_and_disabled_roles_rejected(tmp_path: Path) -> None:
    orchestrator, _, _, _ = build(tmp_path)
    with pytest.raises(KeyError):
        orchestrator.dispatch("Task", role="ghost", owner_id="owner")
    orchestrator.registry.register("offline", description="Disabled")
    orchestrator.registry.set_enabled("offline", False)
    with pytest.raises(ValueError, match="disabled"):
        orchestrator.dispatch("Task", role="offline", owner_id="owner")


async def test_assignment_cancel_propagates_to_job(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    seed_specialists(orchestrator.registry)
    assignment = orchestrator.dispatch("Research", role="researcher", owner_id="owner")
    cancelled = orchestrator.cancel(assignment.assignment_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    job = automation.jobs.get(assignment.job_id or "")
    assert job is not None
    assert job.status == "cancelled"
    messages = orchestrator.messages.list_for_assignment(assignment.assignment_id)
    assert any(message.message_type == "cancellation_signal" for message in messages)


async def test_assignment_limit_per_parent(tmp_path: Path) -> None:
    orchestrator, automation, _, _ = build(tmp_path)
    orchestrator.registry.register("pool", description="Pool", max_concurrent=32)
    orchestrator.max_assignments_per_parent = 4
    for index in range(orchestrator.max_assignments_per_parent):
        orchestrator.dispatch(
            f"Task {index}", role="pool", owner_id="owner", parent_run_id="run-limit"
        )
    with pytest.raises(RuntimeError, match="assignment limit"):
        orchestrator.dispatch("Overflow", role="pool", owner_id="owner", parent_run_id="run-limit")


async def test_assignments_survive_restart(tmp_path: Path) -> None:
    orchestrator, automation, _, database = build(tmp_path)
    seed_specialists(orchestrator.registry)
    assignment = orchestrator.dispatch(
        "Durable assignment", role="researcher", owner_id="owner", parent_run_id="run-restart"
    )
    database.close()
    reopened = Database(tmp_path / "agents.sqlite3")
    executor2 = SpecialistExecutor()
    automation2 = AutomationEngine(
        reopened,
        executor2,
        retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
        poll_seconds=0.05,
    )
    orchestrator2 = AgentOrchestrator(reopened, automation2)
    restored = orchestrator2.store.get(assignment.assignment_id)
    assert restored is not None
    assert restored.status == "dispatched"
    assert restored.role == "researcher"
    await drain(automation2)
    final = orchestrator2.store.get(assignment.assignment_id)
    assert final is not None
    assert final.status == "succeeded"


async def test_message_types_validated(tmp_path: Path) -> None:
    orchestrator, _, _, _ = build(tmp_path)
    store = MessageStore(orchestrator.messages.database)
    with pytest.raises(ValueError):
        store.record("assignment", "sender", "gossip", "not a valid type")
    message = store.record("assignment", "sender", "progress_update", "halfway done")
    assert store.list_for_assignment("assignment") == [message]


async def test_assignment_heartbeats(tmp_path: Path) -> None:
    orchestrator, _, _, _ = build(tmp_path)
    seed_specialists(orchestrator.registry)
    assignment = orchestrator.dispatch("Heartbeat task", role="writer", owner_id="owner")
    beat = orchestrator.store.last_heartbeat(assignment.assignment_id)
    assert beat is not None
    orchestrator.store.heartbeat(assignment.assignment_id, "step 3 of 5")
    refreshed = orchestrator.store.last_heartbeat(assignment.assignment_id)
    assert refreshed is not None
    assert refreshed >= beat
    stale = orchestrator.store.stale_running(older_than_seconds=3600)
    assert assignment.assignment_id not in {item.assignment_id for item in stale}
