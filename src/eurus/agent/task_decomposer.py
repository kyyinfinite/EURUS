from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from eurus.multiagent.contracts import TaskSpec
from eurus.multiagent.task_graph import TaskGraphError

TASK_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

REQUIRED_SUBTASK_FIELDS = ("task_key", "objective")
OPTIONAL_ROLE_FIELD = "preferred_role"


@dataclass(frozen=True, slots=True)
class TaskPlan:
    objective: str
    route: str
    tasks: tuple[TaskSpec, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def topological_order(self) -> tuple[str, ...]:
        remaining = {task.task_id: set(task.depends_on) for task in self.tasks}
        ordered: list[str] = []
        while remaining:
            ready = [task_id for task_id, deps in remaining.items() if not deps]
            if not ready:
                raise TaskGraphError("task plan contains a dependency cycle")
            for task_id in ready:
                ordered.append(task_id)
                remaining.pop(task_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(ordered)


class TaskDecomposer:
    def __init__(self, *, known_roles: tuple[str, ...] | None = None) -> None:
        self.known_roles = frozenset(known_roles) if known_roles else None

    def decompose(
        self,
        objective: str,
        *,
        mission_id: str,
        tasks: Sequence[dict[str, Any]] | None = None,
        route: str = "hierarchical",
    ) -> TaskPlan:
        if not objective.strip():
            raise ValueError("objective must not be empty")
        if route not in {"direct", "hierarchical"}:
            raise ValueError(f"unknown route: {route!r}")
        drafts = list(tasks or [])
        if not drafts:
            drafts = [
                {
                    "task_key": "root",
                    "title": objective.strip()[:120],
                    "objective": objective,
                }
            ]
        specs: list[TaskSpec] = []
        keys: dict[str, str] = {}
        for draft in drafts:
            spec = self._spec_from_draft(draft, mission_id=mission_id)
            if spec.task_key in keys:
                raise ValueError(f"duplicate task key in plan: {spec.task_key!r}")
            keys[spec.task_key] = spec.task_id
            specs.append(spec)
        resolved: list[TaskSpec] = []
        for spec in specs:
            unknown = [dep for dep in spec.depends_on if dep not in keys]
            if unknown:
                raise ValueError(f"task {spec.task_key!r} depends on unknown tasks: {unknown}")
            resolved.append(replace(spec, depends_on=tuple(keys[dep] for dep in spec.depends_on)))
        plan = TaskPlan(objective=objective, route=route, tasks=tuple(resolved))
        plan.topological_order()
        return plan

    def _spec_from_draft(self, draft: dict[str, Any], *, mission_id: str) -> TaskSpec:
        if not isinstance(draft, dict):
            raise ValueError("each subtask must be a mapping")
        for required in REQUIRED_SUBTASK_FIELDS:
            if not str(draft.get(required, "")).strip():
                raise ValueError(f"subtask is missing required field {required!r}")
        task_key = str(draft["task_key"]).strip()
        if not TASK_KEY_PATTERN.match(task_key):
            raise ValueError(f"invalid task key: {task_key!r}")
        role = draft.get(OPTIONAL_ROLE_FIELD)
        role_name = str(role).strip() if role else None
        if role_name and self.known_roles is not None and role_name not in self.known_roles:
            raise ValueError(f"task {task_key!r} prefers unknown role {role_name!r}")
        depends_on = draft.get("depends_on", ())
        if isinstance(depends_on, str):
            depends_on = (depends_on,)
        depends = tuple(str(item).strip() for item in depends_on if str(item).strip())
        acceptance = draft.get("acceptance_criteria", ())
        if isinstance(acceptance, str):
            acceptance = (acceptance,)
        capabilities = draft.get("required_capabilities", ())
        if isinstance(capabilities, str):
            capabilities = (capabilities,)
        return TaskSpec(
            task_id=uuid.uuid4().hex,
            mission_id=mission_id,
            task_key=task_key,
            title=str(draft.get("title", "")).strip()[:500] or task_key,
            objective=str(draft["objective"]).strip()[:20000],
            depends_on=depends,
            required_capabilities=tuple(
                str(item).strip() for item in capabilities if str(item).strip()
            ),
            preferred_role=role_name,
            input_contract=_dict_field(draft, "input_contract"),
            output_contract=_dict_field(draft, "output_contract"),
            acceptance_criteria=tuple(
                str(item).strip() for item in acceptance if str(item).strip()
            ),
            priority=int(draft.get("priority", 5)),
        )


def _dict_field(draft: dict[str, Any], name: str) -> dict[str, Any]:
    value = draft.get(name, {})
    return dict(value) if isinstance(value, dict) else {}
