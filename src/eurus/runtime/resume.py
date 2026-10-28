from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from eurus.models import Message
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine, RunResult
from eurus.runtime.models import TERMINAL_STATUSES, Run, RunStatus, RuntimeEvent
from eurus.runtime.recovery import RecoveryAction, classify_recovery
from eurus.runtime.state_machine import transition_run
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore

log = logging.getLogger(__name__)

EngineFactory = Callable[[Run], AgentEngine | Awaitable[AgentEngine]]

RESUMPTION_HEADER = "[Resumption context from the durable runtime]"
MAX_EVENTS_SUMMARY = 40
MAX_RESTORED_HISTORY = 200


class RecoveryBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResumePlan:
    run: Run
    action: RecoveryAction
    checkpoint: dict[str, Any] | None = None
    uncertain_attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def build_resumption_directive(
    run: Run,
    checkpoint: dict[str, Any] | None,
    events: Sequence[RuntimeEvent],
) -> str:
    lines = [RESUMPTION_HEADER]
    state = checkpoint.get("state") if checkpoint else None
    state = state if isinstance(state, dict) else {}
    iteration = state.get("iteration")
    if isinstance(iteration, int) and iteration > 0:
        lines.append(
            f"A previous execution of this objective already reached iteration {iteration}."
        )
    task_state = state.get("task_state")
    if isinstance(task_state, dict):
        raw_steps = task_state.get("steps")
        if isinstance(raw_steps, list):
            completed = [
                str(item.get("title"))
                for item in raw_steps
                if isinstance(item, dict) and item.get("status") == "succeeded"
            ]
            if completed:
                lines.append(
                    "Plan steps already completed and verified: "
                    + "; ".join(completed[:MAX_EVENTS_SUMMARY])
                    + "."
                )
    finished = [item for item in events if item.event_type == "tool.call.finished"]
    successful_tools = [item for item in finished if not item.payload.get("is_error")]
    if successful_tools:
        names = [str(item.payload.get("tool")) for item in successful_tools[-MAX_EVENTS_SUMMARY:]]
        lines.append("Tool calls that already completed successfully: " + ", ".join(names) + ".")
    failed_tools = [item for item in finished if item.payload.get("is_error")]
    if failed_tools:
        names = [str(item.payload.get("tool")) for item in failed_tools[-MAX_EVENTS_SUMMARY:]]
        lines.append("Tool calls that previously failed: " + ", ".join(names) + ".")
    plans = [item for item in events if item.event_type == "plan.updated"]
    if plans:
        summary = plans[-1].payload.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Last recorded plan summary: {summary.strip()[:400]}")
    recoveries = [item for item in events if item.event_type == "recovery.attempted"]
    if recoveries:
        reasons = [str(item.payload.get("reason"))[:120] for item in recoveries[-5:]]
        lines.append("Previous recovery attempts: " + "; ".join(reasons))
    lines.append(
        "Do not blindly repeat completed work or completed external side effects; verify "
        "current state first and continue from where the previous execution stopped."
    )
    return "\n".join(lines)


def _seed_agent_history(agent: object, restored: Sequence[Message]) -> bool:
    """Restore a checkpointed conversation onto the agent before resuming.

    A pristine agent (only its system prompt) gets the restored conversation in
    place; an agent that already carries context gets the non-system messages
    appended. Corrupt entries are skipped; the agent loop's ``repair_history``
    fixes any tool-call pairing gaps afterwards.
    """
    history = getattr(agent, "history", None)
    if not isinstance(history, list):
        return False
    messages: list[Message] = []
    for item in restored:
        if isinstance(item, Message):
            messages.append(item)
    non_system = [message for message in messages if message.role != "system"]
    if not non_system:
        return False
    if len(history) <= 1 and history and history[0].role == "system":
        history[:] = [history[0], *non_system]
        log.info("resumed run restored %d checkpointed messages", len(non_system))
        return True
    history.extend(non_system)
    log.info("resumed run appended %d checkpointed messages", len(non_system))
    return True


