from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from eurus.agent.planner import MAX_PLAN_STEPS, PlanStep, TaskPlan, TaskPlanner
from eurus.models import Message

MAX_GOAL_CHARS = 4000
MAX_MODEL_PLAN_SECONDS = 45.0
ALLOWED_RISK_LEVELS = frozenset({"low", "medium", "high"})
ALLOWED_TOOL_CATEGORIES = frozenset(
    {
        "read",
        "write",
        "edit",
        "shell",
        "search",
        "web",
        "browser",
        "git",
        "test",
        "memory",
        "analysis",
        "delegation",
        "none",
    }
)
_STEP_KEYS = frozenset(
    {
        "step_id",
        "title",
        "description",
        "depends_on",
        "expected_outputs",
        "success_criteria",
        "tool_categories",
        "risk",
        "verification",
    }
)
_PLAN_KEYS = frozenset({"objective", "assumptions", "open_questions", "steps", "replan_conditions"})

PLANNER_PROMPT = (
    "You are a planning module for an autonomous agent. Produce a minimal, executable "
    "plan for the goal. Respond with JSON only, no prose, matching exactly this schema:\n"
    '{"objective": string, "assumptions": [string], "open_questions": [string], '
    '"steps": [{"step_id": string, "title": string, "description": string, '
    '"depends_on": [string], "expected_outputs": [string], "success_criteria": string, '
    '"tool_categories": [string], "risk": "low"|"medium"|"high", "verification": string}], '
    '"replan_conditions": [string]}\n'
    "Rules: 1 to 8 steps; step_id values are unique short identifiers like s1; "
    "depends_on may only reference earlier steps; success_criteria must be objectively "
    "checkable; risk classifies side-effect potential; tool_categories values come from "
    "read, write, edit, shell, search, web, browser, git, test, memory, analysis, "
    "delegation, none. Never grant permissions or approvals in a plan; plans are "
    "proposals subject to runtime policy.\n\nGoal: "
)


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlannedStep:
    step_id: str
    title: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    success_criteria: str = ""
    tool_categories: tuple[str, ...] = ()
    risk: str = "low"
    verification: str = ""


@dataclass(frozen=True, slots=True)
class StructuredPlan:
    objective: str
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    steps: tuple[PlannedStep, ...] = ()
    replan_conditions: tuple[str, ...] = ()
    source: str = "model"


def _string_list(value: Any, *, max_items: int = 12, max_chars: int = 400) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanValidationError("expected a list of strings")
    items: list[str] = []
    for item in value[:max_items]:
        if not isinstance(item, str):
            raise PlanValidationError("expected a list of strings")
        cleaned = item.strip()
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars]
        if cleaned:
            items.append(cleaned)
    return tuple(items)


