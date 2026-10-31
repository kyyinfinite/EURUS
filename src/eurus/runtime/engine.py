from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from eurus.agent.events import AgentEvent
from eurus.agent.loop import Agent
from eurus.coding.checkpoint import CheckpointStore
from eurus.runtime.budgets import BudgetSpec, BudgetTracker
from eurus.runtime.cancellation import CancellationToken
from eurus.runtime.contracts import LoopDecision, VerificationOutcome, VerificationStatus
from eurus.runtime.instrumentation import span
from eurus.runtime.models import Run, RunStatus, RuntimeEvent
from eurus.runtime.pricing import PricingCatalog
from eurus.runtime.run_state import (
    AttemptEvidence,
    CheckpointInfo,
    render_run_state,
)
from eurus.runtime.state_machine import transition_run
from eurus.runtime.token_governor import TokenGovernor
from eurus.security.redaction import redact_text, summarize_arguments
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore

Verifier = Callable[[str, str], Awaitable[bool]]
EventSink = Callable[[AgentEvent], object]

log = logging.getLogger(__name__)

MAX_CHECKPOINT_HISTORY_MESSAGES = 200
DEFAULT_MAX_AGENT_EVENTS = 2000
DEFAULT_MAX_RUNTIME_EVENTS = 5000
DEFAULT_CHECKPOINT_INTERVAL_STEPS = 4
DEFAULT_CHECKPOINT_KEEP = 10


def _remove_attr(agent: object, name: str) -> None:
    """Remove an attribute the engine had to shim onto a duck-typed agent."""
    if hasattr(agent, name):
        delattr(agent, name)


def _restore_limit(budget: Any, limit: int) -> None:
    """Restore an iteration budget limit captured before a run."""
    budget.limit = limit


@dataclass
class _RunFlow:
    """Mutable per-run state shared by the engine event handlers."""

    tracker: BudgetTracker
    governor: TokenGovernor
    spec: BudgetSpec
    output_parts: list[str]
    budget_stop_requested: bool = False
    steps_since_checkpoint: int = 0
    plan_sequence: int = 0
    checkpoint_iteration: int | None = None
    side_effect_status: str = "settled"
    uncertain_mutation: bool = False
    attempt_failures: list[tuple[str, str]] = field(default_factory=list)
    run_state_cache: str | None = None


_RecordEvent = Callable[..., None]
_SaveCheckpoint = Callable[[dict[str, object]], Awaitable[None]]


def _last_step(events: Sequence[AgentEvent]) -> int:
    for event in reversed(events):
        if event.kind == "step":
            return event.step
    return 0


def _current_status(run: Run) -> RunStatus:
    return run.status


def output_digest(output: str) -> str:
    return hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RunResult:
    run: Run
    events: tuple[AgentEvent, ...]
    output: str
    verified: bool | None = None
    runtime_events: tuple[RuntimeEvent, ...] = ()
    verification: VerificationOutcome | None = None
    cost_estimate: float | None = None
    cost_known: bool = False
    events_dropped: int = 0
    runtime_events_dropped: int = 0

    @property
    def verification_status(self) -> VerificationStatus:
        if self.verification is None:
            return VerificationStatus.NOT_RUN
        return self.verification.status


