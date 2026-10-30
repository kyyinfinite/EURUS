from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eurus.runtime.budgets import BudgetSpec, BudgetTracker
from eurus.runtime.models import Run, RunStatus
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore

log = logging.getLogger(__name__)

MAX_DECISION_LINES = 6
MAX_FAILURE_LINES = 5


@dataclass(frozen=True, slots=True)
class CheckpointInfo:
    """In-memory mirror of the last checkpoint, so renders avoid SQLite."""

    iteration: int | None = None
    side_effect_status: str = ""


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    """In-memory mirror of tool-attempt evidence for the current run."""

    uncertain_mutation: bool = False
    failures: tuple[tuple[str, str], ...] = ()


STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.QUEUED: "queued",
    RunStatus.RUNNING: "running",
    RunStatus.WAITING_APPROVAL: "waiting_approval",
    RunStatus.WAITING_INPUT: "waiting_input",
    RunStatus.RETRY_WAIT: "retry_wait",
    RunStatus.VERIFYING: "verifying",
    RunStatus.INTERRUPTED: "interrupted",
    RunStatus.COMPLETED: "completed",
    RunStatus.FAILED: "failed",
    RunStatus.CANCELLED: "cancelled",
}


def _remaining_seconds(deadline_at: Any) -> float | None:
    if deadline_at is None:
        return None
    try:
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        return max(0.0, float(remaining))
    except Exception:
        return None


def render_run_state(
    run: Run,
    spec: BudgetSpec,
    agent: Any,
    *,
    attempt_store: ToolAttemptStore | None = None,
    run_store: RunStore | None = None,
    tracker: BudgetTracker | None = None,
    attempt_evidence: AttemptEvidence | None = None,
    checkpoint_info: CheckpointInfo | None = None,
) -> str:
    sections: list[str] = []
    identity = _identity_section(run)
    if identity:
        sections.append(identity)
    budgets = _budget_section(run, spec, agent, tracker)
    if budgets:
        sections.append(budgets)
    decisions = _decisions_section(agent)
    if decisions:
        sections.append(decisions)
    failures = _failures_section(agent)
    if failures:
        sections.append(failures)
    attempts = _attempts_section(run, attempt_store, attempt_evidence)
    if attempts:
        sections.append(attempts)
    checkpoint = _checkpoint_section(run, run_store, checkpoint_info)
    if checkpoint:
        sections.append(checkpoint)
    if not sections:
        return ""
    body = "\n".join(sections)
    return f"<eurus-tier:run-state>\n{body}\n</eurus-tier:run-state>"


def _identity_section(run: Run) -> str:
    parts: list[str] = []
    status = STATUS_LABELS.get(run.status, run.status.value)
    line = f"Run {run.run_id[:12]} status={status}"
    if run.stop_reason:
        line += f" stop_reason={run.stop_reason}"
    parts.append(line)
    parts.append(f"Objective: {run.objective[:200]}")
    if run.parent_run_id:
        parts.append(f"Parent run: {run.parent_run_id[:12]} (this is a child/subagent run)")
    return "\n".join(parts)


def _budget_section(run: Run, spec: BudgetSpec, agent: Any, tracker: BudgetTracker | None) -> str:
    parts: list[str] = []
    iteration_used = getattr(agent, "iteration_budget", None)
    if iteration_used is not None:
        parts.append(f"Iterations: {iteration_used.used}/{iteration_used.limit}")
    model_used = getattr(agent, "model_calls_used", None)
    if model_used is not None:
        parts.append(f"Model calls: {model_used}/{agent.max_model_calls}")
    if tracker is not None:
        token_line = f"Tokens: {tracker.tokens_used}"
        if spec.max_tokens is not None:
            token_line += f"/{spec.max_tokens}"
        parts.append(token_line)
        parts.append(f"Tool calls: {tracker.tool_calls_used}/{spec.max_tool_calls}")
    remaining = _remaining_seconds(getattr(agent, "deadline_at", None))
    if remaining is not None and remaining < 120:
        # Only shown near the limit, bucketed so the prompt prefix is not
        # rewritten every second.
        bucket = max(30, math.ceil(remaining / 30) * 30)
        parts.append(f"Wall-clock remaining: <{bucket}s")
    child_allowance = getattr(agent, "context", None)
    allowance = getattr(child_allowance, "max_child_iterations", None)
    if allowance is not None:
        parts.append(f"Child delegation allowance: {allowance} iterations")
    if not parts:
        return ""
    return "Budgets:\n" + "\n".join(f"- {part}" for part in parts)


