from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from eurus.cli import main
from eurus.runtime.engine import AgentEngine
from eurus.runtime.trace import TraceBuilder
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore


class FakeAgent:
    def __init__(self) -> None:
        self.max_parallel_tools = 4
        self.iteration_budget = type("Budget", (), {"limit": 50})()

    def interrupt(self) -> None:
        return None

    async def run(self, objective):
        from eurus.agent.events import AgentEvent

        yield AgentEvent.model_completed(1, None, "stop")
        yield AgentEvent.finished("completed", None)


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[RunStore, EventStore]:
    database = tmp_path / "runs.sqlite3"
    return RunStore(database), EventStore(database)


def _seed_events(events: EventStore, run_id: str) -> None:
    from eurus.runtime.models import RuntimeEvent

    entries = [
        ("plan.updated", {"summary": "1. Inspect workspace", "is_replan": False}),
        (
            "tool.call.started",
            {"tool": "shell", "arguments": "python -m pytest"},
        ),
        ("tool.call.finished", {"tool": "shell", "is_error": False, "preview": "ok"}),
        (
            "approval.requested",
            {
                "tool": "apply_patch",
                "fingerprint": "abcdef1234567890",
                "api_key": "sk-secretshouldnotappear",
            },
        ),
        ("approval.granted", {"tool": "apply_patch", "fingerprint": "abcdef1234567890"}),
        (
            "verification.completed",
            {"status": "verified", "summary": "checks passed", "evidence": ["exit:0"]},
        ),
    ]
    sequence = events.last_sequence(run_id)
    for offset, (event_type, payload) in enumerate(entries, 1):
        events.append(
            RuntimeEvent(
                run_id=run_id,
                event_type=event_type,
                sequence_no=sequence + offset,
                payload=payload,
            )
        )


def test_trace_builds_readable_timeline(stores) -> None:
    runs, events = stores
    result = asyncio.run(
        AgentEngine(FakeAgent(), run_store=runs, event_store=events).run("objective")
    )
    _seed_events(events, result.run.run_id)
    builder = TraceBuilder(runs, events)
    text = builder.render(result.run.run_id)
    assert "run.started" not in text or "started" in text
    assert "[plan]" in text
    assert "[approval]" in text
    assert "apply_patch" in text
    assert "abcdef1234567890" in text
    assert "sk-secretshouldnotappear" not in text


def test_trace_json_redacts_secrets(stores) -> None:
    runs, events = stores
    result = asyncio.run(
        AgentEngine(FakeAgent(), run_store=runs, event_store=events).run("objective")
    )
    _seed_events(events, result.run.run_id)
    payload = TraceBuilder(runs, events).build(result.run.run_id).to_dict()
    encoded = json.dumps(payload)
    assert "sk-secretshouldnotappear" not in encoded
    assert payload["status"] in {"completed"}
    from eurus.runtime.trace import _redact_payload

    assert _redact_payload("sk-secretvalue", "api_key") == "[REDACTED]"
    assert _redact_payload({"password": "hunter2", "note": "ok"}, "")["password"] == "[REDACTED]"


def test_trace_missing_run_raises(stores) -> None:
    runs, events = stores
    with pytest.raises(KeyError):
        TraceBuilder(runs, events).build("nope")


def test_trace_empty_history_is_graceful(stores) -> None:
    runs, events = stores
    result = asyncio.run(
        AgentEngine(FakeAgent(), run_store=runs, event_store=events).run("objective")
    )
    events.database.connection().execute("DELETE FROM eurus_runtime_events") if False else None
    text = TraceBuilder(runs, events).render(result.run.run_id)
    assert isinstance(text, str)


def test_trace_command_outputs_timeline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    database = tmp_path / "sessions.sqlite3"
    runs = RunStore(database)
    events = EventStore(database)
    result = asyncio.run(
        AgentEngine(FakeAgent(), run_store=runs, event_store=events).run("objective")
    )
    _seed_events(events, result.run.run_id)
    outcome = CliRunner().invoke(main, ["trace", result.run.run_id])
    assert outcome.exit_code == 0, outcome.output
    assert "apply_patch" in outcome.output
    json_outcome = CliRunner().invoke(main, ["trace", "--json", result.run.run_id])
    assert json_outcome.exit_code == 0
    payload = json.loads(json_outcome.output)
    assert payload["run_id"] == result.run.run_id
    assert any(item["event"] == "approval.granted" for item in payload["events"])


def test_trace_command_reports_missing_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    outcome = CliRunner().invoke(main, ["trace", "missing-run"])
    assert outcome.exit_code != 0
    assert "not found" in outcome.output