def _restored_history(state: dict[str, Any]) -> list[Message]:
    raw = state.get("history")
    if not isinstance(raw, list):
        return []
    messages: list[Message] = []
    for item in raw[-MAX_RESTORED_HISTORY:]:
        if not isinstance(item, dict):
            continue
        try:
            messages.append(Message.from_dict(item))
        except (KeyError, TypeError, ValueError):
            log.debug("skipping corrupt checkpointed message", exc_info=True)
    return messages


class RecoveryManager:
    def __init__(
        self,
        run_store: RunStore,
        attempt_store: ToolAttemptStore | None = None,
        *,
        event_store: EventStore | None = None,
    ) -> None:
        self.run_store = run_store
        self.attempt_store = attempt_store or ToolAttemptStore(run_store.database)
        self.event_store = event_store or EventStore(run_store.database)

    def inspect(self, run_id: str) -> ResumePlan:
        run = self.run_store.get(run_id)
        if run is None:
            raise KeyError(f"run {run_id} does not exist")
        checkpoint = self.run_store.latest_checkpoint(run_id)
        uncertain = tuple(self.attempt_store.uncertain_keys(run_id))
        action = classify_recovery(run, checkpoint)
        if action is RecoveryAction.RESUME_CHECKPOINT and uncertain:
            action = RecoveryAction.RECONCILE_UNKNOWN_OUTCOME
        if action is RecoveryAction.START_FRESH and uncertain:
            action = RecoveryAction.RECONCILE_UNKNOWN_OUTCOME
        return ResumePlan(
            run=run,
            action=action,
            checkpoint=checkpoint,
            uncertain_attempts=uncertain,
        )

    def resumption_directive(self, run_id: str) -> str | None:
        run = self.run_store.get(run_id)
        if run is None:
            return None
        checkpoint = self.run_store.latest_checkpoint(run_id)
        events = self.event_store.list_for_run(run_id, limit=200)
        directive = build_resumption_directive(run, checkpoint, events)
        if len(directive.splitlines()) <= 2:
            return None
        return directive

    async def resume(
        self,
        run_id: str,
        engine_factory: EngineFactory,
        *,
        budget: BudgetSpec | None = None,
    ) -> RunResult:
        plan = self.inspect(run_id)
        run = plan.run
        if plan.action is RecoveryAction.TERMINAL:
            raise RecoveryBlocked(f"run is terminal ({run.status.value}); start a new run instead")
        if plan.action is RecoveryAction.RECONCILE_UNKNOWN_OUTCOME:
            details = ", ".join(
                f"{item['tool_name']}({item['attempt_id'][:8]})" for item in plan.uncertain_attempts
            )
            raise RecoveryBlocked(
                "resume blocked: unresolved tool side effects require reconciliation "
                f"before replay: {details or 'unknown outcome recorded'}"
            )
        if plan.action is RecoveryAction.MANUAL_REVIEW:
            raise RecoveryBlocked(
                "resume blocked: no usable checkpoint exists for this run; "
                "manual review is required"
            )
        if run.status is RunStatus.RUNNING:
            transition_run(run, RunStatus.INTERRUPTED, stop_reason="resume_recovery")
            self.run_store.save(run, expected_status_version=run.status_version - 1)
        factory_result = engine_factory(run)
        engine: AgentEngine = (
            await factory_result if isinstance(factory_result, Awaitable) else factory_result
        )
        state = plan.checkpoint.get("state") if plan.checkpoint else None
        if isinstance(state, dict):
            restored = _restored_history(state)
            if restored:
                _seed_agent_history(engine.agent, restored)
        transition_run(run, RunStatus.QUEUED)
        run.stop_reason = None
        self.run_store.save(run, expected_status_version=run.status_version - 1)
        directive = self.resumption_directive(run_id)
        if budget is not None:
            return await engine.run(
                run.objective, run=run, budget=budget, resumption_note=directive
            )
        return await engine.run(run.objective, run=run, resumption_note=directive)

    def resume_checkpoint_state(self, run_id: str) -> dict[str, Any] | None:
        checkpoint = self.run_store.latest_checkpoint(run_id)
        if checkpoint is None:
            return None
        state = checkpoint.get("state")
        return state if isinstance(state, dict) else None


def is_terminal(run: Run) -> bool:
    return run.status in TERMINAL_STATUSES
