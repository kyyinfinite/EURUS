from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eurus.content import ContentBlock
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
    "timing",
    "content_block_started",
    "content_block_delta",
    "content_block_completed",
    "media_input_analyzing",
    "audio_output_started",
    "audio_output_delta",
    "artifact",
]

ARTIFACT_STATUSES = frozenset({"created", "ready", "failed"})


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: AgentEventKind
    step: int = 0
    text: str = ""
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    usage: Usage | None = None
    stop_reason: str = ""
    stage: str = ""
    duration_ms: float = 0.0
    block: ContentBlock | None = None
    block_index: int = 0
    media_kind: str = ""
    asset_id: str = ""
    artifact_id: str = ""
    artifact_status: str = ""

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
    def timing(cls, stage: str, duration_ms: float) -> AgentEvent:
        return cls("timing", stage=stage, duration_ms=duration_ms)

    @classmethod
    def finished(cls, stop_reason: str, usage: Usage, text: str = "") -> AgentEvent:
        return cls("done", stop_reason=stop_reason, usage=usage, text=text)

    @classmethod
    def content_block_started(cls, index: int, block: ContentBlock) -> AgentEvent:
        return cls("content_block_started", block_index=index, block=block)

    @classmethod
    def content_block_delta(cls, index: int, text: str) -> AgentEvent:
        return cls("content_block_delta", block_index=index, text=text)

    @classmethod
    def content_block_completed(cls, index: int, block: ContentBlock) -> AgentEvent:
        return cls("content_block_completed", block_index=index, block=block)

    @classmethod
    def media_input_analyzing(cls, asset_id: str, media_kind: str) -> AgentEvent:
        return cls("media_input_analyzing", asset_id=asset_id, media_kind=media_kind)

    @classmethod
    def audio_output_started(cls, asset_id: str) -> AgentEvent:
        return cls("audio_output_started", asset_id=asset_id)

    @classmethod
    def audio_output_delta(cls, text: str) -> AgentEvent:
        return cls("audio_output_delta", text=text)

    @classmethod
    def artifact_lifecycle(cls, artifact_id: str, status: str) -> AgentEvent:
        if status not in ARTIFACT_STATUSES:
            raise ValueError(f"artifact status must be one of {sorted(ARTIFACT_STATUSES)}")
        return cls("artifact", artifact_id=artifact_id, artifact_status=status)