def _parse_step(raw: Any, index: int) -> PlannedStep:
    if not isinstance(raw, dict):
        raise PlanValidationError(f"step {index} must be an object")
    unknown = sorted(set(raw) - _STEP_KEYS)
    if unknown:
        raise PlanValidationError(f"step {index} has unknown key(s): {', '.join(unknown)}")
    step_id = str(raw.get("step_id", "")).strip()
    if not step_id or len(step_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", step_id):
        raise PlanValidationError(f"step {index} has an invalid step_id")
    title = str(raw.get("title", "")).strip()
    if not title or len(title) > 200:
        raise PlanValidationError(f"step {step_id} requires a title of 1 to 200 characters")
    success_criteria = str(raw.get("success_criteria", "")).strip()
    if not success_criteria or len(success_criteria) > 600:
        raise PlanValidationError(
            f"step {step_id} requires success_criteria of 1 to 600 characters"
        )
    risk = str(raw.get("risk", "low")).strip().lower()
    if risk not in ALLOWED_RISK_LEVELS:
        raise PlanValidationError(f"step {step_id} has invalid risk {risk!r}")
    categories = _string_list(raw.get("tool_categories", []), max_items=6, max_chars=40)
    for category in categories:
        if category.lower() not in ALLOWED_TOOL_CATEGORIES:
            raise PlanValidationError(f"step {step_id} has unknown tool category {category!r}")
    return PlannedStep(
        step_id=step_id,
        title=title[:200],
        description=str(raw.get("description", ""))[:600],
        depends_on=_string_list(raw.get("depends_on", []), max_items=8, max_chars=64),
        expected_outputs=_string_list(raw.get("expected_outputs", []), max_items=6),
        success_criteria=success_criteria,
        tool_categories=tuple(category.lower() for category in categories),
        risk=risk,
        verification=str(raw.get("verification", ""))[:400],
    )


def parse_structured_plan(goal: str, data: Any) -> StructuredPlan:
    if not isinstance(data, dict):
        raise PlanValidationError("plan must be a JSON object")
    unknown = sorted(set(data) - _PLAN_KEYS)
    if unknown:
        raise PlanValidationError(f"plan has unknown key(s): {', '.join(unknown)}")
    objective = str(data.get("objective", "")).strip()
    if not objective:
        raise PlanValidationError("plan objective must not be empty")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
        raise PlanValidationError(f"plan must contain 1 to {MAX_PLAN_STEPS} steps")
    steps = tuple(_parse_step(raw, index) for index, raw in enumerate(raw_steps))
    plan = StructuredPlan(
        objective=objective[:MAX_GOAL_CHARS],
        assumptions=_string_list(data.get("assumptions", [])),
        open_questions=_string_list(data.get("open_questions", [])),
        steps=steps,
        replan_conditions=_string_list(data.get("replan_conditions", [])),
    )
    violations = validate_plan(plan)
    if violations:
        raise PlanValidationError("; ".join(violations))
    return plan


def validate_plan(plan: StructuredPlan) -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    for step in plan.steps:
        if step.step_id in seen:
            violations.append(f"duplicate step_id {step.step_id!r}")
        seen.add(step.step_id)
    for step in plan.steps:
        for dependency in step.depends_on:
            if dependency == step.step_id:
                violations.append(f"step {step.step_id!r} depends on itself")
            elif dependency not in seen:
                violations.append(f"step {step.step_id!r} depends on unknown step {dependency!r}")
    if _has_cycle(plan.steps):
        violations.append("dependency graph contains a cycle")
    return violations


def _has_cycle(steps: tuple[PlannedStep, ...]) -> bool:
    graph = {step.step_id: set(step.depends_on) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dependency in graph.get(node, ()):
            if dependency in graph and visit(dependency):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    return any(visit(step.step_id) for step in steps)


def _topological_order(steps: tuple[PlannedStep, ...]) -> list[PlannedStep]:
    remaining = {step.step_id: step for step in steps}
    ordered: list[PlannedStep] = []
    placed: set[str] = set()
    while remaining:
        ready = [
            step
            for step in remaining.values()
            if all(dependency in placed for dependency in step.depends_on)
        ]
        if not ready:
            ordered.extend(remaining.values())
            break
        for step in ready:
            ordered.append(step)
            placed.add(step.step_id)
            del remaining[step.step_id]
    return ordered


def structured_to_task_plan(plan: StructuredPlan, max_steps: int = MAX_PLAN_STEPS) -> TaskPlan:
    ordered = _topological_order(plan.steps)[:max_steps]
    steps = tuple(
        PlanStep(
            number=index,
            title=f"{step.title} [{step.step_id}]",
            success_criteria=step.success_criteria,
            expected_outputs=tuple(step.expected_outputs),
            tool_categories=tuple(step.tool_categories),
            risk=step.risk,
        )
        for index, step in enumerate(ordered, 1)
    )
    return TaskPlan(goal=plan.objective, steps=steps)


def _extract_json_object(text: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise PlanValidationError("model output contains no JSON object")
    try:
        return json.loads(candidate[start : end + 1])
    except ValueError as exc:
        raise PlanValidationError("model plan is not valid JSON") from exc


class StructuredPlanner:
    def __init__(
        self,
        router: Any | None,
        *,
        fallback: TaskPlanner | None = None,
        max_steps: int = MAX_PLAN_STEPS,
        model_timeout: float = MAX_MODEL_PLAN_SECONDS,
    ) -> None:
        self.router = router
        self.fallback = fallback or TaskPlanner(max_steps=max_steps)
        self.max_steps = max_steps
        self.model_timeout = model_timeout
        self.last_source: str = "deterministic"

    async def create_structured_plan(self, goal: str) -> StructuredPlan | None:
        cleaned = goal.strip()
        if not cleaned or not self.router:
            return None
        prompt = PLANNER_PROMPT + cleaned[:MAX_GOAL_CHARS]
        try:
            response = await asyncio.wait_for(
                self.router.chat([Message.user(prompt)]), timeout=self.model_timeout
            )
            answer = response.message.content.strip()
        except Exception:
            return None
        if not answer:
            return None
        try:
            data = _extract_json_object(answer)
            return parse_structured_plan(cleaned, data)
        except PlanValidationError:
            return None

    async def create_plan_async(self, goal: str) -> TaskPlan:
        structured = await self.create_structured_plan(goal)
        if structured is not None:
            self.last_source = "model"
            return structured_to_task_plan(structured, self.max_steps)
        self.last_source = "deterministic"
        return self.fallback.create_plan(goal)

    async def replan_plan_async(
        self,
        goal: str,
        *,
        replan_number: int = 1,
        failures: tuple[str, ...] | list[str] = (),
    ) -> TaskPlan:
        self.last_source = "deterministic"
        return self.fallback.replan_plan(goal, replan_number=replan_number, failures=failures)

    async def create_plan(self, goal: str) -> TaskPlan:
        return await self.create_plan_async(goal)
