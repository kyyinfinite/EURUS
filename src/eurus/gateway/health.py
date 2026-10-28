from __future__ import annotations

from dataclasses import dataclass

from eurus.gateway.tasks import SAFE_RESUME_ACTIONS, TaskStore


@dataclass(frozen=True, slots=True)
class TaskReport:
    task_id: str
    status: str
    updated_at: str
    error: str
    recovery_action: str


@dataclass(frozen=True, slots=True)
class GatewayStatus:
    counts: dict[str, int]
    needs_review: list[TaskReport]
    recent_failures: list[TaskReport]


def _report(task_id: str, status: str, updated_at: str, error: str, action: str) -> TaskReport:
    return TaskReport(task_id[:8], status, updated_at, error, action)


def describe_gateway(store: TaskStore, *, limit: int = 10) -> GatewayStatus:
    review: list[TaskReport] = []
    for task in store.list_by_status("interrupted", 100):
        action = TaskStore.stored_action(task.state)
        if action in SAFE_RESUME_ACTIONS:
            continue
        review.append(_report(task.task_id, task.status, task.updated_at, task.error, action.value))
    failures = [
        _report(task.task_id, task.status, task.updated_at, task.error, "")
        for task in store.list_by_status("failed", limit)
    ]
    return GatewayStatus(store.count_by_status(), review[:limit], failures)
