from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from eurus.office.errors import OfficeBudgetError
from eurus.office.models.task import AgentRole

ROLE_BUDGETS: dict[AgentRole, int] = {
    AgentRole.CLIENT: 6,
    AgentRole.RESEARCHER: 8,
    AgentRole.DESIGNER: 5,
    AgentRole.PRODUCER: 10,
    AgentRole.CRITIC: 6,
    AgentRole.VISUAL_QA: 4,
}


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_total_model_calls: int = 60
    max_revision_cycles: int = 3
    max_workers: int = 3
    deadline_seconds: float | None = None
    role_budgets: dict[AgentRole, int] = field(default_factory=lambda: dict(ROLE_BUDGETS))

    def __post_init__(self) -> None:
        if self.max_total_model_calls < 1:
            raise OfficeBudgetError("max_total_model_calls must be at least 1")
        if not 0 <= self.max_revision_cycles <= 10:
            raise OfficeBudgetError("max_revision_cycles must be between 0 and 10")
        if self.max_workers < 1:
            raise OfficeBudgetError("max_workers must be at least 1")
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise OfficeBudgetError("deadline_seconds must be positive")


class BudgetTracker:
    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.started = time.monotonic()
        self._role_calls: dict[AgentRole, int] = {role: 0 for role in AgentRole}
        self.total_calls = 0

    def ensure_deadline(self) -> None:
        deadline = self.limits.deadline_seconds
        if deadline is None:
            return
        elapsed = time.monotonic() - self.started
        if elapsed > deadline:
            raise OfficeBudgetError(
                f"office job exceeded its {deadline:g}s deadline after {elapsed:.1f}s"
            )

    def start_role_call(self, role: AgentRole) -> None:
        self.ensure_deadline()
        if self._role_calls[role] >= self.limits.role_budgets.get(role, 4):
            raise OfficeBudgetError(f"agent role {role.value} exhausted its model call budget")
        if self.total_calls >= self.limits.max_total_model_calls:
            raise OfficeBudgetError(
                f"office job reached the total model call budget of "
                f"{self.limits.max_total_model_calls}"
            )
        self._role_calls[role] += 1
        self.total_calls += 1

    def calls_for(self, role: AgentRole) -> int:
        return self._role_calls[role]

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining_seconds(self) -> float | None:
        if self.limits.deadline_seconds is None:
            return None
        return max(0.0, self.limits.deadline_seconds - self.elapsed())

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_model_calls": self.total_calls,
            "max_total_model_calls": self.limits.max_total_model_calls,
            "role_calls": {role.value: calls for role, calls in self._role_calls.items()},
            "elapsed_seconds": round(self.elapsed(), 2),
            "deadline_seconds": self.limits.deadline_seconds,
        }
