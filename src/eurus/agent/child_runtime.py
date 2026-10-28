from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eurus.agent.events import AgentEvent
from eurus.agent.loop import Agent
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.contracts import VerificationStatus
from eurus.runtime.engine import AgentEngine, RunResult
from eurus.runtime.models import Run
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import ToolContext

CHILD_TOOL_CALL_RATIO = 8
MAX_CHILD_TOOL_CALLS = 400


@dataclass(frozen=True, slots=True)
class ChildRunReport:
    output: str
    stop_reason: str
    status: str
    verified: bool
    iterations_used: int
    child_run_id: str | None
    events: tuple[AgentEvent, ...]
    durable: bool


def build_child_run(
    context: ToolContext,
    *,
    objective: str,
    parent_run_id: str | None,
    owner_id: str,
    session_id: str | None,
    budget: dict[str, Any],
) -> Run | None:
    run_store = context.run_store
    if run_store is None or parent_run_id is None:
        return None
    if not isinstance(run_store, RunStore):
        return None
    child = Run(
        owner_id=owner_id,
        objective=objective,
        parent_run_id=parent_run_id,
        session_id=session_id,
        budget=budget,
    )
    try:
        run_store.create(child)
    except Exception:
        return None
    return child


def child_budget_spec(
    *,
    max_iterations: int,
    timeout_seconds: float,
    deadline_seconds: float | None = None,
) -> BudgetSpec:
    runtime = (
        timeout_seconds if deadline_seconds is None else min(timeout_seconds, deadline_seconds)
    )
    return BudgetSpec(
        max_iterations=max_iterations,
        max_tool_calls=min(max_iterations * CHILD_TOOL_CALL_RATIO, MAX_CHILD_TOOL_CALLS),
        max_runtime_seconds=runtime,
        max_child_runs=0,
        max_delegation_depth=0,
    )


async def execute_child_agent(
    agent: Agent,
    context: ToolContext,
    objective: str,
    child_run: Run | None,
    *,
    spec: BudgetSpec,
) -> ChildRunReport:
    if child_run is not None and context.run_store is not None:
        run_store = context.run_store
        event_store = EventStore(run_store.database)
        attempt_store = ToolAttemptStore(run_store.database)
        engine = AgentEngine(
            agent,
            run_store=run_store,
            event_store=event_store,
            attempt_store=attempt_store,
        )
        result: RunResult = await engine.run(objective, run=child_run, budget=spec)
        verified = result.verification is not None and (
            result.verification.status is VerificationStatus.VERIFIED
        )
        return ChildRunReport(
            output=result.output,
            stop_reason=result.run.stop_reason or "unknown",
            status=result.run.status.value,
            verified=verified,
            iterations_used=agent.iteration_budget.used,
            child_run_id=child_run.run_id,
            events=result.events,
            durable=True,
        )
    output: list[str] = []
    stop_reason = "unknown"
    async for event in agent.run(objective):
        if event.kind == "text" and event.text:
            output.append(event.text)
        elif event.kind == "text_reset":
            output.clear()
        elif event.kind == "done":
            stop_reason = event.stop_reason
            if event.text:
                output.append(event.text)
    return ChildRunReport(
        output="\n".join(part for part in output if part).strip(),
        stop_reason=stop_reason,
        status="ephemeral",
        verified=False,
        iterations_used=agent.iteration_budget.used,
        child_run_id=None,
        events=(),
        durable=False,
    )