class AgentEngine:
    def __init__(
        self,
        agent: Agent,
        *,
        verifier: Verifier | None = None,
        run_store: RunStore | None = None,
        event_store: EventStore | None = None,
        attempt_store: ToolAttemptStore | None = None,
        checkpoint_interval_steps: int = DEFAULT_CHECKPOINT_INTERVAL_STEPS,
        pricing: PricingCatalog | None = None,
        max_agent_events: int = DEFAULT_MAX_AGENT_EVENTS,
        max_runtime_events: int = DEFAULT_MAX_RUNTIME_EVENTS,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_keep: int | None = DEFAULT_CHECKPOINT_KEEP,
    ) -> None:
        self.agent = agent
        self.verifier = verifier
        if run_store is None and event_store is not None:
            run_store = RunStore(event_store.database)
        self.run_store = run_store
        self.event_store = event_store
        if attempt_store is None and run_store is not None:
            attempt_store = ToolAttemptStore(run_store.database)
        self.attempt_store = attempt_store
        if checkpoint_interval_steps < 1:
            raise ValueError("checkpoint_interval_steps must be at least 1")
        self.checkpoint_interval_steps = checkpoint_interval_steps
        if checkpoint_keep is not None and checkpoint_keep < 1:
            raise ValueError("checkpoint_keep must be at least 1 when set")
        self.checkpoint_keep = checkpoint_keep
        if max_agent_events < 1:
            raise ValueError("max_agent_events must be at least 1")
        if max_runtime_events < 1:
            raise ValueError("max_runtime_events must be at least 1")
        self.max_agent_events = max_agent_events
        self.max_runtime_events = max_runtime_events
        self.pricing = pricing
        self.checkpoint_store = checkpoint_store
        self._sequences: dict[str, int] = {}

    def _event(
        self,
        run: Run,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> RuntimeEvent:
        previous_sequence = self._sequences.get(run.run_id, 0)
        if self.event_store is not None:
            previous_sequence = max(previous_sequence, self.event_store.last_sequence(run.run_id))
        sequence = previous_sequence + 1
        self._sequences[run.run_id] = sequence
        return RuntimeEvent(
            run_id=run.run_id,
            event_type=event_type,
            sequence_no=sequence,
            payload=payload or {},
            parent_run_id=run.parent_run_id,
            step_id=step_id,
            attempt_id=attempt_id,
        )

    def _bind_run_context(self, run: Run, spec: BudgetSpec) -> None:
        if not hasattr(self.agent, "context"):
            return
        context = self.agent.context
        child_allowance: int | None = min(spec.max_iterations, 50) if spec.max_child_runs > 0 else 0
        current = getattr(context, "run_id", None)
        if current == run.run_id and getattr(context, "run_store", None) is self.run_store:
            return
        self.agent.context = replace(
            context,
            run_id=run.run_id,
            owner_id=run.owner_id,
            session_id=run.session_id,
            run_store=self.run_store,
            max_child_iterations=child_allowance,
        )

    def _attach_context_event_sink(self, record_event: Callable[..., None]) -> None:
        if not hasattr(self.agent, "context") or not hasattr(self.agent.context, "event_sink"):
            return

        def emit(event_type: str, payload: dict[str, object]) -> None:
            try:
                record_event(event_type, dict(payload))
            except Exception:
                log.debug("context event sink failed", exc_info=True)

        self.agent.context = replace(self.agent.context, event_sink=emit)

    def _attach_boundary_events(self, record_event: Callable[..., None]) -> None:
        boundary = getattr(self.agent, "boundary", None)
        if boundary is None or getattr(boundary, "event_recorder", None) is not None:
            return

        async def forward(event_type: str, payload: dict[str, object]) -> None:
            record_event(event_type, dict(payload))

        boundary.event_recorder = forward

    def _attach_run_state_provider(self, run: Run, spec: BudgetSpec, flow: _RunFlow) -> None:
        if not hasattr(self.agent, "set_run_state_provider"):
            return
        agent = self.agent

        def state_line() -> str:
            cached = flow.run_state_cache
            if cached is not None:
                return cached
            text = render_run_state(
                run,
                spec,
                agent,
                tracker=agent.runtime_budget_tracker,
                attempt_evidence=AttemptEvidence(
                    uncertain_mutation=flow.uncertain_mutation,
                    failures=tuple(flow.attempt_failures),
                ),
                checkpoint_info=CheckpointInfo(
                    iteration=flow.checkpoint_iteration,
                    side_effect_status=flow.side_effect_status,
                ),
            )
            flow.run_state_cache = text
            return text

        agent.set_run_state_provider(state_line)

    def _seed_attempt_evidence(self, run_id: str, flow: _RunFlow) -> None:
        """One-time read so resumed runs show evidence from a previous execution."""
        if self.attempt_store is None:
            return
        try:
            if self.attempt_store.has_uncertain_mutation(run_id):
                flow.uncertain_mutation = True
            rows = self.attempt_store.list_for_run(run_id, limit=12)
            flow.attempt_failures = [
                (str(item.get("tool_name", "unknown")), str(item.get("status")))
                for item in rows
                if item.get("status") in {"failed", "timeout", "cancelled", "uncertain"}
            ]
        except Exception:
            log.debug("attempt evidence seed failed", exc_info=True)

    def _detach_run_state_provider(self) -> None:
        if hasattr(self.agent, "set_run_state_provider"):
            self.agent.set_run_state_provider(None)

    async def _reconcile_sandbox_orphans(self) -> None:
        sandbox = getattr(self.agent, "sandbox", None)
        reconcile = getattr(sandbox, "reconcile_orphans", None)
        if reconcile is None:
            return
        try:
            await reconcile()
        except Exception:
            log.debug("sandbox orphan reconciliation failed", exc_info=True)

    async def run(
        self,
        objective: str,
        *,
        owner_id: str = "local",
        run: Run | None = None,
        budget: BudgetSpec | None = None,
        cancellation: CancellationToken | None = None,
        success_criteria: str | None = None,
        session_id: str | None = None,
        on_event: EventSink | None = None,
        resumption_note: str | None = None,
    ) -> RunResult:
        spec = budget or BudgetSpec()
        runtime_run = run or Run(owner_id=owner_id, objective=objective, budget=asdict(spec))
        if session_id is not None and not runtime_run.session_id:
            runtime_run.session_id = session_id
        objective_text = runtime_run.objective
        if resumption_note is not None and resumption_note.strip():
            objective_text = f"{runtime_run.objective}\n\n{resumption_note.strip()}"
        self._ensure_startable_run(runtime_run)
        tracker = BudgetTracker(spec, catalog=self.pricing)
        tracker.start()
        flow = _RunFlow(
            tracker=tracker,
            governor=TokenGovernor(),
            spec=spec,
            output_parts=[],
        )
        agent_events: deque[AgentEvent] = deque(maxlen=self.max_agent_events)
        agent_events_total = 0
        runtime_events: deque[RuntimeEvent] = deque(maxlen=self.max_runtime_events)
        runtime_events_total = 0
        self._bind_run_context(runtime_run, spec)
        self.agent.run_id = runtime_run.run_id
        await self._reconcile_sandbox_orphans()
        self._seed_attempt_evidence(runtime_run.run_id, flow)
        self._attach_boundary_recorders(runtime_run.run_id, flow)

        def persist_run() -> None:
            if self.run_store is not None:
                self.run_store.save(
                    runtime_run,
                    expected_status_version=runtime_run.status_version - 1,
                )

        def record_event(
            event_type: str,
            payload: dict[str, object] | None = None,
            *,
            step_id: str | None = None,
            attempt_id: str | None = None,
        ) -> None:
            nonlocal runtime_events_total
            with span("event_write"):
                event = self._event(
                    runtime_run, event_type, payload, step_id=step_id, attempt_id=attempt_id
                )
                if self.event_store is not None:
                    self.event_store.append(event)
            runtime_events.append(event)
            runtime_events_total += 1

        async def save_checkpoint(state: dict[str, object]) -> None:
            if self.run_store is None:
                return
            try:
                with span("checkpoint"):
                    enriched = await asyncio.to_thread(self._prepare_checkpoint_state, state)
                    await asyncio.to_thread(
                        self._persist_checkpoint, runtime_run.run_id, enriched, "runtime"
                    )
            except Exception as exc:
                log.warning("checkpoint save failed for run %s", runtime_run.run_id, exc_info=True)
                try:
                    record_event(
                        "checkpoint.failed",
                        {"error": f"{type(exc).__name__}: {exc}"[:400]},
                    )
                except Exception:
                    log.debug("checkpoint failure event could not be recorded", exc_info=True)
                return
            iteration_value = enriched.get("iteration")
            flow.checkpoint_iteration = (
                iteration_value if isinstance(iteration_value, int) else None
            )
            flow.side_effect_status = str(enriched.get("side_effect_status", "settled"))
            record_event(
                "checkpoint.saved",
                {
                    "iteration": enriched.get("iteration"),
                    "checkpoint_type": "runtime",
                    "side_effect_status": enriched.get("side_effect_status", "settled"),
                    "steps_done": self._completed_step_count(),
                },
            )

        async def save_light_checkpoint(state: dict[str, object]) -> None:
            if self.run_store is None:
                return
            try:
                with span("checkpoint.light"):
                    await asyncio.to_thread(
                        self._persist_checkpoint, runtime_run.run_id, dict(state), "light"
                    )
            except Exception:
                log.warning(
                    "light checkpoint save failed for run %s", runtime_run.run_id, exc_info=True
                )
                return
            iteration = state.get("iteration")
            if isinstance(iteration, int):
                flow.checkpoint_iteration = iteration
            flow.side_effect_status = str(state.get("side_effect_status", "settled"))

        self._attach_boundary_events(record_event)
        self._attach_context_event_sink(record_event)
        self._attach_run_state_provider(runtime_run, spec, flow)

        verified: bool | None = None
        verification = VerificationOutcome(status=VerificationStatus.NOT_RUN)
        cancellation_watcher: asyncio.Task[None] | None = None
        control_watcher: asyncio.Task[None] | None = None
        cancellation_token = cancellation
        if cancellation_token is None and self.run_store is not None:
            cancellation_token = CancellationToken()
        transition_run(runtime_run, RunStatus.RUNNING)
        persist_run()
        record_event("run.started", {"objective": runtime_run.objective})
        if cancellation_token is not None and cancellation_token.cancelled:
            transition_run(runtime_run, RunStatus.CANCELLED, stop_reason="cancelled_before_start")
            persist_run()
            record_event("run.cancelled")
            return RunResult(runtime_run, (), "", None, tuple(runtime_events), verification)
        if cancellation_token is not None:
            cancellation_watcher = asyncio.create_task(self._watch_cancellation(cancellation_token))
        if self.run_store is not None and cancellation_token is not None:
            control_watcher = asyncio.create_task(
                self._watch_persisted_cancellation(runtime_run.run_id, cancellation_token)
            )
        try:
            async with asyncio.timeout(self._effective_timeout(spec)):
                with self._temporary_agent_limits(spec, tracker):
                    async for event in self.agent.run(objective_text):
                        agent_events.append(event)
                        agent_events_total += 1
                        if on_event is not None:
                            emitted = on_event(event)
                            if inspect.isawaitable(emitted):
                                await emitted
                        if event.kind == "text":
                            flow.output_parts.append(event.text)
                        elif event.kind == "text_reset":
                            flow.output_parts.clear()
                        elif event.kind == "step":
                            flow.run_state_cache = None
                            await self._handle_step_event(
                                event,
                                flow,
                                runtime_run,
                                save_checkpoint,
                                save_light_checkpoint,
                            )
                        elif event.kind == "model_call" and not event.text:
                            self._handle_model_call_event(event, flow, record_event)
                        elif event.kind == "plan":
                            self._handle_plan_event(event, flow, record_event)
                        elif event.kind == "recovery":
                            self._handle_recovery_event(event, record_event)
                        elif event.kind == "tool_call" and event.tool_call is not None:
                            self._handle_tool_call_event(event, record_event)
                            if self._is_mutating_tool(event.tool_call.name):
                                await save_checkpoint(
                                    {
                                        "objective": runtime_run.objective,
                                        "iteration": event.step,
                                        "output_chars": sum(
                                            len(part) for part in flow.output_parts
                                        ),
                                        "tool_execution": "pending",
                                        "side_effect_status": "in_flight",
                                    }
                                )
                        elif event.kind == "tool_result" and event.tool_result is not None:
                            self._handle_tool_result_event(event, record_event)
                            if self._is_mutating_tool(event.tool_result.name):
                                await save_light_checkpoint(
                                    {
                                        "objective": runtime_run.objective,
                                        "iteration": event.step,
                                        "tool_execution": "settled",
                                        "side_effect_status": "settled",
                                    }
                                )
                        elif event.kind == "done":
                            verified, verification = await self._handle_done_event(
                                runtime_run=runtime_run,
                                event=event,
                                flow=flow,
                                agent_events=agent_events,
                                cancellation_token=cancellation_token,
                                success_criteria=success_criteria,
                                persist_run=persist_run,
                                record_event=record_event,
                                save_checkpoint=save_checkpoint,
                            )
        except TimeoutError:
            if runtime_run.status in {RunStatus.RUNNING, RunStatus.VERIFYING}:
                transition_run(
                    runtime_run, RunStatus.FAILED, stop_reason="runtime_budget_exhausted"
                )
                persist_run()
                record_event("budget.exhausted", {"stop_reason": "runtime_budget_exhausted"})
        except asyncio.CancelledError:
            if runtime_run.status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                transition_run(runtime_run, RunStatus.CANCELLED, stop_reason="cancelled")
                persist_run()
            record_event("run.cancelled")
            raise
        except Exception as exc:
            if runtime_run.status not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                transition_run(
                    runtime_run, RunStatus.FAILED, stop_reason=f"engine_error:{type(exc).__name__}"
                )
                persist_run()
                record_event("run.failed", {"stop_reason": runtime_run.stop_reason})
            raise
        finally:
            await self._stop_watchers(cancellation_watcher, control_watcher)
            self._detach_run_state_provider()
            self._finalize_checkpoint(runtime_run.run_id)
        if runtime_run.status in {RunStatus.RUNNING, RunStatus.VERIFYING}:
            transition_run(
                runtime_run, RunStatus.FAILED, stop_reason="agent_ended_without_terminal_event"
            )
            persist_run()
        self._record_terminal_event(runtime_run, verification, record_event)
        cost_known = tracker.cost_known and tracker.model_calls_used > 0
        if spec.max_cost is not None and not cost_known:
            record_event(
                "cost.tracking_unavailable",
                {
                    "reason": "usage or pricing information is missing for this run",
                    "models": list(tracker.unpriced_models[:5]),
                    "model_calls_observed": tracker.model_calls_used,
                    "max_cost": spec.max_cost,
                },
            )
        record_event(
            "budget.snapshot", {**tracker.snapshot(), "governor": flow.governor.snapshot()}
        )
        return RunResult(
            runtime_run,
            tuple(agent_events),
            "".join(flow.output_parts),
            verified,
            tuple(runtime_events),
            verification,
            cost_estimate=round(tracker.cost_used, 6) if cost_known else None,
            cost_known=cost_known,
            events_dropped=max(0, agent_events_total - len(agent_events)),
            runtime_events_dropped=max(0, runtime_events_total - len(runtime_events)),
        )

    def _ensure_startable_run(self, runtime_run: Run) -> None:
        """Validate or create the durable run record before the engine starts it."""
        if self.run_store is not None:
            stored_run = self.run_store.get(runtime_run.run_id)
            if stored_run is None:
                self.run_store.create(runtime_run)
            else:
                if stored_run.status != RunStatus.QUEUED:
                    raise ValueError("AgentEngine only starts queued runs")
                if stored_run.status_version != runtime_run.status_version:
                    raise ValueError("run state is stale compared with durable storage")
                if (
                    stored_run.owner_id != runtime_run.owner_id
                    or stored_run.objective != runtime_run.objective
                ):
                    raise ValueError("run identity differs from durable storage")
        if runtime_run.status != RunStatus.QUEUED:
            raise ValueError("AgentEngine only starts queued runs")

    @contextmanager
    def _temporary_agent_limits(self, spec: BudgetSpec, tracker: BudgetTracker) -> Iterator[None]:
        """Apply run-budget limits to the agent and restore them exactly on exit.

        The engine duck-types optional Agent attributes: every attribute it
        rewrites is snapshotted first and restored on exit (deleted again when
        it did not exist before), so no budget override leaks into later runs.
        """
        missing = object()
        agent = self.agent
        restores: list[Callable[[], None]] = []

        def override(name: str, value: object) -> None:
            if not hasattr(agent, name):
                return
            previous: object = getattr(agent, name, missing)
            if previous is missing:
                restores.append(partial(_remove_attr, agent, name))
            else:
                restores.append(partial(setattr, agent, name, previous))
            setattr(agent, name, value)

        if getattr(agent, "max_parallel_tools", None) is not None:
            override("max_parallel_tools", spec.max_parallel_tools)
        iteration_budget = getattr(agent, "iteration_budget", None)
        if iteration_budget is not None and getattr(iteration_budget, "limit", None) is not None:
            original_limit = iteration_budget.limit
            restores.append(partial(_restore_limit, iteration_budget, original_limit))
            iteration_budget.limit = min(original_limit, spec.max_iterations)
        previous_tracker: object = getattr(agent, "runtime_budget_tracker", missing)
        if previous_tracker is missing:
            restores.append(partial(_remove_attr, agent, "runtime_budget_tracker"))
        else:
            restores.append(partial(setattr, agent, "runtime_budget_tracker", previous_tracker))
        agent.runtime_budget_tracker = tracker
        if spec.max_model_calls is not None:
            previous_calls = getattr(agent, "max_model_calls", None)
            if previous_calls is not None:
                override(
                    "max_model_calls",
                    min(previous_calls or spec.max_model_calls, spec.max_model_calls),
                )
        if spec.deadline_at is not None and hasattr(agent, "deadline_at"):
            override("deadline_at", spec.deadline_at)
        elif not hasattr(agent, "deadline_at"):
            # Legacy shim: agents without a deadline attribute get a harmless
            # None that is removed again on exit.
            restores.append(partial(_remove_attr, agent, "deadline_at"))
            agent.deadline_at = None
        try:
            yield
        finally:
            for restore in reversed(restores):
                restore()

    async def _handle_step_event(
        self,
        event: AgentEvent,
        flow: _RunFlow,
        runtime_run: Run,
        save_checkpoint: _SaveCheckpoint,
        save_light_checkpoint: _SaveCheckpoint,
    ) -> None:
        flow.steps_since_checkpoint += 1
        if flow.steps_since_checkpoint < self.checkpoint_interval_steps:
            await save_light_checkpoint(
                {
                    "objective": runtime_run.objective,
                    "iteration": event.step,
                    "tool_execution": "settled",
                    "side_effect_status": "settled",
                }
            )
            return
        flow.steps_since_checkpoint = 0
        await save_checkpoint(
            {
                "objective": runtime_run.objective,
                "iteration": event.step,
                "output_chars": sum(len(part) for part in flow.output_parts),
                "tool_execution": "settled",
                "side_effect_status": "settled",
            }
        )

    def _handle_model_call_event(
        self, event: AgentEvent, flow: _RunFlow, record_event: _RecordEvent
    ) -> None:
        usage = event.usage
        if usage is not None and usage.total_tokens > 0:
            model_used = getattr(getattr(self.agent, "router", None), "last_used", None) or getattr(
                self.agent, "last_used", None
            )
            if model_used is not None and not isinstance(model_used, str):
                model_used = str(model_used)
            flow.tracker.add_model_usage(model_used, usage)
            flow.governor.observe(usage)
        if (
            usage is not None
            and flow.governor.should_winddown(
                tokens_used=flow.tracker.tokens_used,
                max_tokens=flow.spec.max_tokens,
                cost_used=flow.tracker.cost_used if flow.tracker.cost_known else None,
                max_cost=flow.spec.max_cost if flow.tracker.cost_known else None,
            )
            and hasattr(self.agent, "request_wrap_up")
        ):
            flow.governor.request_winddown()
            note = flow.governor.winddown_note(flow.tracker.tokens_used, flow.spec.max_tokens)
            self.agent.request_wrap_up(note)
            record_event(
                "token.winddown",
                {
                    "reason": "budget_headroom_low",
                    "tokens_used": flow.tracker.tokens_used,
                    "max_tokens": flow.spec.max_tokens,
                    "governor": flow.governor.snapshot(),
                },
            )
        record_event(
            "model.call.completed",
            {
                "finish": event.stop_reason,
                "total_tokens": usage.total_tokens if usage else 0,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
            },
            step_id=str(event.step) if event.step else None,
        )
        if flow.tracker.exhausted and not flow.budget_stop_requested:
            flow.budget_stop_requested = True
            record_event(
                "budget.exhausted",
                {
                    "stop_reason": "runtime_budget_exhausted",
                    "snapshot": flow.tracker.snapshot(),
                },
            )
            if hasattr(self.agent, "interrupt"):
                self.agent.interrupt()

    def _handle_plan_event(
        self, event: AgentEvent, flow: _RunFlow, record_event: _RecordEvent
    ) -> None:
        flow.plan_sequence += 1
        record_event(
            "plan.updated",
            {
                "step": event.step,
                "sequence": flow.plan_sequence,
                "is_replan": flow.plan_sequence > 1,
                "summary": event.text[:200],
            },
            step_id=str(event.step) if event.step else None,
        )

    def _handle_recovery_event(self, event: AgentEvent, record_event: _RecordEvent) -> None:
        record_event(
            "recovery.attempted",
            {"step": event.step, "reason": event.text[:200]},
            step_id=str(event.step) if event.step else None,
        )

    def _handle_tool_call_event(self, event: AgentEvent, record_event: _RecordEvent) -> None:
        if event.tool_call is None:  # pragma: no cover - guarded by caller
            return
        record_event(
            "tool.call.started",
            {
                "tool": event.tool_call.name,
                "arguments": summarize_arguments(event.tool_call.arguments)[:400],
            },
            step_id=str(event.step) if event.step else None,
            attempt_id=event.tool_call.id,
        )

    def _handle_tool_result_event(self, event: AgentEvent, record_event: _RecordEvent) -> None:
        if event.tool_result is None:  # pragma: no cover - guarded by caller
            return
        record_event(
            "tool.call.finished",
            {
                "tool": event.tool_result.name,
                "is_error": event.tool_result.is_error,
                "output_chars": len(event.tool_result.output),
                "preview": redact_text(event.tool_result.output[:200]),
            },
            step_id=str(event.step) if event.step else None,
            attempt_id=event.tool_result.call_id,
        )

    async def _handle_done_event(
        self,
        *,
        runtime_run: Run,
        event: AgentEvent,
        flow: _RunFlow,
        agent_events: Sequence[AgentEvent],
        cancellation_token: CancellationToken | None,
        success_criteria: str | None,
        persist_run: Callable[[], None],
        record_event: _RecordEvent,
        save_checkpoint: _SaveCheckpoint,
    ) -> tuple[bool | None, VerificationOutcome]:
        await save_checkpoint(
            {
                "objective": runtime_run.objective,
                "iteration": _last_step(agent_events),
                "output_chars": sum(len(part) for part in flow.output_parts),
                "stop_reason": event.stop_reason,
                "tool_execution": "settled",
                "side_effect_status": "settled",
            }
        )
        if cancellation_token is not None and cancellation_token.cancelled:
            transition_run(
                runtime_run,
                RunStatus.CANCELLED,
                stop_reason="operator_cancelled",
            )
            persist_run()
        elif event.stop_reason == "blocked":
            transition_run(
                runtime_run,
                RunStatus.WAITING_INPUT,
                stop_reason="blocked",
            )
            persist_run()
            record_event(
                "run.blocked",
                {
                    "stop_reason": "blocked",
                    "detail": "required authorization was repeatedly unavailable",
                },
            )
        elif event.stop_reason in {"interrupted", "timeout"}:
            if event.stop_reason == "interrupted" and flow.tracker.exhausted:
                transition_run(
                    runtime_run,
                    RunStatus.FAILED,
                    stop_reason="runtime_budget_exhausted",
                )
            else:
                target = (
                    RunStatus.INTERRUPTED
                    if event.stop_reason == "interrupted"
                    else RunStatus.FAILED
                )
                transition_run(runtime_run, target, stop_reason=event.stop_reason)
            persist_run()
        elif event.stop_reason in {
            "iteration_budget",
            "max_steps",
            "tool_budget",
            "model_call_budget",
            "deadline_exceeded",
            "no_progress",
            "budget_exhausted",
        }:
            reason = (
                "runtime_budget_exhausted"
                if event.stop_reason == "budget_exhausted"
                else event.stop_reason
            )
            transition_run(runtime_run, RunStatus.FAILED, stop_reason=reason)
            persist_run()
            record_event("budget.exhausted", {"stop_reason": reason})
        else:
            return await self._verify_and_complete(
                runtime_run=runtime_run,
                event=event,
                flow=flow,
                success_criteria=success_criteria,
                persist_run=persist_run,
                record_event=record_event,
            )
        return None, VerificationOutcome(status=VerificationStatus.NOT_RUN)

    async def _verify_and_complete(
        self,
        *,
        runtime_run: Run,
        event: AgentEvent,
        flow: _RunFlow,
        success_criteria: str | None,
        persist_run: Callable[[], None],
        record_event: _RecordEvent,
    ) -> tuple[bool | None, VerificationOutcome]:
        transition_run(runtime_run, RunStatus.VERIFYING)
        persist_run()
        final_output = "".join(flow.output_parts)
        if self.verifier is not None:
            passed = await self.verifier(success_criteria or runtime_run.objective, final_output)
            verified: bool | None = passed
            verification = VerificationOutcome(
                status=VerificationStatus.VERIFIED if passed else VerificationStatus.FAILED,
                summary="verifier accepted the result"
                if passed
                else "verifier rejected the result",
                evidence=(
                    f"stop_reason:{event.stop_reason}",
                    f"output_digest:{output_digest(final_output)}",
                ),
            )
        else:
            verified = None
            verification = VerificationOutcome(
                status=VerificationStatus.UNVERIFIED,
                summary="no verifier configured; completion is reported as unverified",
                evidence=(f"stop_reason:{event.stop_reason}",),
            )
            if event.stop_reason == "completed":
                decisions = tuple(getattr(self.agent, "decisions", ()) or ())
                if decisions and decisions[-1] is LoopDecision.VERIFIED_SUCCESS:
                    verified = True
                    verification = VerificationOutcome(
                        status=VerificationStatus.VERIFIED,
                        summary="agent loop completion verifier accepted the result",
                        evidence=(
                            f"stop_reason:{event.stop_reason}",
                            "source:agent_loop_verifier",
                        ),
                    )
        record_event("verification.completed", verification.to_payload())
        if verified:
            transition_run(runtime_run, RunStatus.COMPLETED, stop_reason=event.stop_reason)
        elif verified is None:
            transition_run(runtime_run, RunStatus.COMPLETED, stop_reason="completed_unverified")
        else:
            transition_run(runtime_run, RunStatus.FAILED, stop_reason="verification_failed")
        persist_run()
        return verified, verification

    def _record_terminal_event(
        self, runtime_run: Run, verification: VerificationOutcome, record_event: _RecordEvent
    ) -> None:
        status = runtime_run.status
        if status == RunStatus.INTERRUPTED:
            record_event("run.interrupted", {"stop_reason": runtime_run.stop_reason})
        elif status == RunStatus.COMPLETED:
            record_event(
                "run.completed",
                {
                    "stop_reason": runtime_run.stop_reason,
                    "verification": verification.status.value,
                },
            )
        elif status == RunStatus.FAILED:
            record_event("run.failed", {"stop_reason": runtime_run.stop_reason})
        elif status == RunStatus.CANCELLED:
            record_event("run.cancelled", {"stop_reason": runtime_run.stop_reason})

    @staticmethod
    async def _stop_watchers(*watchers: asyncio.Task[None] | None) -> None:
        tasks = [task for task in watchers if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _history_snapshot(self) -> list[dict[str, object]]:
        """Serialize the agent conversation for checkpoint durability (B2)."""
        history = getattr(self.agent, "history", None)
        if not isinstance(history, Sequence):
            return []
        snapshot: list[dict[str, object]] = []
        for message in list(history)[-MAX_CHECKPOINT_HISTORY_MESSAGES:]:
            to_dict = getattr(message, "to_dict", None)
            if not callable(to_dict):
                return []
            try:
                snapshot.append(to_dict())
            except Exception:
                log.debug("history snapshot failed", exc_info=True)
                return []
        return snapshot

    def _prepare_checkpoint_state(self, state: dict[str, object]) -> dict[str, object]:
        """Enrich a checkpoint payload off the event loop (serialization + validation)."""
        enriched = dict(state)
        task_state = self._task_state_snapshot()
        if task_state is not None:
            enriched["task_state"] = task_state
        history = self._history_snapshot()
        if history:
            enriched["history"] = history
        return enriched

    def _persist_checkpoint(
        self, run_id: str, state: dict[str, object], checkpoint_type: str
    ) -> int:
        assert self.run_store is not None, "checkpoint persistence requires a run store"
        version = self.run_store.save_checkpoint(run_id, state, checkpoint_type=checkpoint_type)
        if self.checkpoint_keep is not None:
            self.run_store.prune_checkpoints(run_id, checkpoint_type, self.checkpoint_keep)
        return version

    def _is_mutating_tool(self, name: str) -> bool:
        registry = getattr(self.agent, "registry", None)
        if registry is None:
            return False
        try:
            tool = registry.get(name)
        except Exception:
            return False
        return bool(getattr(tool, "mutates", False))

    def _task_state_snapshot(self) -> dict[str, object] | None:
        state = getattr(self.agent, "task_state", None)
        if state is None:
            return None
        try:
            snapshot = state.to_dict()
        except Exception:
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _completed_step_count(self) -> int:
        state = getattr(self.agent, "task_state", None)
        if state is None:
            return 0
        try:
            return sum(1 for step in state.steps if step.status == "succeeded")
        except Exception:
            return 0

    def _attach_boundary_recorders(self, run_id: str, flow: _RunFlow | None = None) -> None:
        boundary = getattr(self.agent, "boundary", None)
        if boundary is None or not hasattr(boundary, "outcome_recorder"):
            return
        attempt_store = self.attempt_store

        def note_evidence(tool_name: str, status: str, uncertain: bool) -> None:
            if flow is None:
                return
            if uncertain:
                flow.uncertain_mutation = True
            if status in {"failed", "timeout", "cancelled", "uncertain"}:
                flow.attempt_failures.append((tool_name, status))
                del flow.attempt_failures[:-12]

        async def record(outcome: object) -> None:
            from eurus.runtime.contracts import ToolExecutionOutcome

            if isinstance(outcome, ToolExecutionOutcome):
                persisted = replace(outcome, run_id=run_id)
                note_evidence(
                    persisted.tool_name,
                    str(getattr(persisted.status, "value", persisted.status)),
                    bool(persisted.uncertain),
                )
                if attempt_store is not None:
                    await attempt_store.record_async(persisted)

        boundary.outcome_recorder = record
        if hasattr(boundary, "attempt_started_recorder"):

            async def record_started(started: object) -> None:
                from eurus.tools.boundary import AttemptStarted

                if isinstance(started, AttemptStarted):
                    note_evidence(started.tool_name, "pending", False)
                    if attempt_store is not None:
                        await attempt_store.record_pending_async(
                            run_id=run_id if started.run_id is None else started.run_id,
                            tool_name=started.tool_name,
                            call_key=started.call_key,
                            attempt_id=started.attempt_id,
                            risk_level=started.risk_level,
                        )

            boundary.attempt_started_recorder = record_started
        if attempt_store is not None and hasattr(boundary, "uncertain_keys_provider"):

            def durable_uncertain_keys(active_run_id: str) -> list[str]:
                return [
                    str(row["call_key"])
                    for row in attempt_store.uncertain_keys(active_run_id)
                    if row.get("call_key")
                ]

            boundary.uncertain_keys_provider = durable_uncertain_keys
        if self.checkpoint_store is not None and hasattr(boundary, "before_mutation"):
            checkpoint_store = self.checkpoint_store

            async def capture_preimage(call: object, tool: object) -> None:
                from eurus.coding.checkpoint import mutation_targets
                from eurus.models import ToolCall as MutatingCall
                from eurus.tools.base import Tool as MutatingTool

                if not isinstance(call, MutatingCall) or not isinstance(tool, MutatingTool):
                    return
                workspace = getattr(getattr(self.agent, "context", None), "workspace", None)
                if workspace is None:
                    return
                targets = mutation_targets(call, tool, Path(workspace))
                if not targets:
                    return
                await asyncio.to_thread(checkpoint_store.record, run_id, Path(workspace), targets)

            boundary.before_mutation = capture_preimage

    def _finalize_checkpoint(self, run_id: str) -> None:
        if self.checkpoint_store is None:
            return
        workspace = getattr(getattr(self.agent, "context", None), "workspace", None)
        if workspace is None:
            return
        try:
            self.checkpoint_store.finalize(run_id, Path(workspace))
        except Exception:
            log.warning("checkpoint finalization failed for run %s", run_id, exc_info=True)

    async def _watch_cancellation(self, cancellation: CancellationToken) -> None:
        await cancellation.wait()
        interrupt = getattr(self.agent, "interrupt", None)
        if callable(interrupt):
            interrupt()

    async def _watch_persisted_cancellation(
        self, run_id: str, cancellation: CancellationToken
    ) -> None:
        if self.run_store is None:
            return
        while not cancellation.cancelled:
            if self.run_store.cancellation_requested(run_id):
                cancellation.cancel()
                return
            await asyncio.sleep(0.2)

    @staticmethod
    def _effective_timeout(spec: BudgetSpec) -> float:
        timeout = spec.max_runtime_seconds
        if spec.deadline_at is not None:
            remaining = (spec.deadline_at - datetime.now(UTC)).total_seconds()
            timeout = min(timeout, max(0.0, remaining))
        return timeout
