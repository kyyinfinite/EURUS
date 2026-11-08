from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from eurus.multiagent.contracts import TASK_STATUSES, TERMINAL_TASK_STATUSES, TaskSpec
from eurus.storage.database import Database, DatabasePath


class TaskGraphError(ValueError):
    pass


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


class TaskGraphStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def add_task(self, task: TaskSpec, *, owner_id: str) -> TaskSpec:
        self._require_mission_owner(task.mission_id, owner_id)
        with self.database.connection() as connection:
            mission = connection.execute(
                "SELECT mission_id FROM eurus_agent_missions WHERE mission_id=? AND owner_id=?",
                (task.mission_id, owner_id),
            ).fetchone()
            if mission is None:
                raise KeyError(f"mission {task.mission_id!r} not found for this owner")
            duplicate = connection.execute(
                "SELECT 1 FROM eurus_agent_tasks WHERE mission_id=? AND task_key=?",
                (task.mission_id, task.task_key),
            ).fetchone()
            if duplicate is not None:
                raise TaskGraphError(f"task key {task.task_key!r} already exists in mission")
            connection.execute(
                """INSERT INTO eurus_agent_tasks(
                    task_id, mission_id, parent_task_id, task_key, title, objective,
                    required_capabilities_json, preferred_role, input_contract_json,
                    output_contract_json, acceptance_json, status, priority, attempt,
                    assignment_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.mission_id,
                    task.parent_task_id,
                    task.task_key,
                    task.title[:500],
                    task.objective[:20000],
                    _json_list(task.required_capabilities),
                    task.preferred_role,
                    _json_dict(task.input_contract),
                    _json_dict(task.output_contract),
                    _json_list(task.acceptance_criteria),
                    task.status,
                    task.priority,
                    task.attempt,
                    task.assignment_id,
                    _time(task.created_at),
                    _time(task.updated_at),
                ),
            )
            for dep in task.depends_on:
                self._insert_dependency(connection, task.mission_id, task.task_id, dep)
        return task

    def get(self, task_id: str, *, owner_id: str) -> TaskSpec | None:
        task = self._task_row(task_id)
        if task is None:
            return None
        self._require_mission_owner(task["mission_id"], owner_id)
        dependencies = self.dependencies_of(task_id)
        return self._task(task, dependencies)

    def list_tasks(self, mission_id: str, *, owner_id: str) -> list[TaskSpec]:
        self._require_mission_owner(mission_id, owner_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_tasks WHERE mission_id=? "
                "ORDER BY priority, task_key, created_at",
                (mission_id,),
            ).fetchall()
        deps = self._dependency_map(mission_id)
        return [self._task(row, tuple(deps.get(row["task_id"], ()))) for row in rows]

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        owner_id: str,
        assignment_id: str | None = None,
        increment_attempt: bool = False,
        clear_assignment: bool = False,
    ) -> TaskSpec:
        if status not in TASK_STATUSES:
            raise TaskGraphError(f"unknown task status: {status!r}")
        task = self.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task {task_id!r} not found for this owner")
        with self.database.connection() as connection:
            assignments: list[Any] = [status, _time(_now())]
            sets = ["status=?", "updated_at=?"]
            if clear_assignment:
                sets.append("assignment_id=NULL")
            elif assignment_id is not None:
                sets.append("assignment_id=?")
                assignments.append(assignment_id)
            if increment_attempt:
                sets.append("attempt=attempt+1")
            connection.execute(
                f"UPDATE eurus_agent_tasks SET {', '.join(sets)} WHERE task_id=?",
                (*assignments, task_id),
            )
        updated = self.get(task_id, owner_id=owner_id)
        if updated is None:
            raise TaskGraphError(f"task {task_id!r} disappeared during update")
        return updated

    def add_dependency(
        self, mission_id: str, task_id: str, depends_on_task_id: str, *, owner_id: str
    ) -> None:
        if task_id == depends_on_task_id:
            raise TaskGraphError("a task cannot depend on itself")
        self._require_mission_owner(mission_id, owner_id)
        with self.database.connection() as connection:
            for required in (task_id, depends_on_task_id):
                row = connection.execute(
                    "SELECT mission_id FROM eurus_agent_tasks WHERE task_id=?", (required,)
                ).fetchone()
                if row is None or row["mission_id"] != mission_id:
                    raise TaskGraphError(
                        f"task {required!r} does not exist in mission {mission_id!r}"
                    )
            self._insert_dependency(connection, mission_id, task_id, depends_on_task_id)
            self._assert_acyclic(connection, mission_id)

    def dependencies_of(self, task_id: str) -> tuple[str, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT depends_on_task_id FROM eurus_agent_task_dependencies "
                "WHERE task_id=? ORDER BY depends_on_task_id",
                (task_id,),
            ).fetchall()
        return tuple(row["depends_on_task_id"] for row in rows)

    def dependents_of(self, task_id: str) -> tuple[str, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT task_id FROM eurus_agent_task_dependencies "
                "WHERE depends_on_task_id=? ORDER BY task_id",
                (task_id,),
            ).fetchall()
        return tuple(row["task_id"] for row in rows)

    def promote_ready(self, mission_id: str, *, owner_id: str) -> list[TaskSpec]:
        self._require_mission_owner(mission_id, owner_id)
        tasks = self.list_tasks(mission_id, owner_id=owner_id)
        status_by_id = {task.task_id: task.status for task in tasks}
        promoted: list[TaskSpec] = []
        for task in tasks:
            if task.status != "pending":
                continue
            dep_statuses = [status_by_id.get(dep) for dep in task.depends_on]
            if any(dep in {"failed", "cancelled", "skipped"} for dep in dep_statuses):
                promoted.append(self.update_status(task.task_id, "blocked", owner_id=owner_id))
                continue
            if any(dep != "succeeded" for dep in dep_statuses):
                continue
            promoted.append(self.update_status(task.task_id, "ready", owner_id=owner_id))
        return promoted

    def ready_tasks(self, mission_id: str, *, owner_id: str, limit: int = 20) -> list[TaskSpec]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._require_mission_owner(mission_id, owner_id)
        tasks = [
            task
            for task in self.list_tasks(mission_id, owner_id=owner_id)
            if task.status == "ready"
        ]
        tasks.sort(key=lambda task: (task.priority, task.task_key, task.created_at))
        return tasks[:limit]

    def _task_row(self, task_id: str) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM eurus_agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return row

    def _dependency_map(self, mission_id: str) -> dict[str, tuple[str, ...]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT task_id, depends_on_task_id FROM eurus_agent_task_dependencies "
                "WHERE mission_id=?",
                (mission_id,),
            ).fetchall()
        mapping: dict[str, list[str]] = {}
        for row in rows:
            mapping.setdefault(row["task_id"], []).append(row["depends_on_task_id"])
        return {key: tuple(sorted(values)) for key, values in mapping.items()}

    def _insert_dependency(
        self, connection: sqlite3.Connection, mission_id: str, task_id: str, depends_on: str
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO eurus_agent_task_dependencies(
                mission_id, task_id, depends_on_task_id, created_at
            ) VALUES (?, ?, ?, ?)""",
            (mission_id, task_id, depends_on, _time(_now())),
        )

    def _assert_acyclic(self, connection: sqlite3.Connection, mission_id: str) -> None:
        edges: dict[str, set[str]] = {}
        for row in connection.execute(
            "SELECT task_id, depends_on_task_id FROM eurus_agent_task_dependencies "
            "WHERE mission_id=?",
            (mission_id,),
        ):
            edges.setdefault(row["task_id"], set()).add(row["depends_on_task_id"])
        remaining = {node: set(deps) for node, deps in edges.items()}
        for row in connection.execute(
            "SELECT task_id FROM eurus_agent_tasks WHERE mission_id=?", (mission_id,)
        ):
            remaining.setdefault(row["task_id"], set())
        while remaining:
            ready = [node for node, deps in remaining.items() if not deps]
            if not ready:
                raise TaskGraphError(f"dependency cycle detected in mission {mission_id!r}")
            for node in ready:
                remaining.pop(node)
            for deps in remaining.values():
                deps.difference_update(ready)

    def _require_mission_owner(self, mission_id: str, owner_id: str) -> None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT owner_id FROM eurus_agent_missions WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise KeyError(f"mission {mission_id!r} not found for this owner")

    def _task(self, row: sqlite3.Row, dependencies: tuple[str, ...]) -> TaskSpec:
        return TaskSpec(
            task_id=row["task_id"],
            mission_id=row["mission_id"],
            parent_task_id=row["parent_task_id"],
            task_key=row["task_key"],
            title=row["title"] or "",
            objective=row["objective"],
            depends_on=dependencies,
            required_capabilities=tuple(json_loads(row["required_capabilities_json"] or "[]")),
            preferred_role=row["preferred_role"],
            input_contract=json_loads_dict(row["input_contract_json"]),
            output_contract=json_loads_dict(row["output_contract_json"]),
            acceptance_criteria=tuple(json_loads(row["acceptance_json"] or "[]")),
            status=row["status"],
            priority=int(row["priority"]),
            attempt=int(row["attempt"]),
            assignment_id=row["assignment_id"],
            created_at=_parse(row["created_at"]) or _now(),
            updated_at=_parse(row["updated_at"]) or _now(),
        )


def json_loads(value: str | None) -> list[Any]:
    import json

    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def json_loads_dict(value: str | None) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(values: Iterable[str]) -> str:
    import json

    return json.dumps(list(values))


def _json_dict(values: dict[str, Any]) -> str:
    import json

    return json.dumps(values)


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "TASK_STATUSES",
    "TaskGraphError",
    "TaskGraphStore",
    "TaskSpec",
]
