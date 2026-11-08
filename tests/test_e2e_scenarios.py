from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.automation.supervision import RuntimeSupervisor
from eurus.events.bus import EventBus
from eurus.goals.engine import GoalEngine
from eurus.goals.states import GoalStatus, GoalTaskStatus
from eurus.learning.evaluation import EvaluationEngine, LearningPipeline
from eurus.learning.lessons import LessonStore
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.runtime.models import Run, RunStatus
from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.jobs import JobStore
from eurus.workflows.engine import WorkflowEngine
from eurus.workflows.states import NodeStatus, WorkflowStatus


class ScenarioExecutor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []
        self.behaviors: dict[str, str] = {}

    async def execute(self, job: Any, context: Any) -> str:
        payload = job.payload or {}
        kind = str(payload.get("kind", "manual"))
        self.executed.append((kind, job.job_id))
        behavior = self.behaviors.get(kind)
        if behavior == "fail":
            raise TimeoutError(f"{kind} failed")
        if kind == "goal_plan":
            goal_id = str(payload.get("goal_id"))
            self.goals.capture_plan(
                goal_id,
                {
                    "tasks": [
                        {"title": "gather sources", "success_criteria": "sources listed"},
                        {
                            "title": "compose digest",
                            "detail": "write the digest",
                            "depends_on": ["gather sources"],
                            "success_criteria": "digest written",
                        },
                    ]
                },
            )
            return "plan submitted"
        if kind == "goal_task":
            return f"completed {job.objective.split(':')[0]}"
        return "handled"


