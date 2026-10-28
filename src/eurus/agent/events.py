from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eurus.models import ToolCall, ToolResult, Usage

AgentEventKind = Literal[
    "step",
    "text",
    "tool_call",
    "tool_result",
    "done",
    "lesson",
    "skill",
    "plan",
    "recovery",
    "model_call",
    "text_reset",
]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: AgentEventKind
    step: int = 0
    text: str = ""
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    usage: Usage | None = None
    stop_reason: str = ""

    @classmethod
    def step_started(cls, step: int) -> AgentEvent:
        return cls("step", step=step)

    @classmethod
    def text_delta(cls, text: str) -> AgentEvent:
        return cls("text", text=text)

    @classmethod
    def tool_started(cls, call: ToolCall) -> AgentEvent:
        return cls("tool_call", tool_call=call)

    @classmethod
    def tool_finished(cls, result: ToolResult) -> AgentEvent:
        return cls("tool_result", tool_result=result)

    @classmethod
    def lesson_saved(cls, text: str) -> AgentEvent:
        return cls("lesson", text=text)

    @classmethod
    def skill_drafted(cls, text: str) -> AgentEvent:
        return cls("skill", text=text)

    @classmethod
    def plan_updated(cls, text: str, step: int = 0) -> AgentEvent:
        return cls("plan", step=step, text=text)

    @classmethod
    def recovery_attempted(cls, text: str, step: int = 0) -> AgentEvent:
        return cls("recovery", step=step, text=text)

    @classmethod
    def text_reset(cls) -> AgentEvent:
        return cls("text_reset")

    @classmethod
    def model_completed(cls, step: int, usage: Usage | None, finish: str) -> AgentEvent:
        return cls("model_call", step=step, usage=usage, stop_reason=finish)

    @classmethod
    def finished(cls, stop_reason: str, usage: Usage, text: str = "") -> AgentEvent:
        return cls("done", stop_reason=stop_reason, usage=usage, text=text)