def _decisions_section(agent: Any) -> str:
    decisions = getattr(agent, "decisions", ())
    if not decisions:
        return ""
    recent = list(decisions)[-MAX_DECISION_LINES:]
    replans = getattr(agent, "replan_count", 0)
    header = "Recent loop decisions: " + ", ".join(item.value for item in recent)
    if replans:
        header += f" (replans: {replans})"
    return header


def _failures_section(agent: Any) -> str:
    counts = getattr(agent, "failure_counts", None)
    if not counts:
        return ""
    lines = [
        f"- {name}: {info[0]} failure(s), last error: {str(info[1])[:160]}"
        for name, info in list(counts.items())[-MAX_FAILURE_LINES:]
    ]
    return "Recent tool failures:\n" + "\n".join(lines)


def _attempts_section(
    run: Run,
    attempt_store: ToolAttemptStore | None,
    evidence: AttemptEvidence | None,
) -> str:
    if evidence is not None:
        parts: list[str] = []
        if evidence.uncertain_mutation:
            parts.append(
                "- WARNING: a mutating tool ended with unknown outcome; "
                "do not blindly repeat it. Await reconciliation."
            )
        failures = evidence.failures[-3:]
        if failures:
            lines = [f"- {name} -> {status}" for name, status in failures]
            parts.append("Recent failed attempts:\n" + "\n".join(lines))
        if not parts:
            return ""
        return "Tool execution evidence:\n" + "\n".join(parts)
    if attempt_store is None:
        return ""
    parts = []
    try:
        if attempt_store.has_uncertain_mutation(run.run_id):
            parts.append(
                "- WARNING: a mutating tool ended with unknown outcome; "
                "do not blindly repeat it. Await reconciliation."
            )
        attempts = attempt_store.list_for_run(run.run_id, limit=12)
        failed = [
            item
            for item in attempts
            if item.get("status") in {"failed", "timeout", "cancelled", "uncertain"}
        ]
        if failed:
            shown = failed[-3:]
            lines = [
                f"- {item.get('tool_name', 'unknown')} -> {item.get('status')}" for item in shown
            ]
            parts.append("Recent failed attempts:\n" + "\n".join(lines))
    except Exception:
        log.debug("attempt summary failed", exc_info=True)
        return ""
    if not parts:
        return ""
    return "Tool execution evidence:\n" + "\n".join(parts)


def _checkpoint_section(
    run: Run,
    run_store: RunStore | None,
    info: CheckpointInfo | None,
) -> str:
    if info is not None:
        parts: list[str] = []
        if info.iteration is not None:
            parts.append(f"last checkpoint at iteration {info.iteration}")
        if info.side_effect_status:
            parts.append(f"side-effect status: {info.side_effect_status}")
        if not parts:
            return ""
        return "Recovery state: " + "; ".join(parts)
    if run_store is None:
        return ""
    try:
        checkpoint = run_store.latest_checkpoint(run.run_id)
    except Exception:
        log.debug("checkpoint lookup failed", exc_info=True)
        return ""
    if not checkpoint:
        return ""
    state = checkpoint.get("state") or {}
    parts = []
    iteration = state.get("iteration")
    if iteration is not None:
        parts.append(f"last checkpoint at iteration {iteration}")
    side_effect = state.get("side_effect_status")
    if side_effect:
        parts.append(f"side-effect status: {side_effect}")
    if not parts:
        return ""
    return "Recovery state: " + "; ".join(parts)
