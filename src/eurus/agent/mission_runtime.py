from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor
from eurus.multiagent.contracts import Mission
from eurus.multiagent.orchestrator import OrchestratorEventPublisher
from eurus.multiagent.supervision import MissionSupervision
from eurus.owner import OWNER_ID

log = logging.getLogger(__name__)

ACTIVE_TASK_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "dispatched",
        "running",
        "review_pending",
        "revision_required",
        "reassigned",
    }
)
PROGRESSION_MISSION_STATUSES = ("running",)
DEADLINE_KEYS = ("deadline", "deadline_at")
MIN_DISPATCH_BATCH = 1
MAX_DISPATCH_BATCH = 100
MIN_MISSIONS_PER_TICK = 1
MAX_MISSIONS_PER_TICK = 500
MIN_TASK_ATTEMPTS = 1
MAX_TASK_ATTEMPTS = 10


@dataclass(slots=True)
class MissionProgressionReport:
    missions_checked: int = 0
    dispatched: list[str] = field(default_factory=list)
    tasks_succeeded: list[str] = field(default_factory=list)
    tasks_blocked: list[str] = field(default_factory=list)
    finalized: list[dict[str, Any]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "missions_checked": self.missions_checked,
            "dispatched": list(self.dispatched),
            "tasks_succeeded": list(self.tasks_succeeded),
            "tasks_blocked": list(self.tasks_blocked),
            "finalized": [dict(item) for item in self.finalized],
            "expired": list(self.expired),
            "exhausted": list(self.exhausted),
            "notes": list(self.notes),
        }


