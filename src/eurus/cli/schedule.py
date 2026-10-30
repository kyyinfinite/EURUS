"""Scheduler commands and the durable schedule daemon."""

from __future__ import annotations

import asyncio
import contextlib

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.agent.factory import build_agent
from eurus.cli.base import _state, main
from eurus.cli.common import _build_engine, _deny_remote_approval
from eurus.mcp.manager import MCPToolManager
from eurus.providers.factory import build_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.models import RunStatus
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.tools.builtin import default_registry


@main.group("schedule")
def schedule_group() -> None:
    pass


@schedule_group.command("add")
@click.argument("objective")
@click.option("--owner", default="local", show_default=True)
@click.option("--timezone", default="UTC", show_default=True)
@click.option("--at", "run_at", default=None, help="One-time ISO-8601 datetime.")
@click.option(
    "--every",
    "interval_seconds",
    type=click.IntRange(60, 31536000),
    default=None,
    help="Recurring interval in seconds, minimum 60.",
)
@click.option("--profile", "profile_name", default=None, help="Saved task profile name.")
@click.pass_context
def schedule_add(
    ctx: click.Context,
    objective: str,
    owner: str,
    timezone: str,
    run_at: str | None,
    interval_seconds: int | None,
    profile_name: str | None,
) -> None:
    from eurus.scheduler import ScheduleStore

    if (run_at is None) == (interval_seconds is None):
        raise click.ClickException("provide exactly one of --at or --every")
    store = ScheduleStore(paths.home_dir() / "sessions.sqlite3")
    try:
        profile = store.get_profile(owner, profile_name) if profile_name else None
        if profile_name and profile is None:
            raise ValueError("saved task profile not found for this owner")
        schedule = store.create(
            owner,
            objective,
            kind="once" if run_at else "interval",
            run_at=run_at,
            interval_seconds=interval_seconds,
            timezone=timezone,
            profile=profile,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Schedule created: {schedule.schedule_id} (next run {schedule.next_run_at})")


@schedule_group.command("profile-save")
@click.argument("name")
@click.option("--owner", default="local", show_default=True)
@click.option("--timezone", default="UTC", show_default=True)
@click.option("--max-runtime", type=click.IntRange(1, 86400), default=1800, show_default=True)
@click.option("--max-steps", type=click.IntRange(1, 1000), default=20, show_default=True)
@click.option("--no-notifications", is_flag=True, default=False)
def schedule_profile_save(
    name: str,
    owner: str,
    timezone: str,
    max_runtime: int,
    max_steps: int,
    no_notifications: bool,
) -> None:
    from eurus.scheduler import ScheduleStore, TaskProfile

    store = ScheduleStore(paths.home_dir() / "sessions.sqlite3")
    profile = TaskProfile(
        name=name,
        timezone=timezone,
        max_runtime_seconds=max_runtime,
        max_steps=max_steps,
        notifications=not no_notifications,
    )
    try:
        store.save_profile(owner, profile)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Saved task profile: {name}")


@schedule_group.command("list")
@click.option("--owner", default="local", show_default=True)
def schedule_list(owner: str) -> None:
    from eurus.scheduler import ScheduleStore

    schedules = ScheduleStore(paths.home_dir() / "sessions.sqlite3").list_owner(owner)
    table = Table(title=f"Schedules for {owner}")
    for column in ("ID", "State", "Kind", "Next run (UTC)", "Objective"):
        table.add_column(column)
    for item in schedules:
        table.add_row(
            item.schedule_id[:8],
            "enabled" if item.enabled else "disabled",
            item.kind,
            item.next_run_at,
            item.objective[:72],
        )
    Console().print(table)


@schedule_group.command("enable")
@click.argument("schedule_id")
@click.option("--owner", default="local", show_default=True)
def schedule_enable(schedule_id: str, owner: str) -> None:
    from eurus.scheduler import ScheduleStore

    store = ScheduleStore(paths.home_dir() / "sessions.sqlite3")
    item = next(
        (entry for entry in store.list_owner(owner) if entry.schedule_id.startswith(schedule_id)),
        None,
    )
    if item is None or not store.set_enabled(item.schedule_id, owner, True):
        raise click.ClickException("schedule not found")
    click.echo(f"Enabled {item.schedule_id}")


@schedule_group.command("disable")
@click.argument("schedule_id")
@click.option("--owner", default="local", show_default=True)
def schedule_disable(schedule_id: str, owner: str) -> None:
    from eurus.scheduler import ScheduleStore

    store = ScheduleStore(paths.home_dir() / "sessions.sqlite3")
    item = next(
        (entry for entry in store.list_owner(owner) if entry.schedule_id.startswith(schedule_id)),
        None,
    )
    if item is None or not store.set_enabled(item.schedule_id, owner, False):
        raise click.ClickException("schedule not found")
    click.echo(f"Disabled {item.schedule_id}")


@schedule_group.command("history")
@click.argument("schedule_id")
@click.option("--owner", default="local", show_default=True)
def schedule_history(schedule_id: str, owner: str) -> None:
    from eurus.scheduler import ScheduleStore

    store = ScheduleStore(paths.home_dir() / "sessions.sqlite3")
    item = next(
        (entry for entry in store.list_owner(owner) if entry.schedule_id.startswith(schedule_id)),
        None,
    )
    if item is None:
        raise click.ClickException("schedule not found")
    for run in store.history(item.schedule_id):
        click.echo(f"{run.started_at} {run.status} {run.result[:120]}")


@schedule_group.command("cancel-run")
@click.argument("job_id")
@click.option("--owner", default="local", show_default=True)
def schedule_cancel_run(job_id: str, owner: str) -> None:
    from eurus.scheduler import ScheduleStore
    from eurus.storage.jobs import JobStore

    database_path = paths.home_dir() / "sessions.sqlite3"
    jobs = JobStore(database_path)
    job = next(
        (
            item
            for item in jobs.list_jobs(owner_id=owner, limit=1000)
            if item.job_id.startswith(job_id)
        ),
        None,
    )
    if job is None or not jobs.request_cancel(job.job_id, owner_id=owner):
        raise click.ClickException("queued/running job not found or cannot be cancelled")
    if job.status == "queued":
        ScheduleStore(database_path).finish_run(
            job.job_id, status="cancelled", result="cancelled by owner"
        )
    click.echo(f"Cancellation requested for job {job.job_id}")


@schedule_group.command("daemon")
@click.option("--poll-seconds", type=click.IntRange(1, 300), default=15, show_default=True)
@click.option("--workers", type=click.IntRange(1, 16), default=2, show_default=True)
@click.option("--owner", default="local", show_default=True)
@click.pass_context
def schedule_daemon(ctx: click.Context, poll_seconds: int, workers: int, owner: str) -> None:
    from eurus.automation.worker import AutomationWorkerPool
    from eurus.runtime.models import Run
    from eurus.scheduler import ScheduleStore
    from eurus.storage.jobs import Job, JobStore

    config = _state(ctx).config
    database_path = paths.home_dir() / "sessions.sqlite3"
    store = ScheduleStore(database_path)
    jobs = JobStore(database_path)
    events = EventStore(jobs.database)

    async def execute_job(job: Job) -> str:
        store.mark_run_started(job.job_id)
        schedule = store.get(job.schedule_id or "", owner_id=job.owner_id)
        if schedule is None:
            raise RuntimeError("schedule_unavailable")
        profile = job.payload.get("profile", {})
        timeout = int(profile.get("max_runtime_seconds", 1800))
        router = build_router(config)
        mcp = MCPToolManager.load(paths.home_dir() / "mcp.json")
        try:
            await mcp.connect()
            registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
            mcp.register_tools(registry)
            agent = build_agent(
                config,
                router,
                registry=registry,
                approver=lambda call, tool: _deny_remote_approval(call, tool),
                stream=False,
                lessons=True,
                learn=False,
            )
            runtime_run = Run(
                run_id=job.job_id,
                owner_id=job.owner_id,
                objective=job.objective,
                budget={
                    "max_iterations": int(profile.get("max_steps", 20)),
                    "max_runtime_seconds": timeout,
                },
            )
            engine = _build_engine(
                config,
                agent,
                run_store=RunStore(events.database),
                event_store=events,
                attempt_store=None,
            )
            result = await engine.run(
                job.objective,
                owner_id=job.owner_id,
                run=runtime_run,
                budget=BudgetSpec(
                    max_iterations=int(profile.get("max_steps", 20)),
                    max_runtime_seconds=timeout,
                ),
            )
            if result.run.status != RunStatus.COMPLETED or not result.output.strip():
                raise RuntimeError(result.run.stop_reason or "no_verifiable_text_result")
            return result.output.strip()
        finally:
            from eurus.browser.integration import close_registry_browser

            with contextlib.suppress(Exception):
                await close_registry_browser(registry)
            await mcp.close()
            await router.aclose()

    def finish_job(job: Job, status: str, result: str, error: str | None) -> None:
        schedule_status = status
        if error and status == "failed":
            result = error
        store.finish_run(job.job_id, status=schedule_status, result=result)

    pool = AutomationWorkerPool(
        jobs,
        execute_job,
        workers=workers,
        poll_seconds=float(poll_seconds),
        lease_seconds=60,
        on_finish=finish_job,
    )

    async def dispatch_loop() -> None:
        while True:
            store.dispatch_due(limit=100, owner_id=owner)
            for job in jobs.list_jobs(
                owner_id=owner, statuses=("unknown_outcome", "failed", "cancelled"), limit=100
            ):
                store.finish_run(job.job_id, status=job.status, result=job.error or job.result)
            await asyncio.sleep(poll_seconds)

    async def run_daemon() -> None:
        dispatcher = asyncio.create_task(dispatch_loop())
        workers_task = asyncio.create_task(pool.run())
        try:
            await asyncio.gather(dispatcher, workers_task)
        finally:
            await pool.stop()
            dispatcher.cancel()
            workers_task.cancel()
            await asyncio.gather(dispatcher, workers_task, return_exceptions=True)

    click.echo(f"Scheduler running for owner {owner} with {workers} workers. Press Ctrl+C to stop.")
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        click.echo("Scheduler stopped.")
