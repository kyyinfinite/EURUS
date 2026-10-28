from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType

from eurus.runtime.models import TERMINAL_STATUSES, Run, RunStatus


class InvalidRunTransition(ValueError):
    pass


_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.INTERRUPTED, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_INPUT,
            RunStatus.RETRY_WAIT,
            RunStatus.VERIFYING,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_INPUT: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.INTERRUPTED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.RETRY_WAIT: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.INTERRUPTED: frozenset(
        {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


ALLOWED_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType(_TRANSITIONS)


def can_transition(current: RunStatus, new_status: RunStatus) -> bool:
    return new_status in _TRANSITIONS[current]


def transition_run(
    run: Run,
    new_status: RunStatus,
    *,
    stop_reason: str | None = None,
    result_ref: str | None = None,
) -> Run:
    if not can_transition(run.status, new_status):
        raise InvalidRunTransition(
            f"cannot transition run from {run.status.value!r} to {new_status.value!r}"
        )
    now = datetime.now(UTC)
    run.status = new_status
    run.status_version += 1
    run.updated_at = now
    if new_status == RunStatus.RUNNING and run.started_at is None:
        run.started_at = now
    if new_status in TERMINAL_STATUSES:
        run.finished_at = now
    if stop_reason is not None:
        run.stop_reason = stop_reason
    if result_ref is not None:
        run.result_ref = result_ref
    return run
