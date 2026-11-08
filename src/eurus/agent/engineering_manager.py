from __future__ import annotations

from dataclasses import dataclass, field

from eurus.agent.task_decomposer import TaskPlan
from eurus.multiagent.contracts import TaskSpec

MAX_RECOMMENDED_TASKS = 12
MAX_FAN_IN = 4
MAX_FAN_OUT = 5
VERIFICATION_CAPABILITIES = frozenset({"tests", "qa", "verification", "regression"})


@dataclass(frozen=True, slots=True)
class PlanDecision:
    approved: bool
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        parts = ["approved" if self.approved else "rejected"]
        parts.extend(f"issue: {item}" for item in self.issues)
        parts.extend(f"warning: {item}" for item in self.warnings)
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class ConflictGroup:
    paths: tuple[str, ...]
    assignments: tuple[str, ...]

    @property
    def requires_serialization(self) -> bool:
        return len(self.assignments) > 1


@dataclass(frozen=True, slots=True)
class ConflictPlan:
    groups: tuple[ConflictGroup, ...] = ()
    independent: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_conflicts(self) -> bool:
        return any(group.requires_serialization for group in self.groups)


class EngineeringManager:
    def __init__(
        self,
        *,
        max_tasks: int = MAX_RECOMMENDED_TASKS,
        enabled_roles: tuple[str, ...] | frozenset[str] | None = None,
    ) -> None:
        self.max_tasks = max_tasks
        self.enabled_roles = frozenset(enabled_roles) if enabled_roles else None

    def review_plan(self, plan: TaskPlan) -> PlanDecision:
        issues: list[str] = []
        warnings: list[str] = []
        tasks = plan.tasks

        if not tasks:
            issues.append("plan has no tasks")
            return PlanDecision(approved=False, issues=tuple(issues))

        if plan.route == "hierarchical" and len(tasks) > self.max_tasks:
            warnings.append(
                f"plan decomposes into {len(tasks)} tasks (recommended <= {self.max_tasks}); "
                "consider splitting the mission"
            )

        for task in tasks:
            if self.enabled_roles is not None:
                if task.preferred_role and task.preferred_role not in self.enabled_roles:
                    issues.append(
                        f"task {task.task_key!r} prefers disabled role {task.preferred_role!r}"
                    )
            elif task.preferred_role:
                warnings.append(
                    f"task {task.task_key!r} role {task.preferred_role!r} "
                    "could not be checked against enabled roles"
                )
            if task.priority < 1 or task.priority > 9:
                issues.append(f"task {task.task_key!r} priority out of range")

        for task in tasks:
            if len(task.depends_on) > MAX_FAN_IN:
                warnings.append(
                    f"task {task.task_key!r} waits on {len(task.depends_on)} "
                    f"dependencies (>{MAX_FAN_IN}); integration cost grows quadratically"
                )

        dependents = _dependency_counts(tasks)
        for task_id, count in sorted(dependents.items()):
            if count > MAX_FAN_OUT:
                task = next(item for item in tasks if item.task_id == task_id)
                warnings.append(
                    f"task {task.task_key!r} gates {count} dependents "
                    f"(>{MAX_FAN_OUT}); verify the split is really independent"
                )

        has_verification = any(
            set(task.required_capabilities) & VERIFICATION_CAPABILITIES
            or (task.preferred_role or "") in VERIFICATION_CAPABILITIES
            for task in tasks
        )
        if plan.route == "hierarchical" and not has_verification:
            warnings.append(
                "hierarchical plan has no QA/verification task; acceptance will need "
                "evidence from elsewhere"
            )

        roots = [task.task_key for task in tasks if not task.depends_on]
        if len(roots) > MAX_FAN_OUT:
            warnings.append(
                f"plan has {len(roots)} independent roots; check whether the work is one mission"
            )

        return PlanDecision(approved=not issues, issues=tuple(issues), warnings=tuple(warnings))

    def route_conflicts(
        self, changed_paths: dict[str, tuple[str, ...] | list[str]]
    ) -> ConflictPlan:
        path_owners: dict[str, list[str]] = {}
        for assignment_id in sorted(changed_paths):
            for path in changed_paths[assignment_id]:
                normalized = str(path).strip().replace("\\", "/").lstrip("./")
                if normalized:
                    path_owners.setdefault(normalized, []).append(assignment_id)

        groups: list[ConflictGroup] = []
        seen: set[frozenset[str]] = set()
        for path, owners in sorted(path_owners.items()):
            if len(owners) < 2:
                continue
            key = frozenset(owners)
            if key in seen:
                merged = next(group for group in groups if frozenset(group.assignments) == key)
                groups.remove(merged)
                groups.append(
                    ConflictGroup(
                        paths=tuple(sorted(set(merged.paths) | {path})),
                        assignments=tuple(sorted(key)),
                    )
                )
                continue
            seen.add(key)
            groups.append(ConflictGroup(paths=(path,), assignments=tuple(sorted(key))))

        independent = tuple(
            assignment_id
            for assignment_id in sorted(changed_paths)
            if not any(assignment_id in group.assignments for group in groups)
        )
        return ConflictPlan(groups=tuple(groups), independent=independent)


def _dependency_counts(tasks: tuple[TaskSpec, ...] | list[TaskSpec]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        for dependency in task.depends_on:
            counts[dependency] = counts.get(dependency, 0) + 1
    return counts
