from __future__ import annotations

import json
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import main

_OWNER = "local"


def _require_missions_enabled() -> None:
    from eurus.config import load_config

    flags = load_config().agentic_engineering
    if not flags.enabled:
        raise click.ClickException(
            "agentic engineering is disabled by configuration "
            "([agentic_engineering] enabled = false)"
        )


def _supervisor() -> Any:
    from eurus.agent.supervisor import MainSupervisor
    from eurus.cli.autonomy import _offline_automation
    from eurus.multiagent.orchestrator import AgentOrchestrator
    from eurus.storage.database import Database

    database = Database(paths.database_path())
    automation = _offline_automation(database)
    return MainSupervisor(database, AgentOrchestrator(database, automation))


def _parse_task_option(raw: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    tasks: list[dict[str, Any]] = []
    for item in raw:
        try:
            parsed = json.loads(item)
        except ValueError as exc:
            raise click.ClickException(f"--task is not valid JSON: {item[:120]}") from exc
        if not isinstance(parsed, dict):
            raise click.ClickException("each --task must be a JSON object")
        tasks.append(parsed)
    return tasks


@main.group("missions", help="Create, inspect and steer hierarchical engineering missions.")
def missions_group() -> None:
    pass


@missions_group.command("create")
@click.argument("objective")
@click.option("--criteria", default="", help="Semicolon-separated acceptance criteria.")
@click.option("--task", "tasks", multiple=True, help="Subtask JSON; repeatable.")
@click.option("--budget", default="", help="JSON budget envelope.")
def missions_create(objective: str, criteria: str, tasks: tuple[str, ...], budget: str) -> None:
    _require_missions_enabled()
    supervisor = _supervisor()
    criteria_list = [item.strip() for item in criteria.split(";") if item.strip()]
    budget_dict: dict[str, Any] = {}
    if budget.strip():
        try:
            parsed = json.loads(budget)
        except ValueError as exc:
            raise click.ClickException(f"--budget is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise click.ClickException("--budget must be a JSON object")
        budget_dict = parsed
    try:
        mission = supervisor.create_mission(
            objective,
            owner_id=_OWNER,
            acceptance_criteria=criteria_list,
            budget=budget_dict,
        )
        plan_tasks = _parse_task_option(tasks)
        if plan_tasks:
            supervisor.plan_mission(mission.mission_id, owner_id=_OWNER, tasks=plan_tasks)
            refreshed = supervisor.missions.get(mission.mission_id, owner_id=_OWNER)
            assert refreshed is not None
            mission = refreshed
    except (ValueError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    Console().print(
        f"mission {mission.mission_id} [{mission.status}] tasks={mission.plan.get('task_count', 0)}"
    )


@missions_group.command("list")
@click.option("--status", default=None, help="Comma-separated status filter.")
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def missions_list(status: str | None, limit: int) -> None:
    supervisor = _supervisor()
    statuses = tuple(item.strip() for item in (status or "").split(",") if item.strip())
    missions = supervisor.missions.list_missions(owner_id=_OWNER, statuses=statuses, limit=limit)
    table = Table(title="Missions")
    for column in ("mission_id", "status", "tasks", "objective"):
        table.add_column(column)
    for mission in missions:
        table.add_row(
            mission.mission_id[:12],
            mission.status,
            str(mission.plan.get("task_count", "-")),
            mission.objective[:60],
        )
    Console().print(table)


@missions_group.command("show")
@click.argument("mission_id")
def missions_show(mission_id: str) -> None:
    supervisor = _supervisor()
    try:
        report = supervisor.mission_report(mission_id, owner_id=_OWNER)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    console = Console()
    mission = report["mission"]
    console.print(f"[bold]{mission['objective']}[/bold]")
    console.print(
        f"status={mission['status']} owner={mission['owner_id']} version={mission['version']}"
    )
    for task in report["tasks"]:
        console.print(
            f"  task {task['task_key']} [{task['status']}] "
            f"assignment={task['assignment_id'] or '-'} {task['title'][:70]}"
        )
    aggregation = report["aggregation"]
    console.print(
        f"assignments: total={aggregation['total']} succeeded={aggregation['succeeded']} "
        f"failed={aggregation['failed']} pending={aggregation['pending']}"
    )


@missions_group.command("tasks")
@click.argument("mission_id")
def missions_tasks(mission_id: str) -> None:
    supervisor = _supervisor()
    try:
        tasks = supervisor.tasks.list_tasks(mission_id, owner_id=_OWNER)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title=f"Tasks for {mission_id[:12]}")
    for column in ("task_key", "status", "priority", "role", "assignment"):
        table.add_column(column)
    for task in tasks:
        table.add_row(
            task.task_key,
            task.status,
            str(task.priority),
            task.preferred_role or "-",
            (task.assignment_id or "-")[:12],
        )
    Console().print(table)


@missions_group.command("plan")
@click.argument("mission_id")
@click.option("--task", "tasks", multiple=True, required=True, help="Subtask JSON; repeatable.")
def missions_plan(mission_id: str, tasks: tuple[str, ...]) -> None:
    supervisor = _supervisor()
    plan_tasks = _parse_task_option(tasks)
    try:
        supervisor.plan_mission(mission_id, owner_id=_OWNER, tasks=plan_tasks)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    Console().print(f"mission {mission_id} planned with {len(plan_tasks or [])} task(s)")


def _lifecycle(action: str, mission_id: str) -> Any:
    supervisor = _supervisor()
    method = getattr(supervisor, action)
    try:
        return method(mission_id, owner_id=_OWNER)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@missions_group.command("start")
@click.argument("mission_id")
def missions_start(mission_id: str) -> None:
    mission = _lifecycle("start_mission", mission_id)
    Console().print(f"mission {mission.mission_id} -> {mission.status}")


@missions_group.command("cancel")
@click.argument("mission_id")
@click.option("--reason", default="", help="Why the mission is cancelled.")
def missions_cancel(mission_id: str, reason: str) -> None:
    supervisor = _supervisor()
    try:
        mission = supervisor.cancel_mission(mission_id, owner_id=_OWNER, reason=reason)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    Console().print(f"mission {mission.mission_id} -> {mission.status}")


@missions_group.command("resume")
@click.argument("mission_id")
def missions_resume(mission_id: str) -> None:
    mission = _lifecycle("resume_mission", mission_id)
    Console().print(f"mission {mission.mission_id} -> {mission.status}")


@missions_group.command("dispatch")
@click.argument("mission_id")
@click.option("--limit", type=click.IntRange(1, 100), default=10, show_default=True)
def missions_dispatch(mission_id: str, limit: int) -> None:
    _require_missions_enabled()
    from eurus.config import load_config

    if not load_config().agentic_engineering.hierarchical_dispatch:
        raise click.ClickException(
            "hierarchical dispatch is disabled by configuration "
            "([agentic_engineering] hierarchical_dispatch = false)"
        )
    supervisor = _supervisor()
    try:
        dispatched = supervisor.dispatch_ready(mission_id, owner_id=_OWNER, limit=limit)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not dispatched:
        Console().print("no ready task had a matching specialist; nothing dispatched")
    else:
        Console().print(f"dispatched {len(dispatched)} assignment(s) (durable; daemon executes)")


@missions_group.command("finalize")
@click.argument("mission_id")
@click.option(
    "--require-check",
    "required_checks",
    multiple=True,
    help="Verification check name that must have passed; repeatable.",
)
def missions_finalize(mission_id: str, required_checks: tuple[str, ...]) -> None:
    supervisor = _supervisor()
    try:
        decision = supervisor.finalize_mission(
            mission_id, owner_id=_OWNER, require_checks=required_checks
        )
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    console = Console()
    console.print(f"decision={decision.decision}")
    for reason in decision.reasons:
        console.print(f"  reason: {reason}")
    if decision.failed_check_names:
        console.print(f"  failed checks: {', '.join(decision.failed_check_names)}")
    if decision.unavailable_check_names:
        console.print(f"  missing checks: {', '.join(decision.unavailable_check_names)}")
