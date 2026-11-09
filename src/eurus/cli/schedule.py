from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import _state, main


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
    store = ScheduleStore(paths.database_path())
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

    store = ScheduleStore(paths.database_path())
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

    schedules = ScheduleStore(paths.database_path()).list_owner(owner)
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

    store = ScheduleStore(paths.database_path())
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

    store = ScheduleStore(paths.database_path())
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

    store = ScheduleStore(paths.database_path())
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

    database_path = paths.database_path()
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
    from eurus.cli.autonomy import run_daemon

    config = _state(ctx).config
    click.echo(f"Scheduler running for owner {owner} with {workers} workers. Press Ctrl+C to stop.")
    try:
        asyncio.run(
            run_daemon(
                config,
                poll_seconds=float(poll_seconds),
                workers=workers,
                owner=owner,
            )
        )
    except KeyboardInterrupt:
        click.echo("Scheduler stopped.")
