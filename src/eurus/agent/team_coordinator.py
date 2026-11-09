from __future__ import annotations

import logging
import re

from eurus.agent.engineering_manager import EngineeringManager
from eurus.multiagent.contracts import TaskSpec
from eurus.multiagent.orchestrator import AgentOrchestrator, Assignment
from eurus.multiagent.registry import SpecialistRegistry
from eurus.storage.database import Database, DatabasePath

log = logging.getLogger(__name__)


FALLBACK_ROLE_PREFERENCE = ("analyst", "researcher", "writer", "coder", "reviewer")
_PATH_MENTION = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")


def objective_paths(objective: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_PATH_MENTION.findall(objective[:4000])))


class TeamCoordinator:
    def __init__(
        self,
        database: Database | DatabasePath,
        orchestrator: AgentOrchestrator,
        *,
        engineering_manager: EngineeringManager | None = None,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.orchestrator = orchestrator
        self.registry = SpecialistRegistry(self.database)
        self.engineering_manager = engineering_manager or EngineeringManager()

    def select_role(self, task: TaskSpec) -> str | None:
        if task.preferred_role:
            specialist = self.registry.get(task.preferred_role)
            if specialist is None or not specialist.enabled:
                raise ValueError(
                    f"task {task.task_key!r} prefers role {task.preferred_role!r} "
                    "which is not available"
                )
            return specialist.role
        for capability in task.required_capabilities:
            matches = self.registry.select(capability=capability)
            if matches:
                return matches[0].role
        for role in FALLBACK_ROLE_PREFERENCE:
            if self.registry.select(role=role):
                log.info(
                    "task %s (%s) has no matching specialist role; falling back to %s",
                    task.task_id,
                    task.task_key,
                    role,
                )
                return role
        return None

    def _conflicts_with_running_work(
        self, task: TaskSpec, *, owner_id: str, mission_id: str
    ) -> bool:
        candidate_paths = objective_paths(task.objective)
        if not candidate_paths:
            return False
        running = self.orchestrator.store.list_assignments(
            owner_id=owner_id, mission_id=mission_id, limit=200
        )
        changed: dict[str, tuple[str, ...] | list[str]] = {
            assignment.assignment_id: objective_paths(assignment.objective)
            for assignment in running
            if assignment.status in ("pending", "dispatched", "running")
        }
        changed[task.task_id] = candidate_paths
        plan = self.engineering_manager.route_conflicts(changed)
        return any(task.task_id in group.assignments for group in plan.groups)

    def dispatch_task(self, task: TaskSpec, *, owner_id: str, mission_id: str) -> Assignment | None:
        if self._conflicts_with_running_work(task, owner_id=owner_id, mission_id=mission_id):
            log.info(
                "task %s (%s) touches paths owned by a running assignment; "
                "deferring dispatch to serialize writes",
                task.task_id,
                task.task_key,
            )
            return None
        role = self.select_role(task)
        if role is None:
            log.warning(
                "task %s (%s) has no matching specialist role; leaving it ready",
                task.task_id,
                task.task_key,
            )
            return None
        try:
            assignment = self.orchestrator.dispatch(
                task.objective,
                role=role,
                owner_id=owner_id,
                mission_id=mission_id,
                task_id=task.task_id,
                correlation_id=mission_id,
            )
        except RuntimeError as exc:
            log.warning("task %s dispatch deferred: %s", task.task_key, exc)
            return None
        self._mark_task(task, assignment, owner_id=owner_id)
        return assignment

    def _mark_task(self, task: TaskSpec, assignment: Assignment, *, owner_id: str) -> None:
        from eurus.multiagent.task_graph import TaskGraphStore

        store = TaskGraphStore(self.database)
        store.update_status(
            task.task_id,
            "dispatched",
            owner_id=owner_id,
            assignment_id=assignment.assignment_id,
            increment_attempt=True,
        )
