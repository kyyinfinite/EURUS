from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eurus.runtime.models import Run, RuntimeEvent
from eurus.security.redaction import redact_text
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore

MAX_EVENT_RENDERS = 5000
MAX_DETAIL_CHARS = 240
SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "credential",
    "api_key",
    "token",
    "authorization",
)

_EVENT_LABELS = {
    "run.started": "started",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
    "run.blocked": "blocked",
    "plan.updated": "plan",
    "recovery.attempted": "recovery",
    "tool.call.started": "tool",
    "tool.call.finished": "tool",
    "tool.started": "tool",
    "tool.finished": "tool",
    "approval.requested": "approval",
    "approval.granted": "approval",
    "approval.denied": "approval",
    "approval.unavailable": "approval",
    "model.call.completed": "model",
    "verification.completed": "verification",
    "checkpoint.saved": "checkpoint",
    "budget.exhausted": "budget",
    "budget.snapshot": "budget",
    "cost.tracking_unavailable": "cost",
    "delegation.child_registered": "delegate",
    "delegation.child_completed": "delegate",
}


def _redact_payload(value: Any, key: str = "") -> Any:
    lowered_key = key.lower()
    if any(part in lowered_key for part in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _redact_payload(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, key) for item in value]
    if isinstance(value, str):
        return redact_text(value)[:MAX_DETAIL_CHARS]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)[:MAX_DETAIL_CHARS]


def _render_detail(event: RuntimeEvent) -> str:
    payload = event.payload or {}
    if event.event_type == "run.started":
        objective = str(payload.get("objective", ""))
        return f"objective: {redact_text(objective)[:200]}"
    if event.event_type in {"plan.updated", "recovery.attempted"}:
        summary = str(payload.get("summary") or payload.get("reason") or "")
        marker = "replan" if payload.get("is_replan") else "plan"
        return f"{marker}: {redact_text(summary)[:180]}"
    if event.event_type in {"tool.call.started", "tool.started"}:
        tool = payload.get("tool", "?")
        return f"call {tool}"
    if event.event_type in {"tool.call.finished", "tool.finished"}:
        tool = payload.get("tool", "?")
        status = payload.get("status", "ok" if not payload.get("is_error") else "error")
        return f"{tool} -> {status}"
    if event.event_type.startswith("approval."):
        tool = payload.get("tool", "?")
        fingerprint = str(payload.get("fingerprint", ""))[:16]
        return f"{tool} fingerprint={fingerprint}"
    if event.event_type == "model.call.completed":
        tokens = payload.get("total_tokens", 0)
        return f"tokens={tokens} finish={payload.get('finish', '')}"
    if event.event_type == "verification.completed":
        return f"{payload.get('status', '')}: {redact_text(str(payload.get('summary', '')))[:160]}"
    if event.event_type == "checkpoint.saved":
        return f"iteration={payload.get('iteration', '?')}"
    if event.event_type == "budget.snapshot":
        cost = payload.get("cost_used")
        cost_note = f"cost~{cost}" if isinstance(cost, (int, float)) else "cost unknown"
        return (
            f"tokens={payload.get('tokens_used', 0)} "
            f"model_calls={payload.get('model_calls_used', 0)} {cost_note}"
        )
    if event.event_type == "cost.tracking_unavailable":
        models = payload.get("models") or []
        return f"reason={payload.get('reason', '')} models={models}"
    parts = [f"{key}={_redact_payload(value, key)}" for key, value in list(payload.items())[:4]]
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class TraceLine:
    sequence: int
    at: str
    label: str
    event_type: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "at": self.at,
            "event": self.event_type,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RunTrace:
    run: Run
    lines: tuple[TraceLine, ...]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run.run_id,
            "owner_id": self.run.owner_id,
            "status": self.run.status.value,
            "stop_reason": self.run.stop_reason,
            "objective": redact_text(self.run.objective)[:400],
            "session_id": self.run.session_id,
            "created_at": self.run.created_at.isoformat(),
            "finished_at": self.run.finished_at.isoformat() if self.run.finished_at else None,
            "truncated": self.truncated,
            "events": [line.to_dict() for line in self.lines],
        }


class TraceBuilder:
    def __init__(self, run_store: RunStore, event_store: EventStore) -> None:
        self.run_store = run_store
        self.event_store = event_store

    def build(self, run_id: str, *, limit: int = MAX_EVENT_RENDERS) -> RunTrace:
        run = self.run_store.get(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        events = self.event_store.list_for_run(run_id, limit=limit)
        lines: list[TraceLine] = []
        for event in events:
            label = _EVENT_LABELS.get(event.event_type, event.event_type.split(".")[0])
            try:
                detail = _render_detail(event)
            except Exception:
                detail = "detail unavailable"
            lines.append(
                TraceLine(
                    sequence=event.sequence_no,
                    at=event.created_at.isoformat(),
                    label=label,
                    event_type=event.event_type,
                    detail=detail,
                )
            )
        return RunTrace(run=run, lines=tuple(lines), truncated=len(events) >= limit)

    def render(self, run_id: str, *, limit: int = MAX_EVENT_RENDERS) -> str:
        trace = self.build(run_id, limit=limit)
        run = trace.run
        header = [
            f"run {run.run_id}",
            f"status: {run.status.value}" + (f" ({run.stop_reason})" if run.stop_reason else ""),
            f"objective: {redact_text(run.objective)[:200]}",
            f"created: {run.created_at.isoformat()}",
        ]
        if run.finished_at is not None:
            header.append(f"finished: {run.finished_at.isoformat()}")
        body = [
            f"{line.sequence:>5} {line.at} [{line.label}] {line.detail}" for line in trace.lines
        ]
        if trace.truncated:
            body.append(f"... trace truncated at {len(trace.lines)} events")
        if not body:
            body.append("(no events recorded)")
        return "\n".join([*header, "", *body])
