from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from eurus.automation.engine import AutomationEngine
from eurus.multiagent.communication import AssignmentMailbox, MessageStore
from eurus.multiagent.registry import Specialist, SpecialistRegistry
from eurus.storage.database import Database, DatabasePath
from eurus.storage.jobs import Job

log = logging.getLogger(__name__)

ASSIGNMENT_STATUSES = frozenset(
    {"pending", "dispatched", "running", "succeeded", "failed", "cancelled", "reassigned"}
)
TERMINAL_ASSIGNMENT_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
MAX_ASSIGNMENTS_PER_PARENT = 64


class OrchestratorEventPublisher(Protocol):
    def __call__(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, Any],
    ) -> None: ...


@dataclass(slots=True)
class Assignment:
    assignment_id: str
    specialist_id: str
    role: str
    objective: str
    status: str
    created_at: datetime
    updated_at: datetime
    parent_run_id: str | None = None
    goal_id: str | None = None
    workflow_instance_id: str | None = None
    node_id: str | None = None
    child_run_id: str | None = None
    job_id: str | None = None
    result: str = ""
    error: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    owner_id: str = "local"
    delegation_depth: int = 0
    mission_id: str | None = None
    task_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ASSIGNMENT_STATUSES


@dataclass(slots=True)
class AggregationReport:
    parent_run_id: str
    total: int
    succeeded: int
    failed: int
    pending: int
    cancelled: int
    results: list[Mapping[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    complete: bool = False


class AssignmentStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def create(self, assignment: Assignment) -> Assignment:
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_assignments(
                    assignment_id, parent_run_id, goal_id, workflow_instance_id, node_id,
                    specialist_id, role, objective, status, child_run_id, job_id, result,
                    error, attempts, created_at, started_at, finished_at, updated_at,
                    owner_id, delegation_depth, mission_id, task_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    assignment.assignment_id,
                    assignment.parent_run_id,
                    assignment.goal_id,
                    assignment.workflow_instance_id,
                    assignment.node_id,
                    assignment.specialist_id,
                    assignment.role,
                    assignment.objective,
                    assignment.status,
                    assignment.child_run_id,
                    assignment.job_id,
                    assignment.result,
                    assignment.error,
                    assignment.attempts,
                    _time(assignment.created_at),
                    _time(assignment.started_at) if assignment.started_at else None,
                    _time(assignment.finished_at) if assignment.finished_at else None,
                    _time(assignment.updated_at),
                    assignment.owner_id,
                    assignment.delegation_depth,
                    assignment.mission_id,
                    assignment.task_id,
                ),
            )
        return assignment

    def get(self, assignment_id: str) -> Assignment | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_agent_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
        return self._assignment(row) if row is not None else None

    def get_scoped(self, assignment_id: str, *, owner_id: str) -> Assignment | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_agent_assignments WHERE assignment_id=? AND owner_id=?",
                (assignment_id, owner_id),
            ).fetchone()
        return self._assignment(row) if row is not None else None

    def list_assignments(
        self,
        *,
        owner_id: str | None = None,
        parent_run_id: str | None = None,
        workflow_instance_id: str | None = None,
        goal_id: str | None = None,
        role: str | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
        statuses: Sequence[str] = (),
        limit: int = 100,
    ) -> list[Assignment]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        values: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        if parent_run_id is not None:
            clauses.append("parent_run_id=?")
            values.append(parent_run_id)
        if workflow_instance_id is not None:
            clauses.append("workflow_instance_id=?")
            values.append(workflow_instance_id)
        if goal_id is not None:
            clauses.append("goal_id=?")
            values.append(goal_id)
        if role is not None:
            clauses.append("role=?")
            values.append(role)
        if mission_id is not None:
            clauses.append("mission_id=?")
            values.append(mission_id)
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_agent_assignments{where} ORDER BY created_at LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._assignment(row) for row in rows]

    def update(
        self,
        assignment_id: str,
        *,
        status: str | None = None,
        result: str | None = None,
        error: str | None = None,
        job_id: str | None = None,
        child_run_id: str | None = None,
        increment_attempts: bool = False,
        owner_id: str | None = None,
    ) -> Assignment | None:
        current = self.get(assignment_id)
        if current is None:
            return None
        if owner_id is not None and current.owner_id != owner_id:
            return None
        if status is not None and status not in ASSIGNMENT_STATUSES:
            raise ValueError(f"unknown assignment status: {status}")
        assignments: dict[str, Any] = {"updated_at": _time(_now())}
        if status is not None:
            assignments["status"] = status
            if status in TERMINAL_ASSIGNMENT_STATUSES:
                assignments["finished_at"] = assignments["updated_at"]
            elif status == "running" and current.started_at is None:
                assignments["started_at"] = assignments["updated_at"]
        if result is not None:
            assignments["result"] = result[:20000]
        if error is not None:
            assignments["error"] = error[:4000]
        if job_id is not None:
            assignments["job_id"] = job_id
        if child_run_id is not None:
            assignments["child_run_id"] = child_run_id
        if increment_attempts:
            assignments["attempts"] = current.attempts + 1
        with self.database.connection() as connection:
            connection.execute(
                f"UPDATE eurus_agent_assignments SET "
                f"{', '.join(f'{key}=?' for key in assignments)} "
                f"WHERE assignment_id=? AND owner_id=?",
                (*assignments.values(), assignment_id, current.owner_id),
            )
        return self.get(assignment_id)

    def heartbeat(
        self, assignment_id: str, detail: str = "", *, owner_id: str | None = None
    ) -> bool:
        if owner_id is not None:
            owner_row: sqlite3.Row | None
            with self.database.connection() as connection:
                owner_row = connection.execute(
                    "SELECT owner_id FROM eurus_agent_assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()
            if owner_row is None or str(owner_row["owner_id"]) != owner_id:
                return False
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_heartbeats(assignment_id, last_beat, detail)
                VALUES (?, ?, ?)
                ON CONFLICT(assignment_id) DO UPDATE SET
                    last_beat=excluded.last_beat, detail=excluded.detail""",
                (assignment_id, _time(_now()), detail[:2000]),
            )
            return True

    def last_heartbeat(self, assignment_id: str) -> datetime | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT last_beat FROM eurus_agent_heartbeats WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
        if row is None:
            return None
        text = str(row["last_beat"])
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def stale_running(
        self, *, older_than_seconds: float, owner_id: str | None = None, limit: int = 100
    ) -> list[Assignment]:
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        running = self.list_assignments(
            owner_id=owner_id, statuses=("running", "dispatched", "pending"), limit=500
        )
        stale: list[Assignment] = []
        for assignment in running:
            beat = self.last_heartbeat(assignment.assignment_id)
            reference = beat or assignment.updated_at
            if reference.timestamp() < cutoff:
                stale.append(assignment)
            if len(stale) >= limit:
                break
        return stale

    def _assignment(self, row: Any) -> Assignment:
        return Assignment(
            assignment_id=row["assignment_id"],
            parent_run_id=row["parent_run_id"],
            goal_id=row["goal_id"],
            workflow_instance_id=row["workflow_instance_id"],
            node_id=row["node_id"],
            specialist_id=row["specialist_id"],
            role=row["role"],
            objective=row["objective"],
            status=row["status"],
            child_run_id=row["child_run_id"],
            job_id=row["job_id"],
            result=row["result"] or "",
            error=row["error"],
            attempts=int(row["attempts"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            owner_id=row["owner_id"] or "local",
            delegation_depth=int(row["delegation_depth"] or 0),
            mission_id=row["mission_id"] if "mission_id" in set(row.keys()) else None,
            task_id=row["task_id"] if "task_id" in set(row.keys()) else None,
        )


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
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class AgentOrchestrator:
    def __init__(
        self,
        database: Database | DatabasePath,
        automation: AutomationEngine,
        *,
        event_publisher: OrchestratorEventPublisher | None = None,
        max_delegation_depth: int = 3,
        max_assignments_per_parent: int = MAX_ASSIGNMENTS_PER_PARENT,
        max_reassignments: int = 2,
    ) -> None:
        if not 0 <= max_delegation_depth <= 8:
            raise ValueError("max_delegation_depth must be between 0 and 8")
        if not 1 <= max_assignments_per_parent <= 256:
            raise ValueError("max_assignments_per_parent must be between 1 and 256")
        if not 0 <= max_reassignments <= 10:
            raise ValueError("max_reassignments must be between 0 and 10")
        self.registry = SpecialistRegistry(database)
        self.messages = MessageStore(database)
        self.mailbox = AssignmentMailbox(self.messages)
        self.store = AssignmentStore(database)
        self.automation = automation
        self.event_publisher = event_publisher
        self.max_delegation_depth = max_delegation_depth
        self.max_assignments_per_parent = max_assignments_per_parent
        self.max_reassignments = max_reassignments
        automation.on_job_finished.append(self._on_job_finished)

    def dispatch(
        self,
        objective: str,
        *,
        role: str,
        owner_id: str,
        parent_run_id: str | None = None,
        goal_id: str | None = None,
        workflow_instance_id: str | None = None,
        node_id: str | None = None,
        correlation_id: str = "",
        delegation_depth: int = 0,
        mission_id: str | None = None,
        task_id: str | None = None,
    ) -> Assignment:
        specialist = self.registry.get(role)
        if specialist is None:
            raise KeyError(f"specialist role {role!r} is not registered")
        if not specialist.enabled:
            raise ValueError(f"specialist role {role!r} is disabled")
        if not specialist.allowed_tools:
            log.warning(
                "specialist role %r has an empty tool allowlist; it will run with zero tools",
                role,
            )
        if delegation_depth > 0 and not specialist.delegation_allowed:
            raise PermissionError(f"specialist role {role!r} may not receive nested delegations")
        if delegation_depth > self.max_delegation_depth:
            raise PermissionError(
                f"delegation depth {delegation_depth} exceeds limit {self.max_delegation_depth}"
            )
        if delegation_depth > specialist.max_delegation_depth:
            raise PermissionError(
                f"specialist role {role!r} accepts at most {specialist.max_delegation_depth} "
                "delegation levels"
            )
        if parent_run_id is not None:
            existing = self.store.list_assignments(
                owner_id=owner_id, parent_run_id=parent_run_id, limit=500
            )
            if len(existing) >= self.max_assignments_per_parent:
                raise RuntimeError(f"parent {parent_run_id} reached its assignment limit")
        if not self._has_capacity(specialist, owner_id):
            raise RuntimeError(f"specialist role {role!r} has no free capacity")
        import uuid

        current = _now()
        assignment = Assignment(
            assignment_id=uuid.uuid4().hex,
            specialist_id=specialist.specialist_id,
            role=specialist.role,
            objective=objective,
            status="pending",
            created_at=current,
            updated_at=current,
            parent_run_id=parent_run_id,
            goal_id=goal_id,
            workflow_instance_id=workflow_instance_id,
            node_id=node_id,
            owner_id=owner_id,
            delegation_depth=delegation_depth,
            mission_id=mission_id,
            task_id=task_id,
        )
        assignment = self.store.create(assignment)
        self.mailbox.task_request(
            assignment.assignment_id, objective, parent=parent_run_id or owner_id
        )
        payload: dict[str, Any] = {
            "kind": "specialist_assignment",
            "assignment_id": assignment.assignment_id,
            "role": specialist.role,
            "mode": "specialist",
            "allowed_tools": list(specialist.allowed_tools),
            "tool_policy_version": specialist.tool_policy_version,
            "profile_version": specialist.profile_version,
            "max_steps": specialist.max_iterations,
            "max_runtime_seconds": specialist.max_runtime_seconds,
            "delegation_depth": delegation_depth,
            "model_policy": specialist.model_policy,
            "success_criteria": f"fulfill the {specialist.role} specialist assignment",
        }
        if specialist.allowed_tools:
            payload["allowed_tools"] = list(specialist.allowed_tools)
        if mission_id:
            payload["mission_id"] = mission_id
        if task_id:
            payload["task_id"] = task_id
        if goal_id:
            payload["goal_id"] = goal_id
        if workflow_instance_id:
            payload["workflow_instance_id"] = workflow_instance_id
        job = self.automation.submit(
            owner_id,
            objective,
            payload=payload,
            source="specialist",
            priority=specialist.priority,
            max_attempts=1,
            correlation_id=correlation_id or assignment.assignment_id,
        )
        updated = self.store.update(
            assignment.assignment_id,
            status="dispatched",
            job_id=job.job_id,
            child_run_id=job.job_id,
            increment_attempts=True,
        )
        assert updated is not None
        self.store.heartbeat(assignment.assignment_id, "dispatched", owner_id=owner_id)
        self._publish(
            "AGENT_STARTED",
            "assignment",
            assignment.assignment_id,
            {
                "role": specialist.role,
                "job_id": job.job_id,
                "parent_run_id": parent_run_id or "",
            },
        )
        return updated

    def dispatch_parallel(
        self,
        objectives: Sequence[Mapping[str, Any]],
        *,
        owner_id: str,
        parent_run_id: str | None = None,
        goal_id: str | None = None,
    ) -> list[Assignment]:
        assignments: list[Assignment] = []
        for entry in objectives:
            assignments.append(
                self.dispatch(
                    str(entry["objective"]),
                    role=str(entry["role"]),
                    owner_id=owner_id,
                    parent_run_id=parent_run_id,
                    goal_id=goal_id,
                    delegation_depth=int(entry.get("delegation_depth", 0)),
                )
            )
        return assignments

    def cancel(self, assignment_id: str, *, owner_id: str | None = None) -> Assignment | None:
        assignment = (
            self.store.get_scoped(assignment_id, owner_id=owner_id)
            if owner_id is not None
            else self.store.get(assignment_id)
        )
        if assignment is None:
            return None
        if assignment.is_terminal:
            return assignment
        if assignment.job_id:
            self.automation.jobs.request_cancel(assignment.job_id, owner_id=assignment.owner_id)
        self.mailbox.cancellation(assignment_id, "orchestrator")
        updated = self.store.update(assignment_id, status="cancelled", owner_id=assignment.owner_id)
        self._publish("AGENT_CANCELLED", "assignment", assignment_id, {})
        return updated

    def reassign(self, assignment_id: str, *, role: str | None = None) -> Assignment:
        original = self.store.get(assignment_id)
        if original is None:
            raise KeyError("assignment not found")
        if original.attempts > self.max_reassignments:
            raise RuntimeError(f"assignment {assignment_id} exhausted its reassignment budget")
        target_role = role or original.role
        replacement = self.dispatch(
            original.objective,
            role=target_role,
            owner_id=original.owner_id,
            parent_run_id=original.parent_run_id,
            goal_id=original.goal_id,
            workflow_instance_id=original.workflow_instance_id,
            node_id=original.node_id,
            delegation_depth=original.delegation_depth,
            mission_id=original.mission_id,
            task_id=original.task_id,
        )
        self.store.update(assignment_id, status="reassigned")
        return replacement

    def aggregate(self, parent_run_id: str, *, owner_id: str | None = None) -> AggregationReport:
        assignments = self.store.list_assignments(
            owner_id=owner_id, parent_run_id=parent_run_id, limit=500
        )
        report = AggregationReport(
            parent_run_id=parent_run_id,
            total=len(assignments),
            succeeded=sum(1 for item in assignments if item.status == "succeeded"),
            failed=sum(1 for item in assignments if item.status == "failed"),
            pending=sum(
                1 for item in assignments if item.status in ("pending", "dispatched", "running")
            ),
            cancelled=sum(1 for item in assignments if item.status == "cancelled"),
        )
        for assignment in assignments:
            if assignment.status == "succeeded":
                report.results.append(
                    {
                        "assignment_id": assignment.assignment_id,
                        "role": assignment.role,
                        "result": assignment.result,
                    }
                )
            elif assignment.status == "failed":
                report.errors.append(f"{assignment.role}: {assignment.error or 'failed'}")
        report.complete = (
            report.total > 0
            and report.pending == 0
            and report.failed == 0
            and report.cancelled == 0
        )
        return report

    def _has_capacity(self, specialist: Specialist, owner_id: str | None = None) -> bool:
        active = self.store.list_assignments(owner_id=owner_id, role=specialist.role, limit=500)
        running = sum(1 for item in active if item.status in ("pending", "dispatched", "running"))
        return running < specialist.max_concurrent

    def _on_job_finished(self, job: Job, status: str, result: str, error: str | None) -> None:
        payload = job.payload or {}
        if payload.get("kind") != "specialist_assignment":
            return
        assignment_id = str(payload.get("assignment_id", ""))
        if not assignment_id:
            return
        assignment = self.store.get(assignment_id)
        if assignment is None or assignment.is_terminal:
            return
        if assignment.owner_id != job.owner_id:
            log.error(
                "job %s for assignment %s changed owner from %s to %s; refusing update",
                job.job_id,
                assignment_id,
                assignment.owner_id,
                job.owner_id,
            )
            return
        if status == "completed":
            updated = self.store.update(
                assignment_id,
                status="succeeded",
                result=result[:20000],
                owner_id=job.owner_id,
            )
            self.mailbox.result(assignment_id, "specialist", result[:8000])
            self._publish(
                "AGENT_COMPLETED",
                "assignment",
                assignment_id,
                {
                    "role": assignment.role,
                },
            )
            _ = updated
        elif status == "failed":
            self.store.update(
                assignment_id,
                status="failed",
                error=(error or result)[:4000],
                owner_id=job.owner_id,
            )
            self.mailbox.failure(assignment_id, "specialist", (error or result)[:8000])
            self._publish(
                "AGENT_FAILED",
                "assignment",
                assignment_id,
                {
                    "role": assignment.role,
                    "error": (error or "")[:500],
                },
            )
        elif status == "cancelled":
            self.store.update(assignment_id, status="cancelled", owner_id=job.owner_id)
        elif status == "unknown_outcome":
            self.store.update(
                assignment_id,
                status="failed",
                error=f"unknown outcome: {error or result}"[:4000],
                owner_id=job.owner_id,
            )

    def _publish(
        self, event_type: str, subject_type: str, subject_id: str, payload: dict[str, Any]
    ) -> None:
        if self.event_publisher is None:
            return
        try:
            self.event_publisher(event_type, subject_type, subject_id, payload)
        except Exception:
            log.exception("orchestrator event publication failed")
