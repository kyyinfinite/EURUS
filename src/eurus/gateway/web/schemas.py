from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCallView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    role: Role
    content: str
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class SessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    active_run_id: str | None = None


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="New conversation", max_length=80)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100_000)
    attachments: list[str] = Field(default_factory=list, max_length=8)


class MediaView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    filename: str
    mime: str
    kind: Literal["image", "audio", "video", "document"]
    size: int
    sha256: str
    created_at: str
    url: str


class SendMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str | None
    objective: str
    status: str
    stop_reason: str | None
    owner_id: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    origin: Literal["web", "external"]


class RuntimeEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: str
    event_type: str
    sequence_no: int
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    step_id: str | None = None
    attempt_id: str | None = None
    parent_run_id: str | None = None


class ApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    run_id: str
    session_id: str
    tool_name: str
    arguments_json: str
    fingerprint: str
    expires_in_seconds: int


class ResolveApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    decision: Literal["approved", "denied"]


class ProviderView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    protocol: str
    base_url: str
    api_key_env: str
    status: Literal["ready", "missing key", "no key needed"]
    builtin: bool = False


class ModelInfoView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    context: int


class ToolView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    mutates: bool
    requires_approval: bool
    parallel_safe: bool
    parameters: dict[str, Any] = Field(default_factory=dict)


class MCPServerView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: str
    enabled: bool
    connected: bool
    tools: int
    state: str
    error: str


class MemoryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    content: str
    tags: str
    category: str
    created_at: str
    updated_at: str
    memory_type: str
    confidence: float
    source: str
    use_count: int
    last_used_at: str | None
    scope: str
    trust: str
    expires_at: str | None = None


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20_000)
    category: str = "general"
    tags: str = ""
    memory_type: str = "fact"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ConfigPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str = Field(min_length=1, max_length=64)
    values: dict[str, Any]
    revision: int | None = None


class ConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]
    config_path: str
    revision: int
    editable_sections: list[str]


class LogRecordView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: float
    level: str
    logger: str
    message: str


class HealthView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    version: str
    uptime_seconds: float
    runtime: dict[str, Any]


class RuntimeStatusView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_runs: list[RunView]
    recent_runs: list[RunView]
    runtime_health: dict[str, Any]
    pending_approvals: int
    session_count: int
    websocket_clients: int


class DashboardView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeStatusView
    recent_sessions: list[SessionView]
    session_events: dict[str, int]
    diagnostics: dict[str, Any]
