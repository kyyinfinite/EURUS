from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eurus.errors import ToolError
from eurus.execution.envelope import ExecutionEnvelope
from eurus.models import ToolCall, ToolResult
from eurus.network.policy import NetworkPolicyEngine
from eurus.providers.base import INVALID_ARGUMENTS_KEY, ToolSpec
from eurus.runtime.budgets import BudgetTracker, ChildBudgetCaps
from eurus.tools.schema import validate_arguments, validate_tool_output
from eurus.workspace import WorkspaceManager

if TYPE_CHECKING:
    from eurus.research.engine import ResearchEngine
    from eurus.tools.artifacts import ArtifactProvider

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEAD_SHARE = 0.7

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable["str | ToolOutput"]]
MutationVerifier = Callable[[dict[str, Any], "ToolContext"], Awaitable["MutationVerification"]]

MUTATION_VERIFICATION_STATUSES = frozenset({"completed", "absent", "unknown"})


@dataclass(frozen=True, slots=True)
class MutationVerification:
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in MUTATION_VERIFICATION_STATUSES:
            raise ValueError(f"unknown mutation verification status: {self.status!r}")


@dataclass(frozen=True, slots=True)
class ToolOutput:
    text: str
    is_error: bool = False
    status: str = "ok"


def coerce_output(output: str | ToolOutput, limit: int) -> tuple[str, bool]:
    if isinstance(output, ToolOutput):
        return truncate_text(output.text, limit), output.is_error
    return truncate_text(output, limit), False


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    max_output: int = 20000
    timeout: float = 120.0
    run_id: str | None = None
    owner_id: str = "local"
    session_id: str | None = None
    run_store: Any | None = None
    max_child_iterations: int | None = None
    event_sink: Callable[[str, dict[str, Any]], None] | None = None
    workspaces: WorkspaceManager | None = None
    envelope: ExecutionEnvelope | None = None
    network_policy: NetworkPolicyEngine | None = None
    proxy_url: str = ""
    deadline_at: float | None = None
    research_engine: ResearchEngine | None = None
    budget_tracker: BudgetTracker | None = None
    child_budget_caps: ChildBudgetCaps | None = None
    mission_id: str | None = None
    task_id: str | None = None
    artifacts: ArtifactProvider | None = None

    def remaining_seconds(self, now: float | None = None) -> float | None:
        if self.deadline_at is None:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, self.deadline_at - current)


def effective_tool_timeout(tool: Tool, context: ToolContext) -> float:
    limit = tool.timeout_seconds if tool.timeout_seconds is not None else context.timeout
    remaining = context.remaining_seconds()
    if remaining is not None:
        limit = min(limit, remaining)
    return max(0.0, limit)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    mutates: bool = False
    requires_approval: bool = False
    parallel_safe: bool = True
    interactive: bool = False
    path_keys: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    verify_mutation: MutationVerifier | None = None
    output_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * HEAD_SHARE)
    tail = limit - head
    removed = len(text) - limit
    ending = text[-tail:] if tail else ""
    return f"{text[:head]}\n... [{removed} characters truncated] ...\n{ending}"


def _error(call: ToolCall, message: str, limit: int) -> ToolResult:
    return ToolResult(call.id, call.name, truncate_text(f"error: {message}", limit), True)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for item in tools:
            self.register(item)

    def register(self, tool: Tool) -> None:
        if not NAME_PATTERN.match(tool.name):
            raise ValueError(f"invalid tool name: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        mutates: bool = False,
        requires_approval: bool = False,
        parallel_safe: bool = True,
        interactive: bool = False,
        path_keys: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            self.register(
                Tool(
                    name,
                    description,
                    parameters,
                    handler,
                    mutates,
                    requires_approval,
                    parallel_safe,
                    interactive,
                    path_keys,
                    (),
                    timeout_seconds,
                    None,
                    output_schema,
                )
            )
            return handler

        return decorator

    def unregister(self, name: str) -> Tool | None:
        return self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec() for tool in self.all())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        limit = context.max_output
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(self.names()) or "none"
            return _error(call, f"unknown tool '{call.name}'. available tools: {known}", limit)
        if INVALID_ARGUMENTS_KEY in call.arguments:
            return _error(call, "arguments are not valid JSON, send a JSON object", limit)
        problems = validate_arguments(tool.parameters, call.arguments)
        if problems:
            return _error(call, "invalid arguments: " + "; ".join(problems), limit)
        timeout = effective_tool_timeout(tool, context)
        try:
            output = await asyncio.wait_for(
                tool.handler(dict(call.arguments), context), timeout=timeout
            )
        except ToolError as exc:
            return _error(call, str(exc), limit)
        except TimeoutError:
            return _error(call, f"timed out after {timeout:g}s", limit)
        except Exception as exc:
            return _error(call, f"tool crashed: {type(exc).__name__}: {exc}", limit)
        text, is_error = coerce_output(output, limit)
        if tool.output_schema is not None and not is_error:
            problems = validate_tool_output(tool.output_schema, text)
            if problems:
                return _error(
                    call, "tool output violated its declared schema: " + "; ".join(problems), limit
                )
        return ToolResult(call.id, call.name, text, is_error)
