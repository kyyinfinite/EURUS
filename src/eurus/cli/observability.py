"""Run inspection, trace, and recovery commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

from eurus import paths
from eurus.agent.factory import build_agent
from eurus.cli.base import _state, main
from eurus.cli.common import _approve, _build_engine
from eurus.coding.checkpoint import CheckpointStore
from eurus.errors import EurusError
from eurus.providers.factory import build_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine, RunResult
from eurus.runtime.models import RunStatus, RuntimeEvent
from eurus.runtime.resume import RecoveryBlocked, RecoveryManager
from eurus.runtime.state_machine import InvalidRunTransition, transition_run
from eurus.storage.events import EventStore
from eurus.storage.runs import ConcurrentRunUpdate, RunStore


def _runtime_stores() -> tuple[RunStore, EventStore]:
    database = paths.home_dir() / "sessions.sqlite3"
    return RunStore(database), EventStore(database)


def _safe_payload(value: Any, key: str = "") -> Any:
    sensitive_parts = ("secret", "password", "credential", "api_key", "token", "authorization")
    if any(part in key.lower() for part in sensitive_parts):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _safe_payload(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in ("bearer ", "sk-", "api_key=", "password=")):
            return "[REDACTED]"
        return value[:500]
    return value


@main.command("trace")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--limit", type=click.IntRange(1, 10000), default=5000, show_default=True)
def trace_run(run_id: str, as_json: bool, limit: int) -> None:
    from eurus.runtime.trace import TraceBuilder

    runs, events = _runtime_stores()
    builder = TraceBuilder(runs, events)
    try:
        if as_json:
            click.echo(json.dumps(builder.build(run_id, limit=limit).to_dict(), indent=2))
        else:
            click.echo(builder.render(run_id, limit=limit))
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("inspect")
@click.argument("run_id")
def inspect_run(run_id: str) -> None:
    runs, events = _runtime_stores()
    run = runs.get(run_id)
    if run is None:
        raise click.ClickException(f"run not found: {run_id}")
    checkpoint = runs.latest_checkpoint(run_id)
    children = runs.list_runs(parent_run_id=run_id, limit=1000)
    payload = {
        "run_id": run.run_id,
        "owner_id": run.owner_id,
        "objective": _safe_payload(run.objective),
        "parent_run_id": run.parent_run_id,
        "child_run_ids": [item.run_id for item in children],
        "session_id": run.session_id,
        "status": run.status.value,
        "stop_reason": run.stop_reason,
        "budget": run.budget,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "checkpoint": (
            {
                "version": checkpoint["checkpoint_version"],
                "type": checkpoint["checkpoint_type"],
                "created_at": checkpoint["created_at"].isoformat(),
            }
            if checkpoint
            else None
        ),
        "event_count": len(events.list_for_run(run_id, limit=10000)),
        "result_ref": run.result_ref,
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@main.command("history")
@click.option("--owner", "owner_id", default=None, help="Filter by run owner.")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice([item.value for item in RunStatus]),
)
@click.option("--limit", type=click.IntRange(1, 1000), default=50, show_default=True)
def run_history(owner_id: str | None, statuses: tuple[str, ...], limit: int) -> None:
    runs, _ = _runtime_stores()
    items = runs.list_runs(
        owner_id=owner_id,
        statuses=tuple(RunStatus(item) for item in statuses),
        limit=limit,
    )
    if not items:
        click.echo("No runtime runs found.")
        return
    click.echo(
        json.dumps(
            [
                {
                    "run_id": item.run_id,
                    "status": item.status.value,
                    "owner_id": item.owner_id,
                    "created_at": item.created_at.isoformat(),
                    "parent_run_id": item.parent_run_id,
                    "stop_reason": item.stop_reason,
                }
                for item in items
            ],
            indent=2,
            ensure_ascii=False,
        )
    )


@main.command("events")
@click.argument("run_id")
@click.option("--after", "after_sequence", type=click.IntRange(0), default=0, show_default=True)
@click.option("--limit", type=click.IntRange(1, 10000), default=1000, show_default=True)
def run_events(run_id: str, after_sequence: int, limit: int) -> None:
    runs, events = _runtime_stores()
    if runs.get(run_id) is None:
        raise click.ClickException(f"run not found: {run_id}")
    items = events.list_for_run(run_id, after_sequence=after_sequence, limit=limit)
    for item in items:
        payload = {
            "sequence": item.sequence_no,
            "event_id": item.event_id,
            "type": item.event_type,
            "timestamp": item.created_at.isoformat(),
            "run_id": item.run_id,
            "parent_run_id": item.parent_run_id,
            "step_id": item.step_id,
            "attempt_id": item.attempt_id,
            "payload": _safe_payload(item.payload),
        }
        click.echo(json.dumps(payload, ensure_ascii=False))
    if not items:
        click.echo("No runtime events found for this run.")


@main.command("cancel")
@click.argument("run_id")
def cancel_run(run_id: str) -> None:
    runs, events = _runtime_stores()
    run = runs.get(run_id)
    if run is None:
        raise click.ClickException(f"run not found: {run_id}")
    if run.status == RunStatus.CANCELLED:
        click.echo(f"run already cancelled: {run_id}")
        return
    if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
        raise click.ClickException(f"terminal run cannot be cancelled: {run.status.value}")
    if run.status == RunStatus.RUNNING:
        if not runs.request_cancel(run_id):
            raise click.ClickException(
                "cancellation request was not accepted because the run state changed"
            )
        click.echo(f"cancellation requested: {run_id}")
        return
    previous_version = run.status_version
    try:
        transition_run(run, RunStatus.CANCELLED, stop_reason="operator_cancelled")
        runs.save(run, expected_status_version=previous_version)
    except (InvalidRunTransition, ConcurrentRunUpdate) as exc:
        raise click.ClickException(f"cancellation not applied: {exc}") from exc
    sequence = events.last_sequence(run_id) + 1
    events.append(
        RuntimeEvent(
            run_id=run_id,
            event_type="run.cancelled",
            sequence_no=sequence,
            parent_run_id=run.parent_run_id,
            payload={"source": "operator"},
        )
    )
    click.echo(f"cancelled run: {run_id}")


@main.command("resume")
@click.argument("run_id")
@click.option("--max-iterations", default=200, show_default=True, help="Iteration budget on resume")
@click.pass_context
def resume_run(ctx: click.Context, run_id: str, max_iterations: int) -> None:
    config = _state(ctx).config
    runs, events = _runtime_stores()
    manager = RecoveryManager(runs)
    try:
        plan = manager.inspect(run_id)
    except KeyError as exc:
        raise click.ClickException(f"run not found: {run_id}") from exc
    click.echo(f"recovery action: {plan.action.value}")
    if plan.action.value == "terminal":
        raise click.ClickException(f"terminal run cannot be resumed: {plan.run.status.value}")
    if plan.action.value == "reconcile_unknown_outcome":
        raise click.ClickException(
            "resume blocked: unresolved tool side effects require manual reconciliation "
            "before replay"
        )
    if plan.action.value == "manual_review":
        raise click.ClickException(
            "resume blocked: recovery state or checkpoint is insufficient; manual review required"
        )

    async def go() -> RunResult:
        router = build_router(config)

        def factory(_resumed: object) -> AgentEngine:
            agent = build_agent(
                config,
                router,
                approver=_approve,
                stream=False,
                lessons=True,
                learn=False,
            )
            return _build_engine(
                config,
                agent,
                run_store=runs,
                event_store=events,
                attempt_store=None,
            )

        try:
            return await manager.resume(
                run_id,
                factory,
                budget=BudgetSpec(
                    max_iterations=max(1, max_iterations),
                    max_runtime_seconds=1200.0,
                    max_child_runs=4,
                ),
            )
        finally:
            await router.aclose()

    try:
        result = asyncio.run(go())
    except RecoveryBlocked as exc:
        raise click.ClickException(str(exc)) from exc
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"resumed run {result.run.run_id}: status={result.run.status.value} "
        f"stop_reason={result.run.stop_reason} "
        f"verification={result.verification_status.value}"
    )


@main.command("undo")
@click.argument("run_id")
@click.option(
    "--workspace",
    default=".",
    show_default=True,
    help="Workspace the run mutated; undo refuses files changed after the run.",
)
def undo_run(run_id: str, workspace: str) -> None:
    """Restore the files a finished run changed, using its pre-mutation checkpoint."""
    root = Path(workspace).expanduser().resolve()
    store = CheckpointStore()
    checkpoint = store.load(run_id)
    if checkpoint is None:
        raise click.ClickException(f"no checkpoint found for run: {run_id}")
    if checkpoint.workspace != str(root):
        raise click.ClickException(
            f"checkpoint belongs to workspace {checkpoint.workspace}; "
            f"rerun with --workspace {checkpoint.workspace}"
        )
    restored, refused = store.undo(run_id, root)
    for item in restored:
        click.echo(f"restored {item}")
    for item in refused:
        click.echo(f"refused {item}")
    if not restored and not refused:
        click.echo("checkpoint has no restorable files")
