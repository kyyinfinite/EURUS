from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from eurus.storage.database import Database, DatabasePath
from eurus.workflows.definition import WorkflowDefinition
from eurus.workflows.states import (
    TERMINAL_NODE_STATUSES,
    TERMINAL_WORKFLOW_STATUSES,
    NodeStatus,
    WorkflowStatus,
    can_transition_node,
    can_transition_workflow,
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


@dataclass(slots=True)
class WorkflowInstance:
    instance_id: str
    workflow_id: str
    version: int
    owner_id: str
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime
    context: dict[str, Any] = field(default_factory=dict)
    goal_id: str | None = None
    parent_instance_id: str | None = None
    child_instance_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKFLOW_STATUSES


@dataclass(slots=True)
class NodeState:
    instance_id: str
    node_id: str
    node_type: str
    status: NodeStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    output: str = ""
    error: str | None = None
    job_id: str | None = None
    resume_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_NODE_STATUSES


@dataclass(slots=True)
class WorkflowApproval:
    approval_id: str
    instance_id: str
    node_id: str
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision: str | None = None
    note: str = ""


class WorkflowStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def save_definition(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        definition.validate()
        current = _now()
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_workflow_definitions(
                    workflow_id, version, name, definition_json, status, owner_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, version) DO UPDATE SET
                    name=excluded.name, definition_json=excluded.definition_json,
                    status=excluded.status, updated_at=excluded.updated_at""",
                (
                    definition.workflow_id,
                    definition.version,
                    definition.name,
                    json.dumps(definition.to_json(), ensure_ascii=False),
                    definition.status,
                    definition.owner_id,
                    _time(current),
                    _time(current),
                ),
            )
        return definition

    def latest_version(self, workflow_id: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM eurus_workflow_definitions WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_definition(
        self, workflow_id: str, *, version: int | None = None, owner_id: str | None = None
    ) -> WorkflowDefinition | None:
        resolved = version if version is not None else self.latest_version(workflow_id)
        if resolved < 1:
            return None
        clauses = ["workflow_id=?", "version=?"]
        values: list[Any] = [workflow_id, resolved]
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT definition_json FROM eurus_workflow_definitions "
                f"WHERE {' AND '.join(clauses)}",
                values,
            ).fetchone()
        if row is None:
            return None
        document = json.loads(row[0])
        parsed = WorkflowDefinition.from_json(document)
        parsed.version = resolved
        return parsed

    def list_definitions(
        self, *, owner_id: str | None = None, limit: int = 100
    ) -> list[WorkflowDefinition]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = (
            "SELECT definition_json FROM eurus_workflow_definitions "
            "WHERE version=(SELECT MAX(version) FROM eurus_workflow_definitions AS newer "
            "WHERE newer.workflow_id=eurus_workflow_definitions.workflow_id)"
        )
        values: list[Any] = []
        if owner_id is not None:
            query += " AND owner_id=?"
            values.append(owner_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return [WorkflowDefinition.from_json(json.loads(row[0])) for row in rows]

    def set_definition_status(self, workflow_id: str, status: str) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """UPDATE eurus_workflow_definitions SET status=?, updated_at=?
                WHERE version=(SELECT MAX(version) FROM eurus_workflow_definitions
                WHERE workflow_id=?)""",
                (status, _time(_now()), workflow_id),
            )
            return cursor.rowcount > 0

    def create_instance(
        self,
        definition: WorkflowDefinition,
        owner_id: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        goal_id: str | None = None,
        parent_instance_id: str | None = None,
        instance_id: str | None = None,
    ) -> WorkflowInstance:
        definition.validate()
        current = _now()
        instance = WorkflowInstance(
            instance_id=instance_id or uuid.uuid4().hex,
            workflow_id=definition.workflow_id,
            version=definition.version,
            owner_id=owner_id,
            status=WorkflowStatus.PENDING,
            created_at=current,
            updated_at=current,
            context={
                "inputs": dict(inputs or {}),
                "nodes": {},
                "vars": {},
            },
            goal_id=goal_id,
            parent_instance_id=parent_instance_id,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_workflow_instances(
                    instance_id, workflow_id, version, owner_id, status, context_json,
                    goal_id, parent_instance_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    instance.instance_id,
                    instance.workflow_id,
                    instance.version,
                    instance.owner_id,
                    instance.status.value,
                    json.dumps(instance.context, ensure_ascii=False),
                    instance.goal_id,
                    instance.parent_instance_id,
                    _time(current),
                    _time(current),
                ),
            )
            for node in definition.nodes:
                connection.execute(
                    """INSERT INTO eurus_workflow_node_states(
                        instance_id, node_id, node_type, status, attempts, output,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, '', ?, ?)""",
                    (
                        instance.instance_id,
                        node.node_id,
                        node.node_type,
                        NodeStatus.PENDING.value,
                        _time(current),
                        _time(current),
                    ),
                )
        return instance

    def get_instance(
        self, instance_id: str, *, owner_id: str | None = None
    ) -> WorkflowInstance | None:
        with self.database.connection() as connection:
            if owner_id is None:
                row = connection.execute(
                    "SELECT * FROM eurus_workflow_instances WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM eurus_workflow_instances WHERE instance_id=? AND owner_id=?",
                    (instance_id, owner_id),
                ).fetchone()
        return self._instance(row) if row is not None else None

    def list_instances(
        self,
        *,
        owner_id: str | None = None,
        statuses: Sequence[WorkflowStatus] = (),
        workflow_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkflowInstance]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        values: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(owner_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(status.value for status in statuses)
        if workflow_id is not None:
            clauses.append("workflow_id=?")
            values.append(workflow_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_workflow_instances{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._instance(row) for row in rows]

    def transition_instance(
        self,
        instance_id: str,
        target: WorkflowStatus,
        *,
        expected_status: WorkflowStatus | None = None,
        error: str | None = None,
        owner_id: str | None = None,
    ) -> WorkflowInstance:
        current = self.get_instance(instance_id, owner_id=owner_id)
        if current is None:
            raise KeyError("workflow instance not found")
        if not can_transition_workflow(current.status, target):
            raise ValueError(
                f"cannot transition workflow from {current.status.value} to {target.value}"
            )
        updated_at = _now()
        assignments: dict[str, Any] = {
            "status": target.value,
            "updated_at": _time(updated_at),
            "error": error,
        }
        if target == WorkflowStatus.RUNNING and current.started_at is None:
            assignments["started_at"] = _time(updated_at)
        if target in TERMINAL_WORKFLOW_STATUSES:
            assignments["finished_at"] = _time(updated_at)
        clauses = ["instance_id=?"]
        values: list[Any] = list(assignments.values())
        if expected_status is not None:
            clauses.append("status=?")
            values.append(expected_status.value)
        values.append(instance_id)
        with self.database.connection() as connection:
            cursor = connection.execute(
                f"UPDATE eurus_workflow_instances SET "
                f"{', '.join(f'{key}=?' for key in assignments)} "
                f"WHERE {' AND '.join(clauses)}",
                values,
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("workflow instance was modified concurrently")
        result = replace(current, status=target, updated_at=updated_at, error=error)
        if target == WorkflowStatus.RUNNING and result.started_at is None:
            result = replace(result, started_at=updated_at)
        if target in TERMINAL_WORKFLOW_STATUSES and result.finished_at is None:
            result = replace(result, finished_at=updated_at)
        return result

    def save_context(self, instance_id: str, context: Mapping[str, Any]) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_workflow_instances SET context_json=?, updated_at=? "
                "WHERE instance_id=?",
                (json.dumps(dict(context), ensure_ascii=False), _time(_now()), instance_id),
            )

    def set_child_instance(self, instance_id: str, child_instance_id: str | None) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_workflow_instances SET child_instance_id=?, updated_at=? "
                "WHERE instance_id=?",
                (child_instance_id, _time(_now()), instance_id),
            )

    def get_node(self, instance_id: str, node_id: str) -> NodeState | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_workflow_node_states WHERE instance_id=? AND node_id=?",
                (instance_id, node_id),
            ).fetchone()
        return self._node(row) if row is not None else None

    def list_nodes(self, instance_id: str) -> list[NodeState]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_workflow_node_states WHERE instance_id=? "
                "ORDER BY created_at, node_id",
                (instance_id,),
            ).fetchall()
        return [self._node(row) for row in rows]

    def update_node(
        self,
        instance_id: str,
        node_id: str,
        *,
        status: NodeStatus | None = None,
        output: str | None = None,
        error: str | None = None,
        job_id: str | None = None,
        resume_at: datetime | None = None,
        increment_attempts: bool = False,
    ) -> NodeState:
        node = self.get_node(instance_id, node_id)
        if node is None:
            raise KeyError(f"node {node_id} not found on instance {instance_id}")
        if (
            status is not None
            and status != node.status
            and not can_transition_node(node.status, status)
        ):
            raise ValueError(
                f"cannot transition node {node_id} from {node.status.value} to {status.value}"
            )
        assignments: dict[str, Any] = {"updated_at": _time(_now())}
        if status is not None:
            assignments["status"] = status.value
            if status == NodeStatus.RUNNING and node.started_at is None:
                assignments["started_at"] = assignments["updated_at"]
            if status in TERMINAL_NODE_STATUSES:
                assignments["finished_at"] = assignments["updated_at"]
        if output is not None:
            assignments["output"] = output[:20000]
        if error is not None:
            assignments["error"] = error[:4000]
        if job_id is not None:
            assignments["job_id"] = job_id
        if resume_at is not None:
            assignments["resume_at"] = _time(resume_at)
        if increment_attempts:
            assignments["attempts"] = node.attempts + 1
        with self.database.connection() as connection:
            connection.execute(
                f"UPDATE eurus_workflow_node_states SET "
                f"{', '.join(f'{key}=?' for key in assignments)} "
                f"WHERE instance_id=? AND node_id=?",
                (*assignments.values(), instance_id, node_id),
            )
        refreshed = self.get_node(instance_id, node_id)
        assert refreshed is not None
        return refreshed

    def node_counts(self, instance_id: str) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM eurus_workflow_node_states "
                "WHERE instance_id=? GROUP BY status",
                (instance_id,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def create_approval(self, instance_id: str, node_id: str, *, prompt: str) -> WorkflowApproval:
        current = _now()
        approval = WorkflowApproval(
            approval_id=uuid.uuid4().hex,
            instance_id=instance_id,
            node_id=node_id,
            requested_at=current,
            note=prompt[:4000],
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_workflow_approvals(
                    approval_id, instance_id, node_id, requested_at, note
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    approval.approval_id,
                    approval.instance_id,
                    approval.node_id,
                    _time(current),
                    approval.note,
                ),
            )
        return approval

    def get_approval(self, approval_id: str) -> WorkflowApproval | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_workflow_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        return self._approval(row) if row is not None else None

    def list_approvals(
        self,
        *,
        instance_id: str | None = None,
        pending: bool = False,
        limit: int = 100,
    ) -> list[WorkflowApproval]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        values: list[Any] = []
        if instance_id is not None:
            clauses.append("instance_id=?")
            values.append(instance_id)
        if pending:
            clauses.append("decided_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM eurus_workflow_approvals{where} ORDER BY requested_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._approval(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str = "",
        owner_id: str | None = None,
    ) -> WorkflowApproval | None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        approval = self.get_approval(approval_id)
        if approval is None:
            return None
        if owner_id is not None:
            instance = self.get_instance(approval.instance_id, owner_id=owner_id)
            if instance is None:
                return None
        with self.database.connection() as connection:
            cursor = connection.execute(
                """UPDATE eurus_workflow_approvals SET decided_at=?, decided_by=?,
                decision=?, note=? WHERE approval_id=? AND decided_at IS NULL""",
                (_time(_now()), decided_by, decision, note[:4000], approval_id),
            )
            if cursor.rowcount != 1:
                existing = self.get_approval(approval_id)
                if existing is not None and existing.decision == decision:
                    return existing
                return None
        return self.get_approval(approval_id)

    def _instance(self, row: sqlite3.Row) -> WorkflowInstance:
        return WorkflowInstance(
            instance_id=row["instance_id"],
            workflow_id=row["workflow_id"],
            version=int(row["version"]),
            owner_id=row["owner_id"],
            status=WorkflowStatus(row["status"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            context=json.loads(row["context_json"] or "{}"),
            goal_id=row["goal_id"],
            parent_instance_id=row["parent_instance_id"],
            child_instance_id=row["child_instance_id"],
            error=row["error"],
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
        )

    def _node(self, row: sqlite3.Row) -> NodeState:
        return NodeState(
            instance_id=row["instance_id"],
            node_id=row["node_id"],
            node_type=row["node_type"],
            status=NodeStatus(row["status"]),
            attempts=int(row["attempts"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            output=row["output"] or "",
            error=row["error"],
            job_id=row["job_id"],
            resume_at=_parse(row["resume_at"]),
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
        )

    def _approval(self, row: sqlite3.Row) -> WorkflowApproval:
        return WorkflowApproval(
            approval_id=row["approval_id"],
            instance_id=row["instance_id"],
            node_id=row["node_id"],
            requested_at=_parse(row["requested_at"]) or datetime.now(UTC),
            decided_at=_parse(row["decided_at"]),
            decided_by=row["decided_by"],
            decision=row["decision"],
            note=row["note"] or "",
        )
