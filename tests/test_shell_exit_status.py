from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from eurus.agent import Agent
from eurus.execution.host import HostSandbox
from eurus.execution.sandbox import ExecutionRequest, ExecutionResult
from eurus.models import ToolCall
from eurus.providers.testing import openai_stream, scripted_router
from eurus.runtime.contracts import ToolExecutionOutcome
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary
from eurus.tools.shell import shell_tool

EMPTY = {"type": "object", "properties": {}}


class FakeSandbox:
    isolated = False

    def __init__(self, results: list[ExecutionResult]) -> None:
        self.results = list(results)
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return self.results.pop(0)


def make_context(timeout: float = 5.0, max_output: int = 1000) -> ToolContext:
    return ToolContext(workspace=Path("."), max_output=max_output, timeout=timeout)


async def approve(call: ToolCall, tool: Tool) -> bool:
    return True


async def run_boundary(
    tool: Tool, arguments: dict[str, Any], ctx: ToolContext | None = None
) -> ToolExecutionOutcome:
    boundary = ToolExecutionBoundary(ToolRegistry([tool]), ctx or make_context(), approver=approve)
    outcome = await boundary.execute(ToolCall("c1", tool.name, arguments))
    assert isinstance(outcome, ToolExecutionOutcome)
    return outcome


def test_exit_nonzero_is_reported_as_error() -> None:
    sandbox = FakeSandbox([ExecutionResult(stdout="", stderr="boom", exit_code=1)])
    tool = shell_tool(sandbox)

    outcome = asyncio.run(run_boundary(tool, {"command": "false"}))

    assert outcome.is_error is True
    assert outcome.status.value == "failed"
    assert "exit_code: 1" in outcome.output
    assert "boom" in outcome.output


def test_exit_zero_is_success() -> None:
    sandbox = FakeSandbox([ExecutionResult(stdout="ok", stderr="", exit_code=0)])
    tool = shell_tool(sandbox)

    outcome = asyncio.run(run_boundary(tool, {"command": "true"}))

    assert outcome.is_error is False
    assert outcome.status.value == "success"
    assert "exit_code: 0" in outcome.output


def test_timed_out_is_timeout_and_uncertain_for_mutating_shell() -> None:
    sandbox = FakeSandbox([ExecutionResult(stdout="", stderr="", exit_code=124, timed_out=True)])
    tool = shell_tool(sandbox)

    outcome = asyncio.run(run_boundary(tool, {"command": "sleep 999"}))

    assert outcome.is_error is True
    assert outcome.status.value == "timeout"
    assert outcome.uncertain is True
    assert "timed_out: true" in outcome.output


def test_host_sandbox_timeout_is_timeout_and_uncertain() -> None:
    sandbox = HostSandbox(Path("."))
    tool = shell_tool(sandbox)

    outcome = asyncio.run(
        run_boundary(tool, {"command": "sleep 5"}, make_context(timeout=0.2, max_output=4000))
    )

    assert outcome.is_error is True
    assert outcome.status.value == "timeout"
    assert outcome.uncertain is True


def test_tool_output_success_starting_with_error_prefix_is_not_error() -> None:
    from eurus.tools.base import ToolOutput

    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput("error: just a prefix of a successful payload", is_error=False)

    tool = Tool("probe", "probe", EMPTY, handler)

    outcome = asyncio.run(run_boundary(tool, {}))

    assert outcome.is_error is False
    assert outcome.status.value == "success"
    assert outcome.output.startswith("error:")


def test_string_handler_error_prefix_still_flags_error() -> None:
    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> str:
        return "error: boom"

    tool = Tool("probe", "probe", EMPTY, handler)

    outcome = asyncio.run(run_boundary(tool, {}))

    assert outcome.is_error is True
    assert outcome.status.value == "failed"


def test_shell_failure_increments_agent_failure_counter() -> None:
    from eurus.agent.loop import call_key

    sandbox = FakeSandbox([ExecutionResult(stdout="", stderr="denied", exit_code=1)])
    registry = ToolRegistry([shell_tool(sandbox)])
    streams = [
        openai_stream("", [("c1", "shell", {"command": "false"})]),
        openai_stream("gave up"),
    ]
    router, _ = scripted_router(streams=streams)
    agent = Agent(
        router,
        registry,
        make_context(),
        system_prompt="sys",
        max_steps=5,
        approver=approve,
    )

    async def go() -> list[str]:
        return [event.stop_reason async for event in agent.run("run false")]

    reasons = asyncio.run(go())

    assert reasons[-1] == "completed"
    key = call_key(ToolCall("c1", "shell", {"command": "false"}))
    counts = agent.failure_counts
    assert counts.get(key, (0, ""))[0] >= 1
