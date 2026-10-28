from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_PLAN_STEPS = 8

SIMPLE_QUESTION_PREFIXES = (
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "which",
    "define",
    "explain",
    "describe",
    "summarize",
    "translate",
)

TOOL_VERBS = (
    "analyze",
    "apply",
    "build",
    "convert",
    "create",
    "delete",
    "deploy",
    "download",
    "edit",
    "execute",
    "fetch",
    "fix",
    "generate",
    "implement",
    "install",
    "migrate",
    "modify",
    "move",
    "organize",
    "refactor",
    "remove",
    "rename",
    "run",
    "scan",
    "search",
    "test",
    "update",
    "write",
)

TEMPORAL_MARKERS = ("latest", "current", "today", "now", "currently", "right now")

CODE_TASK_WORDS = ("bug", "fix", "refactor", "implement", "code", "test")


class PlanStrategy(StrEnum):
    STANDARD = "standard"
    ALTERNATE = "alternate"
    MINIMAL = "minimal"


@dataclass(frozen=True, slots=True)
class PlanStep:
    number: int
    title: str
    success_criteria: str
    expected_outputs: tuple[str, ...] = ()
    tool_categories: tuple[str, ...] = ()
    risk: str = "medium"


@dataclass(frozen=True, slots=True)
class TaskPlan:
    goal: str
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class ObjectiveProfile:
    is_simple: bool
    needs_tools: bool
    missing_info: tuple[str, ...] = ()


def assess_objective(goal: str) -> ObjectiveProfile:
    lowered = re.sub(r"\s+", " ", goal.strip().lower())
    if not lowered:
        return ObjectiveProfile(is_simple=False, needs_tools=False)
    is_question = lowered.endswith("?") or any(
        lowered == prefix or lowered.startswith(prefix + " ") for prefix in SIMPLE_QUESTION_PREFIXES
    )
    has_tool_verb = any(re.search(rf"\b{verb}\b", lowered) for verb in TOOL_VERBS)
    missing: list[str] = []
    if any(marker in lowered for marker in TEMPORAL_MARKERS):
        missing.append(
            "the objective references the present time; confirm data freshness with tools "
            "if precision matters"
        )
    is_simple = is_question and not has_tool_verb and len(lowered) <= 240
    return ObjectiveProfile(
        is_simple=is_simple, needs_tools=has_tool_verb, missing_info=tuple(missing)
    )


_STANDARD_CODE_ACTIONS = [
    ("Inspect workspace", "Identify relevant files and project conventions"),
    ("Trace the implementation", "Locate the code and tests related to the goal"),
    ("Propose a minimal change", "Explain the intended patch before modifying files"),
    ("Apply approved change", "Only edit files inside the configured workspace"),
    ("Run focused checks", "Run relevant tests and inspect failures"),
    ("Review the diff", "Confirm only intended files and lines changed"),
    ("Report outcome", "Summarize changes, checks, and remaining risks"),
]

_STANDARD_GENERAL_ACTIONS = [
    ("Clarify the goal", "Identify the requested result and constraints"),
    ("Gather context", "Use available tools rather than guessing"),
    ("Perform the task", "Complete the smallest safe set of actions"),
    ("Verify the result", "Check the result against the original goal"),
    ("Report outcome", "Summarize the result and any limitations"),
]

_ALTERNATE_CODE_ACTIONS = [
    ("Re-read the failing evidence", "Collect the exact errors and current state before acting"),
    ("Change the approach", "Use a different tool, entry point, or a smaller change than before"),
    ("Apply the narrowed change", "Modify only what the fresh evidence supports"),
    ("Verify the outcome", "Run the focused checks that previously exposed the failure"),
    ("Report the outcome", "State what changed, what was abandoned, and why"),
]

_ALTERNATE_GENERAL_ACTIONS = [
    ("Re-examine the evidence", "Identify which earlier assumption proved wrong"),
    ("Change the approach", "Select a different method or source for the same goal"),
    ("Perform the smallest useful action", "Make one concrete step that can be checked"),
    ("Verify the result", "Check the result against the original goal"),
    ("Report outcome", "Summarize the result and remaining limitations"),
]

_MINIMAL_ACTIONS = [
    ("Act on the smallest useful slice", "Complete one concrete, checkable action toward the goal"),
    ("Verify and report", "Check the outcome against the goal and report status honestly"),
]


class TaskPlanner:
    def __init__(self, max_steps: int = MAX_PLAN_STEPS) -> None:
        if not 1 <= max_steps <= MAX_PLAN_STEPS:
            raise ValueError(f"max_steps must be between 1 and {MAX_PLAN_STEPS}")
        self.max_steps = max_steps

    def create_plan(
        self, goal: str, *, strategy: PlanStrategy | str = PlanStrategy.STANDARD
    ) -> TaskPlan:
        cleaned = goal.strip()
        if not cleaned:
            raise ValueError("task goal must not be empty")
        lowered = cleaned.lower()
        is_code_task = any(word in lowered for word in CODE_TASK_WORDS)
        selected = PlanStrategy(strategy)
        if selected is PlanStrategy.MINIMAL:
            actions = _MINIMAL_ACTIONS
        elif selected is PlanStrategy.ALTERNATE:
            actions = _ALTERNATE_CODE_ACTIONS if is_code_task else _ALTERNATE_GENERAL_ACTIONS
        elif is_code_task:
            actions = _STANDARD_CODE_ACTIONS
        else:
            actions = _STANDARD_GENERAL_ACTIONS
        actions = actions[: self.max_steps]
        steps = tuple(
            PlanStep(i, title, criteria) for i, (title, criteria) in enumerate(actions, 1)
        )
        return TaskPlan(cleaned, steps)

    def replan_plan(
        self,
        goal: str,
        *,
        replan_number: int = 1,
        failures: tuple[str, ...] | list[str] = (),
    ) -> TaskPlan:
        if replan_number < 1:
            raise ValueError("replan_number must be at least 1")
        strategy = PlanStrategy.ALTERNATE if replan_number == 1 else PlanStrategy.MINIMAL
        return self.create_plan(goal, strategy=strategy)
