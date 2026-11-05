from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class GoalStatus(StrEnum):
    CREATED = "created"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    ACTIVE = "active"
    EXECUTING = "executing"
    WAITING = "waiting"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"
    EVALUATING = "evaluating"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MilestoneStatus(StrEnum):
    PENDING = "pending"
    REACHED = "reached"
    MISSED = "missed"


ALLOWED_GOAL_TRANSITIONS: Mapping[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.CREATED: frozenset(
        {
            GoalStatus.ANALYZING,
            GoalStatus.PLANNING,
            GoalStatus.WAITING,
            GoalStatus.CANCELLED,
            GoalStatus.FAILED,
        }
    ),
    GoalStatus.ANALYZING: frozenset(
        {GoalStatus.PLANNING, GoalStatus.WAITING, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.PLANNING: frozenset(
        {GoalStatus.ACTIVE, GoalStatus.WAITING, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.ACTIVE: frozenset(
        {
            GoalStatus.EXECUTING,
            GoalStatus.WAITING,
            GoalStatus.BLOCKED,
            GoalStatus.NEEDS_APPROVAL,
            GoalStatus.EVALUATING,
            GoalStatus.REPLANNING,
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.EXECUTING: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.WAITING,
            GoalStatus.BLOCKED,
            GoalStatus.NEEDS_APPROVAL,
            GoalStatus.EVALUATING,
            GoalStatus.REPLANNING,
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.WAITING: frozenset(
        {GoalStatus.ACTIVE, GoalStatus.EXECUTING, GoalStatus.FAILED, GoalStatus.CANCELLED}
    ),
    GoalStatus.BLOCKED: frozenset(
        {
            GoalStatus.REPLANNING,
            GoalStatus.ACTIVE,
            GoalStatus.WAITING,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.NEEDS_APPROVAL: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.EXECUTING,
            GoalStatus.WAITING,
            GoalStatus.CANCELLED,
            GoalStatus.FAILED,
        }
    ),
    GoalStatus.EVALUATING: frozenset(
        {
            GoalStatus.COMPLETED,
            GoalStatus.FAILED,
            GoalStatus.REPLANNING,
            GoalStatus.ACTIVE,
            GoalStatus.WAITING,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.REPLANNING: frozenset(
        {
            GoalStatus.PLANNING,
            GoalStatus.ACTIVE,
            GoalStatus.WAITING,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}

TERMINAL_GOAL_STATUSES = frozenset({GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED})


class InvalidGoalTransition(ValueError):
    pass


def can_transition(current: GoalStatus, target: GoalStatus) -> bool:
    return target in ALLOWED_GOAL_TRANSITIONS.get(current, frozenset())
