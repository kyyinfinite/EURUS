from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from eurus.agent.acceptance import AcceptancePolicy, AcceptanceService
from eurus.agent.engineering_manager import EngineeringManager
from eurus.multiagent.contracts import (
    MISSION_STATUSES,
    TERMINAL_MISSION_STATUSES,
    AcceptanceDecision,
    Mission,
)
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.task_graph import TaskGraphError, TaskGraphStore
from eurus.storage.database import Database, DatabasePath

OrchestratorFactory = Callable[[Database], AgentOrchestrator]

ROUTES = frozenset({"direct", "hierarchical"})

_HIERARCHICAL_HINTS = (
    "feature",
    "migrate",
    "refactor",
    "architecture",
    "multi-",
    "dashboard",
    "integration",
    "redesign",
    "pipeline",
)


class MissionStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def create(
        self,
        objective: str,
        *,
        owner_id: str,
        acceptance_criteria: Sequence[str] = (),
        budget: dict[str, Any] | None = None,
        root_run_id: str | None = None,
    ) -> Mission:
        if not objective.strip():
            raise ValueError("mission objective must not be empty")
        current = _now()
        mission = Mission(
            mission_id=uuid.uuid4().hex,
            owner_id=owner_id,
            objective=objective[:20000],
            status="created",
            root_run_id=root_run_id,
            acceptance_criteria=tuple(acceptance_criteria),
            budget=dict(budget or {}),
            created_at=current,
            updated_at=current,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_missions(
                    mission_id, owner_id, root_run_id, objective, status, plan_json,
                    acceptance_json, budget_json, version, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mission.mission_id,
                    mission.owner_id,
                    mission.root_run_id,
                    mission.objective,
                    mission.status,
                    json.dumps(mission.plan),
                    json.dumps(list(mission.acceptance_criteria)),
                    json.dumps(mission.budget),
                    mission.version,
                    _time(mission.created_at),
                    _time(mission.updated_at),
                    None,
                ),
            )
        return mission

    def get(self, mission_id: str, *, owner_id: str) -> Mission | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_agent_missions WHERE mission_id=? AND owner_id=?",
                (mission_id, owner_id),
            ).fetchone()
        return self._mission(row) if row is not None else None

    def list_missions(
        self, *, owner_id: str, statuses: Sequence[str] = (), limit: int = 50
    ) -> list[Mission]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT * FROM eurus_agent_missions WHERE owner_id=?"
        values: list[Any] = [owner_id]
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            values.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._mission(row) for row in rows]

    def update(
        self,
        mission_id: str,
        *,
        owner_id: str,
        status: str | None = None,
        plan: dict[str, Any] | None = None,
        version_check: int | None = None,
    ) -> Mission | None:
        if status is not None and status not in MISSION_STATUSES:
            raise ValueError(f"unknown mission status: {status!r}")
        current = self.get(mission_id, owner_id=owner_id)
        if current is None:
            return None
        if version_check is not None and version_check != current.version:
            return None
        updated_at = _now()
        status_value = status or current.status
        if current.status in TERMINAL_MISSION_STATUSES and status_value != current.status:
            raise ValueError(
                f"mission {mission_id} is terminal ({current.status}); "
                "only resume flows may reopen it explicitly"
            )
        completed_at = (
            current.completed_at
            if status_value not in TERMINAL_MISSION_STATUSES
            else (current.completed_at or updated_at)
        )
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE eurus_agent_missions SET status=?, plan_json=?, version=?,
                updated_at=?, completed_at=? WHERE mission_id=? AND owner_id=?""",
                (
                    status_value,
                    json.dumps(plan if plan is not None else current.plan),
                    current.version + 1,
                    _time(updated_at),
                    _time(completed_at) if completed_at else None,
                    mission_id,
                    owner_id,
                ),
            )
        return self.get(mission_id, owner_id=owner_id)

    def _mission(self, row: Any) -> Mission:
        completed = _parse(row["completed_at"])
        return Mission(
            mission_id=row["mission_id"],
            owner_id=row["owner_id"],
            objective=row["objective"],
            status=row["status"],
            root_run_id=row["root_run_id"],
            plan=_json_dict(row["plan_json"]),
            acceptance_criteria=tuple(_json_list(row["acceptance_json"])),
            budget=_json_dict(row["budget_json"]),
            version=int(row["version"]),
            created_at=_parse(row["created_at"]) or _now(),
            updated_at=_parse(row["updated_at"]) or _now(),
            completed_at=completed,
        )


class MainSupervisor:
    def __init__(
        self,
        database: Database | DatabasePath,
        orchestrator: AgentOrchestrator,
        *,
        acceptance_policy: AcceptancePolicy | None = None,
        engineering_manager: EngineeringManager | None = None,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.orchestrator = orchestrator
        self.missions = MissionStore(self.database)
        self.tasks = TaskGraphStore(self.database)
        self.acceptance = AcceptanceService(self.database, policy=acceptance_policy)
        self.engineering_manager = engineering_manager or EngineeringManager()

    def create_mission(
        self,
        objective: str,
        *,
        owner_id: str,
        acceptance_criteria: Sequence[str] = (),
        budget: dict[str, Any] | None = None,
        root_run_id: str | None = None,
    ) -> Mission:
        return self.missions.create(
            objective,
            owner_id=owner_id,
            acceptance_criteria=acceptance_criteria,
            budget=budget,
            root_run_id=root_run_id,
        )

    def plan_mission(
        self,
        mission_id: str,
        *,
        owner_id: str,
        tasks: Sequence[dict[str, Any]] | None = None,
    ) -> Mission:
        mission = self._require(mission_id, owner_id)
        if mission.status not in {"created", "planning", "planned"}:
            raise ValueError(f"mission {mission_id} in status {mission.status!r} cannot be planned")
        self.missions.update(mission_id, owner_id=owner_id, status="planning")
        from eurus.agent.task_decomposer import TaskDecomposer

        plan = TaskDecomposer().decompose(mission.objective, tasks=tasks, mission_id=mission_id)
        decision = self.engineering_manager.review_plan(plan)
        if not decision.approved:
            self.missions.update(mission_id, owner_id=owner_id, status="created")
            raise ValueError(
                f"engineering review rejected the plan for mission {mission_id}: "
                + "; ".join(decision.issues)
            )
        try:
            for spec in plan.tasks:
                self.tasks.add_task(spec, owner_id=owner_id)
        except TaskGraphError:
            self.missions.update(mission_id, owner_id=owner_id, status="created")
            raise
        updated = self.missions.update(
            mission_id,
            owner_id=owner_id,
            status="planned",
            plan={
                "route": plan.route,
                "task_count": len(plan.tasks),
                "review_warnings": list(decision.warnings[:10]),
            },
        )
        assert updated is not None
        return updated

    def start_mission(self, mission_id: str, *, owner_id: str) -> Mission:
        mission = self._require(mission_id, owner_id)
        if mission.status not in {"planned", "running", "blocked", "awaiting_approval"}:
            raise ValueError(
                f"mission {mission_id} in status {mission.status!r} cannot start; plan it first"
            )
        started = self.missions.update(mission_id, owner_id=owner_id, status="running")
        assert started is not None
        self.tasks.promote_ready(mission_id, owner_id=owner_id)
        return started

    def cancel_mission(self, mission_id: str, *, owner_id: str, reason: str = "") -> Mission:
        mission = self._require(mission_id, owner_id)
        if mission.status in TERMINAL_MISSION_STATUSES:
            return mission
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status in {"succeeded", "failed", "cancelled", "skipped"}:
                continue
            if task.assignment_id and task.status != "pending":
                self.orchestrator.cancel(task.assignment_id, owner_id=owner_id)
            try:
                self.tasks.update_status(task.task_id, "cancelled", owner_id=owner_id)
            except KeyError:
                continue
        cancelled = self.missions.update(mission_id, owner_id=owner_id, status="cancelled")
        assert cancelled is not None
        return cancelled

    def resume_mission(self, mission_id: str, *, owner_id: str) -> Mission:
        mission = self._require(mission_id, owner_id)
        if mission.status not in TERMINAL_MISSION_STATUSES and mission.status != "running":
            raise ValueError(f"mission {mission_id} in status {mission.status!r} cannot resume")
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status in {"dispatched", "running", "review_pending"}:
                assignment = (
                    self.orchestrator.store.get(task.assignment_id) if task.assignment_id else None
                )
                if assignment is None or assignment.is_terminal:
                    self.tasks.update_status(
                        task.task_id,
                        "ready",
                        owner_id=owner_id,
                        clear_assignment=True,
                    )
        resumed = self.missions.update(mission_id, owner_id=owner_id, status="running")
        assert resumed is not None
        self.tasks.promote_ready(mission_id, owner_id=owner_id)
        return resumed

    def dispatch_ready(self, mission_id: str, *, owner_id: str, limit: int = 10) -> list[str]:
        self._require(mission_id, owner_id)
        self.tasks.promote_ready(mission_id, owner_id=owner_id)
        dispatched: list[str] = []
        from eurus.agent.team_coordinator import TeamCoordinator

        coordinator = TeamCoordinator(self.database, self.orchestrator)
        for task in self.tasks.ready_tasks(mission_id, owner_id=owner_id, limit=limit):
            assignment = coordinator.dispatch_task(task, owner_id=owner_id, mission_id=mission_id)
            if assignment is not None:
                dispatched.append(assignment.assignment_id)
        return dispatched

    def on_task_succeeded(self, mission_id: str, task_id: str, *, owner_id: str) -> None:
        self._require(mission_id, owner_id)
        for dependent_id in self.tasks.dependents_of(task_id):
            dependent = self.tasks.get(dependent_id, owner_id=owner_id)
            if dependent is None or dependent.status != "blocked":
                continue
            self.tasks.update_status(dependent_id, "pending", owner_id=owner_id)
        self.tasks.promote_ready(mission_id, owner_id=owner_id)

    def on_task_failed(self, mission_id: str, task_id: str, *, owner_id: str) -> int:
        self._require(mission_id, owner_id)
        blocked = 0
        for dependent_id in self.tasks.dependents_of(task_id):
            dependent = self.tasks.get(dependent_id, owner_id=owner_id)
            if dependent is None or dependent.status in {
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
            }:
                continue
            self.tasks.update_status(dependent_id, "blocked", owner_id=owner_id)
            blocked += 1
        return blocked

    def record_verification(
        self,
        mission_id: str,
        task_id: str,
        *,
        owner_id: str,
        check_name: str,
        status: str,
        category: str = "test",
        command_argv: Sequence[str] = (),
        exit_code: int | None = None,
        duration_ms: int | None = None,
        working_directory: str | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        artifact_id: str | None = None,
        environment: dict[str, Any] | None = None,
    ) -> str:
        self._require(mission_id, owner_id)
        return self.acceptance.record_verification(
            mission_id=mission_id,
            task_id=task_id,
            owner_id=owner_id,
            check_name=check_name,
            status=status,
            category=category,
            command_argv=command_argv,
            exit_code=exit_code,
            duration_ms=duration_ms,
            working_directory=working_directory,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            artifact_id=artifact_id,
            environment=environment,
        )

    def finalize_mission(
        self, mission_id: str, *, owner_id: str, require_checks: Sequence[str] = ()
    ) -> AcceptanceDecision:
        self._require(mission_id, owner_id)
        decision = self.acceptance.evaluate(
            mission_id, owner_id=owner_id, required_checks=require_checks
        )
        status = "completed" if decision.verified else decision.decision
        self.missions.update(mission_id, owner_id=owner_id, status=status)
        return decision

    def mission_report(self, mission_id: str, *, owner_id: str) -> dict[str, Any]:
        mission = self._require(mission_id, owner_id)
        tasks = self.tasks.list_tasks(mission_id, owner_id=owner_id)
        assignments = self.orchestrator.store.list_assignments(
            owner_id=owner_id, mission_id=mission_id, limit=500
        )
        report = {
            "total": len(assignments),
            "succeeded": sum(1 for item in assignments if item.status == "succeeded"),
            "failed": sum(1 for item in assignments if item.status == "failed"),
            "pending": sum(
                1 for item in assignments if item.status in ("pending", "dispatched", "running")
            ),
            "cancelled": sum(1 for item in assignments if item.status == "cancelled"),
        }
        report["complete"] = (
            report["total"] > 0
            and report["pending"] == 0
            and report["failed"] == 0
            and report["cancelled"] == 0
        )
        return {
            "mission": mission.to_dict(),
            "tasks": [task.to_dict() for task in tasks],
            "assignments": [
                {
                    "assignment_id": item.assignment_id,
                    "role": item.role,
                    "status": item.status,
                    "task_id": item.task_id,
                }
                for item in assignments
            ],
            "aggregation": report,
        }

    def decide_route(self, objective: str, *, explicit: str | None = None) -> str:
        if explicit is not None:
            if explicit not in ROUTES:
                raise ValueError(f"unknown route: {explicit!r}")
            return explicit
        lowered = objective.lower()
        if any(hint in lowered for hint in _HIERARCHICAL_HINTS):
            return "hierarchical"
        if lowered.count(" and ") >= 2 or len(lowered) > 400:
            return "hierarchical"
        return "direct"

    def _require(self, mission_id: str, owner_id: str) -> Mission:
        mission = self.missions.get(mission_id, owner_id=owner_id)
        if mission is None:
            raise KeyError(f"mission {mission_id!r} not found for this owner")
        return mission


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