class Scenario:
    def __init__(self, tmp_path: Path, *, behaviors: dict[str, str] | None = None) -> None:
        self.database_path = tmp_path / "scenario.sqlite3"
        self.database = Database(self.database_path)
        self.executor = ScenarioExecutor()
        if behaviors:
            self.executor.behaviors = behaviors
        self.retry = RetryManager(
            default_policy=RetryPolicy(
                max_attempts=2,
                backoff_base_seconds=0.01,
                backoff_max_seconds=0.01,
                jitter_fraction=0.0,
            )
        )
        self.schedule_store = ScheduleStore(self.database_path)
        self.automation = AutomationEngine(
            self.database,
            self.executor,
            retry_manager=self.retry,
            poll_seconds=0.05,
            scheduler=self.schedule_store,
            schedule_runs=self.schedule_store,
        )
        self.events = EventBus(
            self.database, automation=self.automation, schedule_triggers=self.schedule_store
        )

        def publish(event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
            self.events.publish(
                event_type, subject_type=subject_type, subject_id=subject_id, payload=payload
            )

        self.goals = GoalEngine(self.database, self.automation, event_publisher=publish)
        self.workflows = WorkflowEngine(self.database, self.automation, event_publisher=publish)
        self.orchestrator = AgentOrchestrator(
            self.database, self.automation, event_publisher=publish
        )
        self.executor.goals = self.goals
        self.supervisor = RuntimeSupervisor(
            self.automation,
            goals=self.goals,
            workflows=self.workflows,
            events=self.events,
            assignments=self.orchestrator,
            stuck_after_seconds=3600,
            stale_assignment_seconds=3600,
        )

    def rebuild(self) -> Scenario:
        self.database.close()
        reopened = Scenario.__new__(Scenario)
        reopened.database_path = self.database_path
        reopened.database = Database(self.database_path)
        reopened.executor = ScenarioExecutor()
        reopened.executor.behaviors = dict(self.executor.behaviors)
        reopened.retry = RetryManager(
            default_policy=RetryPolicy(
                max_attempts=2,
                backoff_base_seconds=0.01,
                backoff_max_seconds=0.01,
                jitter_fraction=0.0,
            )
        )
        reopened.schedule_store = ScheduleStore(self.database_path)
        reopened.automation = AutomationEngine(
            reopened.database,
            reopened.executor,
            retry_manager=reopened.retry,
            poll_seconds=0.05,
            scheduler=reopened.schedule_store,
            schedule_runs=reopened.schedule_store,
        )
        reopened.events = EventBus(
            reopened.database,
            automation=reopened.automation,
            schedule_triggers=reopened.schedule_store,
        )

        def publish(event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
            reopened.events.publish(
                event_type, subject_type=subject_type, subject_id=subject_id, payload=payload
            )

        reopened.goals = GoalEngine(reopened.database, reopened.automation, event_publisher=publish)
        reopened.workflows = WorkflowEngine(
            reopened.database, reopened.automation, event_publisher=publish
        )
        reopened.orchestrator = AgentOrchestrator(
            reopened.database, reopened.automation, event_publisher=publish
        )
        reopened.executor.goals = reopened.goals
        reopened.supervisor = RuntimeSupervisor(
            reopened.automation,
            goals=reopened.goals,
            workflows=reopened.workflows,
            events=reopened.events,
            assignments=reopened.orchestrator,
            stuck_after_seconds=3600,
            stale_assignment_seconds=3600,
        )
        return reopened

    async def pump(self, *, rounds: int = 12) -> None:
        for _ in range(rounds):
            self.supervisor.supervise()
            worked = False
            for _ in range(10):
                if await self.automation.run_worker():
                    worked = True
                else:
                    break
            if not worked:
                await asyncio.sleep(0.02)


async def test_scenario_a_autonomous_goal(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    goal = scenario.goals.create_goal(
        "owner",
        "Publish a weekly market digest",
        success_criteria=["digest exists", "sources cited"],
    )
    await scenario.pump()
    final = scenario.goals.store.get(goal.goal_id)
    assert final is not None
    assert final.status == GoalStatus.COMPLETED
    assert final.progress == 1.0
    tasks = scenario.goals.store.list_tasks(goal.goal_id)
    assert {task.status for task in tasks} == {GoalTaskStatus.SUCCEEDED}
    kinds = [kind for kind, _ in scenario.executor.executed]
    assert kinds.count("goal_plan") >= 1
    assert kinds.count("goal_task") >= 2
    goal_events = [
        event
        for event in scenario.events.store.list_events(limit=50)
        if event.subject_id == goal.goal_id
    ]
    assert any(event.event_type == "GOAL_COMPLETED" for event in goal_events)


async def test_scenario_b_persistent_scheduling(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    backdated = datetime.now(UTC) - timedelta(minutes=10)
    scenario.schedule_store.create(
        "owner",
        "Nightly summary report",
        kind="cron",
        cron_expr="* * * * *",
        timezone="UTC",
        misfire_policy="coalesce",
        catch_up_limit=1,
        max_occurrences=3,
        now=backdated,
    )
    await scenario.pump(rounds=4)
    jobs = [job for job in scenario.automation.jobs.list_jobs(limit=50) if job.source == "schedule"]
    assert len(jobs) >= 1
    assert any("Nightly summary" in job.objective for job in jobs)
    occurrences = scenario.schedule_store.occurrences(
        scenario.schedule_store.list_owner("owner")[0].schedule_id, limit=10
    )
    assert len(occurrences) >= 1
    scenario.rebuild()
    await scenario.pump(rounds=4)
    histories = scenario.schedule_store.history(
        scenario.schedule_store.list_owner("owner")[0].schedule_id, limit=10
    )
    assert any(run.status in ("completed", "queued", "running") for run in histories)


async def test_scenario_c_long_running_automation_restart(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    job = scenario.automation.submit(
        "owner", "Index the document corpus", source="manual", max_attempts=3
    )
    claimed = scenario.automation.jobs.claim("worker-1", lease_seconds=5)
    assert claimed is not None
    await asyncio.sleep(5.1)
    scenario.rebuild()
    scenario.supervisor.supervise()
    stored = scenario.automation.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status in ("queued", "running", "completed")
    await scenario.pump()
    final = scenario.automation.jobs.get(job.job_id)
    assert final is not None
    assert final.status == "completed"
    assert final.result == "handled"


async def test_scenario_d_workflow_with_failure_recovery(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, behaviors={"workflow_node": "fail"})
    scenario.workflows.register_definition(
        "publish-flow",
        "Publish flow",
        [
            {"node_id": "fan", "node_type": "parallel"},
            {
                "node_id": "draft",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "Draft the article"},
            },
            {
                "node_id": "review",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "Review the draft"},
            },
            {
                "node_id": "join",
                "node_type": "join",
                "depends_on": ["draft", "review"],
            },
            {
                "node_id": "publish",
                "node_type": "agent_task",
                "depends_on": ["join"],
                "config": {"objective": "Publish the article"},
            },
        ],
    )
    instance = scenario.workflows.start("publish-flow", "owner")
    await scenario.pump()
    final = scenario.workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.FAILED
    nodes = {
        node.node_id: node for node in scenario.workflows.store.list_nodes(instance.instance_id)
    }
    assert nodes["publish"].status == NodeStatus.SKIPPED
    draft_attempts = [
        job_id for kind, job_id in scenario.executor.executed if kind == "workflow_node"
    ]
    assert len(draft_attempts) >= 1
    scenario.executor.behaviors.pop("workflow_node")
    await scenario.pump()
    completed_instance = scenario.workflows.start("publish-flow", "owner")
    await scenario.pump()
    done = scenario.workflows.store.get_instance(completed_instance.instance_id)
    assert done is not None
    assert done.status == WorkflowStatus.COMPLETED


async def test_scenario_e_multi_agent(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    registry = scenario.orchestrator.registry
    registry.register(
        "researcher", description="Research", allowed_tools=["web_search"], priority=4
    )
    registry.register("writer", description="Writing", allowed_tools=["create_file"], priority=5)
    registry.register("critic", description="Critique", allowed_tools=["read_file"], priority=6)
    scenario.executor.behaviors["specialist_assignment"] = "fail"
    first_critic = scenario.orchestrator.dispatch(
        "Critique the draft", role="critic", owner_id="owner", parent_run_id="synthesis-run"
    )
    await scenario.pump()
    failed = scenario.orchestrator.store.get(first_critic.assignment_id)
    assert failed is not None
    assert failed.status == "failed"
    scenario.executor.behaviors.pop("specialist_assignment")
    scenario.orchestrator.reassign(first_critic.assignment_id, role="writer")
    scenario.orchestrator.dispatch(
        "Research background", role="researcher", owner_id="owner", parent_run_id="synthesis-run"
    )
    await scenario.pump()
    report = scenario.orchestrator.aggregate("synthesis-run")
    assert report.succeeded >= 2
    assert report.complete is True
    synthesized = scenario.automation.submit(
        "owner",
        "Synthesize the specialist reports into the final answer",
        source="manual",
    )
    await scenario.automation.run_worker()
    final = scenario.automation.jobs.get(synthesized.job_id)
    assert final is not None
    assert final.status == "completed"


async def test_scenario_f_event_automation(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.events.subscribe(
        "incoming-webhook",
        event_types=["WEBHOOK_RECEIVED"],
        subject_type="webhook",
        handler_config={
            "objective": "Process webhook delivery {{payload.delivery}}",
            "owner_id": "ops",
        },
    )
    event = scenario.events.publish(
        "WEBHOOK_RECEIVED",
        subject_type="webhook",
        subject_id="hook-17",
        payload={"delivery": "del-991"},
    )
    assert event is not None
    await scenario.pump()
    jobs = [
        job
        for job in scenario.automation.jobs.list_jobs(limit=50)
        if (job.payload or {}).get("kind") == "event"
    ]
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert "del-991" in jobs[0].objective
    delivery = scenario.events.store.deliveries_for_event(event.event_id)[0]
    assert delivery.status == "delivered"


async def test_scenario_g_learning(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    lessons = LessonStore(tmp_path / "sessions.db")
    engine = EvaluationEngine(scenario.database)
    pipeline = LearningPipeline(engine, lessons=lessons)
    run = Run(run_id="run-learn-1", owner_id="owner", objective="Produce the digest")
    run.status = RunStatus.COMPLETED
    run.result_ref = "digest text"
    report = pipeline.process_run(
        run,
        attempts=[
            {"tool_name": "web_search", "status": "succeeded", "is_error": False},
            {"tool_name": "create_file", "status": "succeeded", "is_error": False},
        ],
        lesson_candidates=[
            "Run web searches before drafting so sources are ready",
        ],
    )
    assert all(evaluation.passed for evaluation in report.evaluations)
    accepted = report.lesson_reviews[0]
    assert accepted.accepted is True
    assert accepted.provenance_id is not None
    stored = lessons.relevant("draft the digest with sources")
    assert any("web searches" in lesson for lesson in stored)
    assert engine.reverse(accepted.provenance_id) is True
    assert engine.provenance_for("lesson", str(accepted.lesson_id)) == []


async def test_scenario_h_security_policy_blocks_unauthorized_tool(tmp_path: Path) -> None:
    from eurus.automation.executor import ScopedToolRegistry
    from eurus.automation.policies import ExecutionPolicy, PolicyEnforcer, PolicyViolation
    from eurus.tools.base import Tool
    from eurus.tools.builtin import default_registry

    scenario = Scenario(tmp_path)
    job = scenario.automation.submit(
        "owner",
        "Read the configuration and restart the service",
        source="schedule",
        payload={
            "deadline_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    worked = await scenario.automation.run_worker()
    assert worked is True
    stored = scenario.automation.jobs.get(job.job_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error == "PolicyViolation"

    registry = default_registry(include_capability_tools=False)

    async def shell_handler(arguments: dict[str, Any], context: Any) -> str:
        return "shell executed"

    registry.register(
        Tool(
            "shell",
            "Runs shell commands",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            shell_handler,
            mutates=True,
        )
    )
    assert "shell" in registry.names()
    scheduled_policy = ExecutionPolicy(
        allowed_tools=frozenset(),
        blocked_tools=frozenset({"shell"}),
        policy_scope="scheduled",
    )
    scoped = ScopedToolRegistry(
        registry, allowed=scheduled_policy.allowed_tools, blocked=scheduled_policy.blocked_tools
    )
    assert scoped.get("shell") is None
    assert "shell" not in scoped.names()
    assert scoped.get("calculate") is None
    assert scoped.names() == []

    specialist_policy = ExecutionPolicy(
        allowed_tools=frozenset({"calculate"}), policy_scope="specialist"
    )
    specialist_scope = ScopedToolRegistry(
        registry,
        allowed=specialist_policy.allowed_tools,
        blocked=specialist_policy.blocked_tools,
    )
    assert specialist_scope.get("shell") is None
    assert specialist_scope.get("calculate") is not None

    overlapping = ExecutionPolicy(
        allowed_tools=frozenset({"shell"}), blocked_tools=frozenset({"shell"})
    )
    with pytest.raises(ValueError):
        overlapping.validate()
    expired = ExecutionPolicy(
        deadline_at=datetime.now(UTC) - timedelta(minutes=1), policy_scope="scheduled"
    )
    with pytest.raises(PolicyViolation):
        PolicyEnforcer(policy=expired).verify_startable(job)


async def test_scenario_restarts_preserve_all_state(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    goal = scenario.goals.create_goal("owner", "Durable objective")
    scenario.workflows.register_definition(
        "durable-flow",
        "Durable flow",
        [{"node_id": "only", "node_type": "agent_task", "config": {"objective": "Persist"}}],
    )
    instance = scenario.workflows.start("durable-flow", "owner")
    scenario.orchestrator.registry.register("clerk", description="Clerk")
    assignment = scenario.orchestrator.dispatch(
        "File records", role="clerk", owner_id="owner", parent_run_id="durable-parent"
    )
    scenario.supervisor.supervise()
    scenario.rebuild()
    assert scenario.goals.store.get(goal.goal_id) is not None
    assert scenario.workflows.store.get_instance(instance.instance_id) is not None
    restored = scenario.orchestrator.store.get(assignment.assignment_id)
    assert restored is not None
    await scenario.pump()
    final_goal = scenario.goals.store.get(goal.goal_id)
    assert final_goal is not None
    assert final_goal.status == GoalStatus.COMPLETED
    final_workflow = scenario.workflows.store.get_instance(instance.instance_id)
    assert final_workflow is not None
    assert final_workflow.status == WorkflowStatus.COMPLETED
    final_assignment = scenario.orchestrator.store.get(assignment.assignment_id)
    assert final_assignment is not None
    assert final_assignment.status == "succeeded"


def test_durable_job_store_integration(tmp_path: Path) -> None:
    store = JobStore(Database(tmp_path / "jobs-only.sqlite3"))
    job = store.enqueue("owner", "Direct enqueue", max_attempts=3, source="manual")
    claimed = store.claim("w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.job_id == job.job_id
    duplicate_claim = store.claim("w2", lease_seconds=60)
    assert duplicate_claim is None or duplicate_claim.job_id != job.job_id
    assert (
        store.finish(claimed.job_id, "w1", claimed.lease_version, status="completed", result="done")
        is True
    )
    assert store.get(job.job_id).status == "completed"
