from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_SCHEDULE = "waiting_schedule"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_SCHEDULE = "waiting_schedule"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)
TERMINAL_NODE_STATUSES = frozenset(
    {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.SKIPPED,
        NodeStatus.CANCELLED,
    }
)

ALLOWED_WORKFLOW_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.PAUSED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.WAITING_SCHEDULE,
            WorkflowStatus.PAUSED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.WAITING_APPROVAL: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.WAITING_SCHEDULE: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.PAUSED: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}

ALLOWED_NODE_TRANSITIONS: Mapping[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset(
        {
            NodeStatus.RUNNING,
            NodeStatus.WAITING_APPROVAL,
            NodeStatus.WAITING_SCHEDULE,
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.RUNNING: frozenset({NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}),
    NodeStatus.WAITING_APPROVAL: frozenset(
        {
            NodeStatus.RUNNING,
            NodeStatus.SUCCEEDED,
            NodeStatus.SKIPPED,
            NodeStatus.FAILED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.WAITING_SCHEDULE: frozenset(
        {
            NodeStatus.RUNNING,
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.SUCCEEDED: frozenset(),
    NodeStatus.FAILED: frozenset(),
    NodeStatus.SKIPPED: frozenset(),
    NodeStatus.CANCELLED: frozenset(),
}


class InvalidWorkflowTransition(ValueError):
    pass


class InvalidNodeTransition(ValueError):
    pass


def can_transition_workflow(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    return target in ALLOWED_WORKFLOW_TRANSITIONS.get(current, frozenset())


def can_transition_node(current: NodeStatus, target: NodeStatus) -> bool:
    return target in ALLOWED_NODE_TRANSITIONS.get(current, frozenset())
