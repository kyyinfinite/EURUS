from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eurus.goals.engine import GoalEngine
from eurus.tools.base import Tool, ToolContext

PLAN_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "string", "description": "Identifier of the goal being planned."},
        "tasks": {
            "type": "array",
            "description": "Ordered task decomposition for the goal.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Titles of tasks that must finish first.",
                    },
                    "success_criteria": {"type": "string"},
                    "milestone": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        "milestones": {
            "type": "array",
            "description": "Optional milestones for progress tracking.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "criteria": {"type": "string"},
                    "target_progress": {"type": "number"},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["goal_id", "tasks"],
}

EVALUATION_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "string"},
        "passed": {"type": "boolean"},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete evidence supporting the verdict.",
        },
    },
    "required": ["goal_id", "passed"],
}

PROGRESS_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "string"},
        "progress": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "note": {"type": "string"},
    },
    "required": ["goal_id", "progress"],
}


def goal_tools(engine: GoalEngine) -> list[Tool]:
    async def submit_plan(arguments: dict[str, Any], context: ToolContext) -> str:
        goal_id = str(arguments.get("goal_id", ""))
        plan = {
            "tasks": arguments.get("tasks", []),
            "milestones": arguments.get("milestones", []),
        }
        if not engine.capture_plan(goal_id, plan):
            return "plan rejected: provide a goal_id and a non-empty tasks list"
        task_count = len(plan["tasks"])
        milestone_count = len(plan["milestones"])
        return f"plan accepted: {task_count} tasks, {milestone_count} milestones recorded"

    async def submit_evaluation(arguments: dict[str, Any], context: ToolContext) -> str:
        goal_id = str(arguments.get("goal_id", ""))
        evaluation = {
            "passed": bool(arguments.get("passed", False)),
            "evidence": list(arguments.get("evidence", [])),
        }
        if not engine.capture_evaluation(goal_id, evaluation):
            return "evaluation rejected: provide goal_id and boolean passed"
        return "evaluation recorded"

    async def report_progress(arguments: dict[str, Any], context: ToolContext) -> str:
        goal_id = str(arguments.get("goal_id", ""))
        try:
            progress = float(arguments.get("progress", 0.0))
        except (TypeError, ValueError):
            return "progress must be a number"
        goal = engine.get(goal_id)
        if goal is None:
            return "goal not found"
        engine.store.set_progress(goal.goal_id, max(0.0, min(1.0, progress)))
        return "progress updated"

    return [
        Tool(
            name="goal_submit_plan",
            description=(
                "Record the decomposed task plan for a goal. Call this once with the full "
                "task list; titles must be unique and dependencies reference other titles."
            ),
            parameters=PLAN_TOOL_PARAMETERS,
            handler=submit_plan,
        ),
        Tool(
            name="goal_submit_evaluation",
            description=("Record the final evaluation verdict for a goal with concrete evidence."),
            parameters=EVALUATION_TOOL_PARAMETERS,
            handler=submit_evaluation,
        ),
        Tool(
            name="goal_report_progress",
            description="Report fractional progress (0 to 1) on a goal during execution.",
            parameters=PROGRESS_TOOL_PARAMETERS,
            handler=report_progress,
        ),
    ]


def goal_tool_provider(
    engine: GoalEngine,
) -> Callable[[Any, Any], list[Tool]]:
    def provider(job: Any, context: Any) -> list[Tool]:
        payload = getattr(job, "payload", None) or {}
        if payload.get("goal_id"):
            return goal_tools(engine)
        return []

    return provider