def deadline_from_budget(budget: Mapping[str, Any]) -> datetime | None:
    for key in DEADLINE_KEYS:
        value = budget.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class MissionProgression:
    def __init__(
        self,
        supervisor: MainSupervisor,
        *,
        review: ReviewLoop | None = None,
        stale_after_seconds: float = 900.0,
        dispatch_batch_limit: int = 5,
        max_missions_per_tick: int = 20,
        max_task_attempts: int = 3,
        hierarchical_dispatch: bool = True,
        event_publisher: OrchestratorEventPublisher | None = None,
    ) -> None:
        if not MIN_DISPATCH_BATCH <= dispatch_batch_limit <= MAX_DISPATCH_BATCH:
            raise ValueError("dispatch_batch_limit must be between 1 and 100")
        if not MIN_MISSIONS_PER_TICK <= max_missions_per_tick <= MAX_MISSIONS_PER_TICK:
            raise ValueError("max_missions_per_tick must be between 1 and 500")
        if not MIN_TASK_ATTEMPTS <= max_task_attempts <= MAX_TASK_ATTEMPTS:
            raise ValueError("max_task_attempts must be between 1 and 10")
        self.supervisor = supervisor
        self.hierarchical_dispatch = hierarchical_dispatch
        self.dispatch_batch_limit = dispatch_batch_limit
        self.max_missions_per_tick = max_missions_per_tick
        self.max_task_attempts = max_task_attempts
        self.event_publisher = event_publisher
        self.supervision = MissionSupervision(
            supervisor.database,
            supervisor.orchestrator,
            stale_after_seconds=stale_after_seconds,
            review=review,
        )

    def stats(self, *, owner_id: str | None = None) -> dict[str, int]:
        owner = owner_id or OWNER_ID
        missions = self.supervisor.missions.list_missions(
            owner_id=owner,
            statuses=PROGRESSION_MISSION_STATUSES,
            limit=self.max_missions_per_tick,
        )
        active_tasks = 0
        for mission in missions:
            for task in self.supervisor.tasks.list_tasks(mission.mission_id, owner_id=owner):
                if task.status in ACTIVE_TASK_STATUSES:
                    active_tasks += 1
        return {"running": len(missions), "active_tasks": active_tasks}

    def tick(
        self, *, owner_id: str | None = None, now: datetime | None = None
    ) -> MissionProgressionReport:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        owner = owner_id or OWNER_ID
        report = MissionProgressionReport()
        missions = self.supervisor.missions.list_missions(
            owner_id=owner,
            statuses=PROGRESSION_MISSION_STATUSES,
            limit=self.max_missions_per_tick,
        )
        report.missions_checked = len(missions)
        for mission in missions:
            try:
                self._tick_mission(mission, report, owner_id=owner, current=current)
            except Exception as exc:
                log.exception(
                    "mission progression failed for %s (%s)",
                    mission.mission_id,
                    type(exc).__name__,
                )
                report.notes.append(
                    f"mission {mission.mission_id[:8]} progression failed: "
                    f"{type(exc).__name__}: {exc}"[:400]
                )
        return report

    def _tick_mission(
        self,
        mission: Mission,
        report: MissionProgressionReport,
        *,
        owner_id: str,
        current: datetime,
    ) -> None:
        mission_id = mission.mission_id
        tasks = self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id)
        before = {task.task_id: task.status for task in tasks}
        deadline = deadline_from_budget(mission.budget)
        supervision = self.supervision.tick(
            mission_id, owner_id=owner_id, deadline=deadline if deadline else None
        )
        if supervision.deadline_expired:
            self._expire_mission(mission_id, owner_id=owner_id, deadline=deadline, report=report)
            return
        tasks = self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id)
        for task in tasks:
            previous = before.get(task.task_id)
            if task.status == "succeeded" and previous != "succeeded":
                self.supervisor.on_task_succeeded(mission_id, task.task_id, owner_id=owner_id)
                report.tasks_succeeded.append(task.task_id)
            elif task.status in {"failed", "blocked"} and previous not in {"failed", "blocked"}:
                self.supervisor.on_task_failed(mission_id, task.task_id, owner_id=owner_id)
                report.tasks_blocked.append(task.task_id)
        tasks = self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id)
        for task in tasks:
            if task.status == "ready" and task.attempt >= self.max_task_attempts:
                self.supervisor.tasks.update_status(
                    task.task_id, "failed", owner_id=owner_id, clear_assignment=True
                )
                self.supervisor.on_task_failed(mission_id, task.task_id, owner_id=owner_id)
                report.exhausted.append(task.task_id)
                report.notes.append(
                    f"mission {mission_id[:8]} task {task.task_key!r} exhausted "
                    f"{task.attempt} attempt(s); marked failed"
                )
        tasks = self.supervisor.tasks.list_tasks(mission_id, owner_id=owner_id)
        if all(task.status not in ACTIVE_TASK_STATUSES for task in tasks):
            self._finalize_mission(mission_id, owner_id=owner_id, report=report)
            return
        if not self.hierarchical_dispatch:
            return
        dispatched = self.supervisor.dispatch_ready(
            mission_id, owner_id=owner_id, limit=self.dispatch_batch_limit
        )
        if dispatched:
            report.dispatched.extend(dispatched)
            report.notes.append(f"mission {mission_id[:8]} dispatched {len(dispatched)} task(s)")

    def _expire_mission(
        self,
        mission_id: str,
        *,
        owner_id: str,
        deadline: datetime | None,
        report: MissionProgressionReport,
    ) -> None:
        self.supervisor.missions.update(mission_id, owner_id=owner_id, status="cancelled")
        report.expired.append(mission_id)
        report.notes.append(f"mission {mission_id[:8]} deadline expired; cancelled")
        self._publish(
            "MISSION_EXPIRED",
            mission_id,
            {"deadline": deadline.isoformat() if deadline else None},
        )

    def _finalize_mission(
        self, mission_id: str, *, owner_id: str, report: MissionProgressionReport
    ) -> None:
        decision = self.supervisor.finalize_mission(mission_id, owner_id=owner_id)
        entry = {
            "mission_id": mission_id,
            "decision": decision.decision,
            "verified": decision.verified,
        }
        report.finalized.append(entry)
        report.notes.append(f"mission {mission_id[:8]} finalized as {decision.decision}")
        self._publish("MISSION_FINALIZED", mission_id, {"decision": decision.decision})

    def _publish(self, event_type: str, mission_id: str, payload: dict[str, Any]) -> None:
        if self.event_publisher is None:
            return
        try:
            self.event_publisher(event_type, "mission", mission_id, payload)
        except Exception:
            log.exception("mission progression event publication failed")


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "MissionProgression",
    "MissionProgressionReport",
    "deadline_from_budget",
]
