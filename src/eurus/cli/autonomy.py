from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main
from eurus.config import Config
from eurus.owner import OWNER_ID

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AutonomyStack:
    database: Any
    automation: Any
    schedule_store: Any
    goals: Any
    workflows: Any
    orchestrator: Any
    events: Any
    supervisor: Any
    trajectory: Any = None
    learning: Any = None

    def close(self) -> None:
        if self.supervisor is not None and hasattr(self.supervisor, "close"):
            self.supervisor.close()
        if self.database is not None and hasattr(self.database, "close"):
            self.database.close()


def _publish_to_bus(bus: Any) -> Any:
    def publish(
        event_type: str, subject_type: str, subject_id: str, payload: Mapping[str, Any]
    ) -> None:
        bus.publish(
            event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
        )

    return publish


class _OwnerScopedPublisher:
    _SUBJECT_TABLES = {
        "job": ("eurus_jobs", "job_id"),
        "goal": ("eurus_goals", "goal_id"),
        "workflow_instance": ("eurus_workflow_instances", "instance_id"),
        "assignment": ("eurus_agent_assignments", "assignment_id"),
    }

    def __init__(self, bus: Any, database: Any) -> None:
        self.bus = bus
        self.database = database

    def __call__(
        self, event_type: str, subject_type: str, subject_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.bus.publish(
            event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            owner_id=self._resolve_owner(subject_type, subject_id),
        )

    def _resolve_owner(self, subject_type: str, subject_id: str) -> str:
        entry = self._SUBJECT_TABLES.get(subject_type)
        if entry is None or not subject_id:
            return ""
        table, column = entry
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    f"SELECT owner_id FROM {table} WHERE {column}=?", (subject_id,)
                ).fetchone()
        except sqlite3.Error:
            return ""
        if row is None:
            return ""
        return str(row["owner_id"] or "")


class _SideEffectMarker:
    def __init__(self) -> None:
        self.jobs: Any = None

    def bind(self, jobs: Any) -> None:
        self.jobs = jobs

    def factory(self, job: Any) -> Callable[[], None] | None:
        jobs = self.jobs
        if jobs is None:
            raise RuntimeError("side-effect marker used before runtime wiring completed")

        def mark() -> None:
            worker_id = str(getattr(job, "worker_id", "") or "")
            if not worker_id:
                raise RuntimeError(
                    f"job {job.job_id} has no worker identity for the side-effect boundary"
                )
            if not jobs.mark_side_effect_started(job.job_id, worker_id, job.lease_version):
                raise RuntimeError(
                    f"lease lost for job {job.job_id} before the side-effect boundary"
                )

        return mark


class _ScheduleTriggerAdapter:
    def __init__(self, store: Any) -> None:
        self.store = store

    def trigger_event_schedules(self, event: Any, *, owner_id: str | None = None) -> list[str]:
        return list(
            self.store.trigger_event_schedules(
                str(getattr(event, "event_type", "")),
                subject_id=str(getattr(event, "subject_id", "")),
                owner_id=owner_id,
            )
        )


class _OrchestratorLookup:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def bind(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def specialist_for_role(self, role: str) -> Any | None:
        if self.orchestrator is None:
            return None
        return self.orchestrator.registry.get(role)


def build_stack(
    config: Config,
    *,
    workers: int = 2,
    poll_seconds: float = 15.0,
    owner_id: str = OWNER_ID,
    enable_goals: bool = True,
    enable_workflows: bool = True,
    enable_events: bool = True,
    enable_specialists: bool = True,
    enable_learning: bool = True,
    notification_sink: Any = None,
) -> AutonomyStack:
    from eurus.automation.engine import AutomationEngine
    from eurus.automation.executor import AgentJobExecutor
    from eurus.automation.notifications import LoggingNotificationSink
    from eurus.automation.supervision import RuntimeSupervisor
    from eurus.events.bus import EventBus
    from eurus.goals.engine import GoalEngine
    from eurus.goals.tools import goal_tool_provider
    from eurus.learning.integration import LearningCoordinator
    from eurus.learning.lessons import LessonStore
    from eurus.multiagent.orchestrator import AgentOrchestrator
    from eurus.scheduler import ScheduleStore
    from eurus.storage.database import Database
    from eurus.trajectory import TrajectoryStore
    from eurus.workflows.engine import WorkflowEngine

    database_path = paths.database_path()
    database = Database(database_path)
    schedule_store = ScheduleStore(database_path)
    trajectory = TrajectoryStore(database_path.parent / "trajectory.sqlite3")
    marker = _SideEffectMarker()
    executor = AgentJobExecutor(
        config,
        database=database,
        trajectory_store=trajectory,
        side_effect_marker_factory=marker.factory,
    )
    sink = notification_sink if notification_sink is not None else LoggingNotificationSink()
    automation = AutomationEngine(
        database,
        executor,
        scheduler=schedule_store,
        schedule_runs=schedule_store,
        occurrence_finalizer=schedule_store.finalize_occurrence,
        event_publisher=None,
        notifications=sink,
        workers=workers,
        poll_seconds=max(0.05, min(poll_seconds, 300.0)),
        owner_id=owner_id,
    )
    marker.bind(automation.jobs)
    events = EventBus(
        database,
        automation=automation,
        schedule_triggers=_ScheduleTriggerAdapter(schedule_store),
        notifications=sink,
    )
    publish = _OwnerScopedPublisher(events, database) if enable_events else None
    automation.event_publisher = publish
    specialist_lookup = _OrchestratorLookup(None)
    goals = GoalEngine(database, automation, event_publisher=publish) if enable_goals else None
    if goals is not None:
        executor.tool_provider = goal_tool_provider(goals)
    workflows = (
        WorkflowEngine(
            database,
            automation,
            event_publisher=publish,
            specialist_lookup=specialist_lookup,
        )
        if enable_workflows
        else None
    )
    orchestrator = (
        AgentOrchestrator(database, automation, event_publisher=publish)
        if enable_specialists
        else None
    )
    specialist_lookup.bind(orchestrator)
    learning: LearningCoordinator | None = None
    if enable_learning:
        lessons = LessonStore(
            database_path,
            limit=config.learning.lessons_in_prompt,
            max_lessons=config.learning.max_lessons,
        )
        learning = LearningCoordinator(database, lessons=lessons)
        automation.on_job_finished.append(learning.on_job_finished)
        from eurus.learning.effectiveness import SkillOutcomeHook, SkillOutcomeStore

        automation.on_job_finished.append(SkillOutcomeHook(SkillOutcomeStore(database_path)))
        if config.learning.skills:
            from eurus.learning.skills import ScheduledRunSkillHook

            automation.on_job_finished.append(ScheduledRunSkillHook(config))
    from eurus.storage.runs import RunStore

    supervisor = RuntimeSupervisor(
        automation,
        goals=goals,
        workflows=workflows,
        events=events if enable_events else None,
        assignments=orchestrator,
        learning=learning,
        runs=RunStore(database),
        trajectory=trajectory,
        owner_id=owner_id,
    )
    return AutonomyStack(
        database=database,
        automation=automation,
        schedule_store=schedule_store,
        goals=goals,
        workflows=workflows,
        orchestrator=orchestrator,
        events=events,
        supervisor=supervisor,
        trajectory=trajectory,
        learning=learning,
    )


@main.group("autonomy")
def autonomy_group() -> None:
    pass


async def run_daemon(
    config: Config,
    *,
    poll_seconds: float,
    workers: int,
    owner: str,
    enable_goals: bool = True,
    enable_workflows: bool = True,
    enable_events: bool = True,
) -> None:
    stack = build_stack(
        config,
        workers=workers,
        poll_seconds=max(0.05, min(poll_seconds, 300.0)),
        owner_id=owner,
        enable_goals=enable_goals,
        enable_workflows=enable_workflows,
        enable_events=enable_events,
    )
    automation = stack.automation
    supervisor = stack.supervisor
    stopping = asyncio.Event()
    interval = max(0.05, min(poll_seconds, 300.0))

    async def supervision_loop() -> None:
        consecutive_failures = 0
        while not stopping.is_set():
            wait_seconds = interval
            try:
                report = supervisor.supervise()
            except Exception:
                consecutive_failures += 1
                wait_seconds = min(2.0 * (2 ** (consecutive_failures - 1)), 300.0)
                log.exception(
                    "supervision tick failed (consecutive=%s, retrying in %.1fs)",
                    consecutive_failures,
                    wait_seconds,
                )
            else:
                if consecutive_failures:
                    log.info(
                        "supervision recovered after %s consecutive failures",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                for name, error in report.subsystem_errors.items():
                    log.warning("subsystem %s reported an error: %s", name, error)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=wait_seconds)

    workers_task = asyncio.create_task(automation.run_workers())
    supervisor_task = asyncio.create_task(supervision_loop())
    try:
        await asyncio.gather(workers_task, supervisor_task)
    finally:
        stopping.set()
        await automation.stop_workers()
        for task in (workers_task, supervisor_task):
            task.cancel()
        await asyncio.gather(workers_task, supervisor_task, return_exceptions=True)
        supervisor.close()
        stack.database.close()


@autonomy_group.command("daemon")
@click.option("--poll-seconds", type=click.IntRange(1, 300), default=15, show_default=True)
@click.option("--workers", type=click.IntRange(1, 16), default=2, show_default=True)
@click.option("--owner", default=OWNER_ID, show_default=True)
@click.option("--no-goals", is_flag=True, default=False, help="Disable the goal engine.")
@click.option("--no-workflows", is_flag=True, default=False, help="Disable workflows.")
@click.option("--no-events", is_flag=True, default=False, help="Disable the event bus.")
@click.pass_context
def autonomy_daemon(
    ctx: click.Context,
    poll_seconds: int,
    workers: int,
    owner: str,
    no_goals: bool,
    no_workflows: bool,
    no_events: bool,
) -> None:
    config = _state(ctx).config
    console = Console()
    console.print(
        f"Autonomy runtime running for owner {owner} with {workers} workers. Press Ctrl+C to stop."
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            run_daemon(
                config,
                poll_seconds=float(poll_seconds),
                workers=workers,
                owner=owner,
                enable_goals=not no_goals,
                enable_workflows=not no_workflows,
                enable_events=not no_events,
            )
        )


@autonomy_group.command("health")
@click.option("--owner", default=None)
@click.pass_context
def autonomy_health(ctx: click.Context, owner: str | None) -> None:
    from eurus.automation.engine import AutomationEngine
    from eurus.automation.supervision import RuntimeSupervisor
    from eurus.events.bus import EventBus
    from eurus.storage.database import Database

    _ = _state(ctx).config
    database = Database(paths.database_path())
    automation = AutomationEngine(database, _NoopExecutor(), owner_id=owner)
    events = EventBus(database)
    from eurus.storage.runs import RunStore

    supervisor = RuntimeSupervisor(
        automation, events=events, runs=RunStore(database), owner_id=owner
    )
    health = supervisor.health()
    database.close()
    console = Console()
    table = Table(title="Autonomy runtime health")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key, value in health.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                table.add_row(f"{key}.{inner_key}", str(inner_value))
        else:
            table.add_row(key, str(value))
    console.print(table)


@autonomy_group.group("goals")
def autonomy_goals() -> None:
    pass


@autonomy_goals.command("create")
@click.argument("objective")
@click.option("--owner", default=OWNER_ID, show_default=True)
@click.option("--description", default="")
@click.option("--criteria", default="", help="Semicolon-separated success criteria.")
@click.option("--deadline", default=None, help="ISO-8601 deadline.")
@click.option("--priority", type=click.IntRange(1, 9), default=5, show_default=True)
def goals_create(
    objective: str,
    owner: str,
    description: str,
    criteria: str,
    deadline: str | None,
    priority: int,
) -> None:
    from datetime import UTC, datetime

    from eurus.goals.engine import GoalEngine
    from eurus.storage.database import Database

    database = Database(paths.database_path())
    automation_placeholder = _offline_automation(database)
    goals = GoalEngine(database, automation_placeholder)
    deadline_at = None
    if deadline:
        parsed = datetime.fromisoformat(deadline)
        deadline_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    goal = goals.create_goal(
        owner,
        objective,
        description=description,
        success_criteria=[item.strip() for item in criteria.split(";") if item.strip()],
        deadline_at=deadline_at,
        priority=priority,
    )
    database.close()
    Console().print(f"created goal {goal.goal_id} [{goal.status.value}]")


@autonomy_goals.command("list")
@click.option("--owner", default=None)
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def goals_list(owner: str | None, limit: int) -> None:
    from eurus.goals.store import GoalStore
    from eurus.storage.database import Database

    store = GoalStore(Database(paths.database_path()))
    goals = store.list_goals(owner_id=owner, limit=limit)
    table = Table(title="Goals")
    table.add_column("goal_id")
    table.add_column("status")
    table.add_column("progress", justify="right")
    table.add_column("objective")
    for goal in goals:
        table.add_row(
            goal.goal_id[:12],
            goal.status.value,
            f"{goal.progress:.0%}",
            goal.objective[:60],
        )
    Console().print(table)


@autonomy_goals.command("show")
@click.argument("goal_id")
@click.option("--owner", default=None, help="Restrict the lookup to this owner.")
def goals_show(goal_id: str, owner: str | None) -> None:
    from eurus.goals.store import GoalStore
    from eurus.storage.database import Database

    store = GoalStore(Database(paths.database_path()))
    goal = store.get(goal_id, owner_id=owner)
    if goal is None:
        raise click.ClickException("goal not found")
    console = Console()
    console.print(f"[bold]{goal.objective}[/bold]")
    console.print(f"status={goal.status.value} progress={goal.progress:.0%} owner={goal.owner_id}")
    console.print(f"created={goal.created_at.isoformat()} deadline={goal.deadline_at or 'none'}")
    for task in store.list_tasks(goal.goal_id):
        console.print(f"  task {task.task_id[:8]} [{task.status.value}] {task.title[:70]}")
    for milestone in store.list_milestones(goal.goal_id):
        console.print(
            f"  milestone [{milestone.status.value}] {milestone.title} "
            f"(target {milestone.target_progress:.0%})"
        )
    for evidence in goal.completion_evidence[-10:]:
        console.print(f"  evidence: {evidence[:120]}")


def _goal_control(action: str) -> Callable[[str, str], None]:
    from eurus.goals.engine import GoalEngine
    from eurus.storage.database import Database

    def control(goal_id: str, owner: str) -> None:
        database = Database(paths.database_path())
        goals = GoalEngine(database, _offline_automation(database))
        result = getattr(goals, action)(goal_id, owner)
        database.close()
        if result is None:
            raise click.ClickException("goal not found or not allowed")
        Console().print(f"goal {result.goal_id} -> {result.status.value}")

    return control


@autonomy_goals.command("pause")
@click.argument("goal_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def goals_pause(goal_id: str, owner: str) -> None:
    _goal_control("pause")(goal_id, owner)


@autonomy_goals.command("resume")
@click.argument("goal_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def goals_resume(goal_id: str, owner: str) -> None:
    _goal_control("resume")(goal_id, owner)


@autonomy_goals.command("cancel")
@click.argument("goal_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def goals_cancel(goal_id: str, owner: str) -> None:
    _goal_control("cancel")(goal_id, owner)


@autonomy_group.group("workflows")
def autonomy_workflows() -> None:
    pass


@autonomy_workflows.command("define")
@click.argument("definition_file", type=click.Path(path_type=Path, exists=True))
@click.pass_context
def workflows_define(ctx: click.Context, definition_file: Path) -> None:
    from eurus.storage.database import Database
    from eurus.workflows.engine import WorkflowEngine

    document = json.loads(definition_file.read_text(encoding="utf-8"))
    database = Database(paths.database_path())
    workflows = WorkflowEngine(database, _offline_automation(database))
    try:
        definition = workflows.register_definition_document(document)
    except ValueError as exc:
        database.close()
        raise click.ClickException(str(exc)) from exc
    database.close()
    Console().print(
        f"registered workflow {definition.workflow_id} version {definition.version} "
        f"with {len(definition.nodes)} nodes"
    )


@autonomy_workflows.command("list")
def workflows_list() -> None:
    from eurus.storage.database import Database
    from eurus.workflows.store import WorkflowStore

    store = WorkflowStore(Database(paths.database_path()))
    definitions = store.list_definitions()
    table = Table(title="Workflow definitions")
    table.add_column("workflow_id")
    table.add_column("version", justify="right")
    table.add_column("name")
    table.add_column("nodes", justify="right")
    for definition in definitions:
        table.add_row(
            definition.workflow_id,
            str(definition.version),
            definition.name,
            str(len(definition.nodes)),
        )
    Console().print(table)


@autonomy_workflows.command("start")
@click.argument("workflow_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
@click.option("--input", "inputs", multiple=True, help="key=value input pair.")
@click.option("--goal", "goal_id", default=None)
def workflows_start(
    workflow_id: str, owner: str, inputs: tuple[str, ...], goal_id: str | None
) -> None:
    from eurus.storage.database import Database
    from eurus.workflows.engine import WorkflowEngine

    provided = _parse_inputs(inputs)
    database = Database(paths.database_path())
    workflows = WorkflowEngine(database, _offline_automation(database))
    try:
        instance = workflows.start(workflow_id, owner, inputs=provided, goal_id=goal_id)
    except (KeyError, ValueError) as exc:
        database.close()
        raise click.ClickException(str(exc)) from exc
    database.close()
    Console().print(f"started instance {instance.instance_id} of {workflow_id}")


@autonomy_workflows.command("show")
@click.argument("instance_id")
@click.option("--owner", default=None, help="Restrict the lookup to this owner.")
def workflows_show(instance_id: str, owner: str | None) -> None:
    from eurus.storage.database import Database
    from eurus.workflows.store import WorkflowStore

    store = WorkflowStore(Database(paths.database_path()))
    instance = store.get_instance(instance_id, owner_id=owner)
    if instance is None:
        raise click.ClickException("instance not found")
    console = Console()
    console.print(
        f"[bold]{instance.workflow_id} v{instance.version}[/bold] status={instance.status.value}"
    )
    for node in store.list_nodes(instance_id):
        job = f" job={node.job_id[:12]}" if node.job_id else ""
        error = f" error={node.error[:60]}" if node.error else ""
        console.print(f"  {node.node_id} [{node.node_type}] {node.status.value}{job}{error}")
    for approval in store.list_approvals(instance_id=instance_id):
        state = approval.decision or "pending"
        console.print(f"  approval {approval.approval_id[:12]} [{state}] {approval.note[:60]}")


def _workflow_control(action: str) -> Callable[[str, str], None]:
    from eurus.storage.database import Database
    from eurus.workflows.engine import WorkflowEngine

    def control(instance_id: str, owner: str) -> None:
        database = Database(paths.database_path())
        workflows = WorkflowEngine(database, _offline_automation(database))
        result = getattr(workflows, action)(instance_id, owner)
        database.close()
        if result is None:
            raise click.ClickException("instance not found or not allowed")
        Console().print(f"instance {result.instance_id} -> {result.status.value}")

    return control


@autonomy_workflows.command("pause")
@click.argument("instance_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def workflows_pause(instance_id: str, owner: str) -> None:
    _workflow_control("pause")(instance_id, owner)


@autonomy_workflows.command("resume")
@click.argument("instance_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def workflows_resume(instance_id: str, owner: str) -> None:
    _workflow_control("resume")(instance_id, owner)


@autonomy_workflows.command("cancel")
@click.argument("instance_id")
@click.option("--owner", default=OWNER_ID, show_default=True)
def workflows_cancel(instance_id: str, owner: str) -> None:
    _workflow_control("cancel")(instance_id, owner)


@autonomy_workflows.command("approvals")
@click.option("--pending-only", is_flag=True, default=True)
def workflows_approvals(pending_only: bool) -> None:
    from eurus.storage.database import Database
    from eurus.workflows.store import WorkflowStore

    store = WorkflowStore(Database(paths.database_path()))
    approvals = store.list_approvals(pending=pending_only)
    table = Table(title="Workflow approvals")
    table.add_column("approval_id")
    table.add_column("instance")
    table.add_column("node")
    table.add_column("decision")
    table.add_column("prompt")
    for approval in approvals:
        table.add_row(
            approval.approval_id[:12],
            approval.instance_id[:12],
            approval.node_id,
            approval.decision or "pending",
            approval.note[:50],
        )
    Console().print(table)


@autonomy_workflows.command("decide")
@click.argument("approval_id")
@click.argument("decision", type=click.Choice(["approved", "rejected"]))
@click.option("--by", "decided_by", default=OWNER_ID, show_default=True)
@click.option("--note", default="")
@click.option("--owner", default=None, help="Restrict the decision to this owner.")
def workflows_decide(
    approval_id: str, decision: str, decided_by: str, note: str, owner: str | None
) -> None:
    from eurus.storage.database import Database
    from eurus.workflows.engine import WorkflowEngine

    database = Database(paths.database_path())
    workflows = WorkflowEngine(database, _offline_automation(database))
    decided = workflows.decide_approval(
        approval_id, decision=decision, decided_by=decided_by, note=note, owner_id=owner
    )
    database.close()
    if not decided:
        raise click.ClickException("approval could not be decided")
    Console().print(f"approval {approval_id[:12]} -> {decision}")


@autonomy_group.group("agents")
def autonomy_agents() -> None:
    pass


@autonomy_agents.command("list")
def agents_list() -> None:
    from eurus.multiagent.registry import SpecialistRegistry
    from eurus.storage.database import Database

    registry = SpecialistRegistry(Database(paths.database_path()))
    table = Table(title="Specialists")
    table.add_column("role")
    table.add_column("enabled")
    table.add_column("concurrency", justify="right")
    table.add_column("tools")
    table.add_column("description")
    for specialist in registry.list_specialists(enabled_only=False):
        table.add_row(
            specialist.role,
            str(specialist.enabled),
            str(specialist.max_concurrent),
            ",".join(specialist.allowed_tools)[:40],
            specialist.description[:50],
        )
    Console().print(table)


@autonomy_agents.command("register")
@click.argument("role")
@click.option("--description", default="")
@click.option("--capability", "capabilities", multiple=True)
@click.option("--tool", "allowed_tools", multiple=True)
@click.option("--max-concurrent", type=click.IntRange(1, 32), default=2, show_default=True)
@click.option("--delegation", is_flag=True, default=False, help="Allow nested delegation.")
def agents_register(
    role: str,
    description: str,
    capabilities: tuple[str, ...],
    allowed_tools: tuple[str, ...],
    max_concurrent: int,
    delegation: bool,
) -> None:
    from eurus.multiagent.registry import SpecialistRegistry
    from eurus.storage.database import Database

    registry = SpecialistRegistry(Database(paths.database_path()))
    try:
        specialist = registry.register(
            role,
            description=description,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
            max_concurrent=max_concurrent,
            delegation_allowed=delegation,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    Console().print(f"registered specialist {specialist.role} ({specialist.specialist_id[:12]})")


@autonomy_agents.command("enable")
@click.argument("role")
def agents_enable(role: str) -> None:
    _agents_toggle(role, True)


@autonomy_agents.command("disable")
@click.argument("role")
def agents_disable(role: str) -> None:
    _agents_toggle(role, False)


def _agents_toggle(role: str, enabled: bool) -> None:
    from eurus.multiagent.registry import SpecialistRegistry
    from eurus.storage.database import Database

    registry = SpecialistRegistry(Database(paths.database_path()))
    if not registry.set_enabled(role, enabled):
        raise click.ClickException("specialist not found")
    Console().print(f"{role} -> {'enabled' if enabled else 'disabled'}")


@autonomy_agents.command("assignments")
@click.option("--parent", default=None, help="Filter by parent run id.")
@click.option("--role", default=None)
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def agents_assignments(parent: str | None, role: str | None, limit: int) -> None:
    from eurus.multiagent.orchestrator import AgentOrchestrator
    from eurus.storage.database import Database

    database = Database(paths.database_path())
    orchestrator = AgentOrchestrator(database, _offline_automation(database))
    assignments = orchestrator.store.list_assignments(parent_run_id=parent, role=role, limit=limit)
    database.close()
    table = Table(title="Specialist assignments")
    table.add_column("assignment")
    table.add_column("role")
    table.add_column("status")
    table.add_column("objective")
    for assignment in assignments:
        table.add_row(
            assignment.assignment_id[:12],
            assignment.role,
            assignment.status,
            assignment.objective[:60],
        )
    Console().print(table)


@autonomy_agents.command("dispatch")
@click.argument("objective")
@click.argument("role")
@click.option("--owner", default=OWNER_ID, show_default=True)
@click.option("--parent", "parent_run_id", default=None)
def agents_dispatch(objective: str, role: str, owner: str, parent_run_id: str | None) -> None:
    from eurus.multiagent.orchestrator import AgentOrchestrator
    from eurus.storage.database import Database

    database = Database(paths.database_path())
    orchestrator = AgentOrchestrator(database, _offline_automation(database))
    try:
        assignment = orchestrator.dispatch(
            objective, role=role, owner_id=owner, parent_run_id=parent_run_id
        )
    except (KeyError, ValueError, PermissionError, RuntimeError) as exc:
        database.close()
        raise click.ClickException(str(exc)) from exc
    database.close()
    Console().print(f"dispatched assignment {assignment.assignment_id} to {role}")


@autonomy_group.group("events")
def autonomy_events() -> None:
    pass


@autonomy_events.command("publish")
@click.argument("event_type")
@click.option("--subject-type", default="")
@click.option("--subject-id", default="")
@click.option("--payload", default="{}", help="JSON payload.")
@click.option("--dedup-key", default=None)
@click.option("--owner", default=None, help="Owner scope for the event (global when omitted).")
def events_publish(
    event_type: str,
    subject_type: str,
    subject_id: str,
    payload: str,
    dedup_key: str | None,
    owner: str | None,
) -> None:
    from eurus.events.bus import EventBus
    from eurus.storage.database import Database

    bus = EventBus(Database(paths.database_path()))
    try:
        body = json.loads(payload) if payload.strip() else {}
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"payload is not valid JSON: {exc}") from exc
    event = bus.publish(
        event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=body,
        dedup_key=dedup_key,
        owner_id=owner,
    )
    if event is None:
        Console().print("event deduplicated: no new event created")
        return
    scope = event.owner_id or "global"
    Console().print(f"published {event.event_type} ({event.event_id[:12]}) owner={scope}")


@autonomy_events.command("list")
@click.option("--type", "event_type", default=None)
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def events_list(event_type: str | None, limit: int) -> None:
    from eurus.events.store import DomainEventStore
    from eurus.storage.database import Database

    store = DomainEventStore(Database(paths.database_path()))
    events = store.list_events(event_type=event_type, limit=limit)
    table = Table(title="Domain events")
    table.add_column("event_id")
    table.add_column("type")
    table.add_column("subject")
    table.add_column("occurred_at")
    for event in events:
        table.add_row(
            event.event_id[:12],
            event.event_type,
            f"{event.subject_type}:{event.subject_id}"[:40],
            event.occurred_at.isoformat(timespec="seconds"),
        )
    Console().print(table)


@autonomy_events.command("subscribe")
@click.argument("name")
@click.option("--type", "event_types", multiple=True, help="Event type; repeatable, or '*'.")
@click.option("--subject-type", default="")
@click.option("--subject-id", default="")
@click.option(
    "--handler",
    type=click.Choice(["submit_job", "trigger_schedules", "notify"]),
    default="submit_job",
    show_default=True,
)
@click.option("--objective", default=None, help="Objective template for submit_job handlers.")
@click.option("--owner", default=OWNER_ID, show_default=True)
def events_subscribe(
    name: str,
    event_types: tuple[str, ...],
    subject_type: str,
    subject_id: str,
    handler: str,
    objective: str | None,
    owner: str,
) -> None:
    from eurus.events.bus import EventBus
    from eurus.storage.database import Database

    config: dict[str, Any] = {"owner_id": owner}
    if objective is not None:
        config["objective"] = objective
    bus = EventBus(Database(paths.database_path()))
    try:
        subscription = bus.subscribe(
            name,
            event_types=event_types,
            subject_type=subject_type,
            subject_id=subject_id,
            handler_kind=handler,
            handler_config=config,
            owner_id=owner,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    Console().print(f"subscription {subscription.name} ({subscription.subscription_id[:12]})")


@autonomy_events.command("unsubscribe")
@click.argument("subscription_id")
def events_unsubscribe(subscription_id: str) -> None:
    from eurus.events.bus import EventBus
    from eurus.storage.database import Database

    bus = EventBus(Database(paths.database_path()))
    if not bus.unsubscribe(subscription_id):
        raise click.ClickException("subscription not found")
    Console().print(f"removed subscription {subscription_id[:12]}")


@autonomy_events.command("stats")
def events_stats() -> None:
    from eurus.events.store import DomainEventStore
    from eurus.storage.database import Database

    stats = DomainEventStore(Database(paths.database_path())).stats()
    table = Table(title="Event bus stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key, value in stats.items():
        table.add_row(key, str(value))
    Console().print(table)


@autonomy_group.group("jobs")
def autonomy_jobs() -> None:
    pass


@autonomy_jobs.command("list")
@click.option("--owner", default=None)
@click.option("--status", "statuses", multiple=True)
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def jobs_list(owner: str | None, statuses: tuple[str, ...], limit: int) -> None:
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    store = JobStore(Database(paths.database_path()))
    jobs = store.list_jobs(
        owner_id=owner,
        statuses=tuple(statuses),
        limit=limit,
    )
    table = Table(title="Automation jobs")
    table.add_column("job_id")
    table.add_column("status")
    table.add_column("source")
    table.add_column("objective")
    for job in jobs:
        table.add_row(job.job_id[:12], job.status, job.source, job.objective[:60])
    Console().print(table)


@autonomy_jobs.command("retry")
@click.argument("job_id")
@click.option("--owner", default=None)
def jobs_retry(job_id: str, owner: str | None) -> None:
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    store = JobStore(Database(paths.database_path()))
    job = store.replay(job_id, owner_id=owner)
    if job is None:
        raise click.ClickException("job cannot be replayed")
    Console().print(f"replayed job {job.job_id} (attempt reset)")


@autonomy_jobs.command("dead-letters")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def jobs_dead_letters(limit: int) -> None:
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    store = JobStore(Database(paths.database_path()))
    letters = store.list_dead_letters(limit=limit)
    table = Table(title="Dead letters")
    table.add_column("id")
    table.add_column("job")
    table.add_column("reason")
    table.add_column("detail")
    for letter in letters:
        table.add_row(
            letter.dead_letter_id[:12],
            letter.job_id[:12],
            letter.reason,
            (letter.detail or "")[:60],
        )
    Console().print(table)


@autonomy_jobs.command("resolve")
@click.argument("dead_letter_id")
@click.option(
    "--resolution",
    type=click.Choice(["replayed", "discarded", "completed"]),
    default="discarded",
    show_default=True,
)
@click.option("--owner", default=None, help="Restrict the resolution to this owner.")
def jobs_resolve(dead_letter_id: str, resolution: str, owner: str | None) -> None:
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    store = JobStore(Database(paths.database_path()))
    try:
        resolved = store.resolve_dead_letter(dead_letter_id, resolution=resolution, owner_id=owner)
    except KeyError as exc:
        raise click.ClickException("dead letter not found") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if resolved is None:
        raise click.ClickException("dead letter not found")
    Console().print(f"resolved {dead_letter_id[:12]}: {resolution}")


def _offline_automation(database: Any) -> Any:
    from eurus.automation.engine import AutomationEngine

    return AutomationEngine(database, _NoopExecutor())


class _NoopExecutor:
    async def execute(self, job: Any, context: Any) -> str:
        raise RuntimeError("automation daemon is not running in this command")


def _parse_inputs(inputs: tuple[str, ...]) -> dict[str, Any]:
    provided: dict[str, Any] = {}
    for pair in inputs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise click.ClickException(f"invalid input pair: {pair!r}; expected key=value")
        provided[key] = value
    return provided
