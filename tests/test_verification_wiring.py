from __future__ import annotations

import asyncio
from pathlib import Path

from eurus.agent.factory import build_agent
from eurus.agent.verification import (
    RuntimeVerificationService,
)
from eurus.config import load_config
from eurus.models import Message
from eurus.providers.testing import FakeTransport, openai_stream


def _config(tmp_path: Path, **overrides):
    return load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={"agent": {"model": "groq/x"}, **overrides},
    )


def test_build_agent_wires_verifiers_and_planner(tmp_path, monkeypatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = _config(tmp_path)
    transport = FakeTransport(streams=[openai_stream("ok")])
    from eurus.providers.factory import build_router

    router = build_router(config, transport=transport, environ={"GROQ_API_KEY": "k"})
    agent = build_agent(config, router, workspace=tmp_path)
    assert agent.completion_verifier is not None
    assert agent.step_verifier is not None
    assert agent.planner is not None


def test_build_agent_respects_verification_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = _config(tmp_path, verification={"enabled": False})
    transport = FakeTransport(streams=[openai_stream("ok")])
    from eurus.providers.factory import build_router

    router = build_router(config, transport=transport, environ={"GROQ_API_KEY": "k"})
    agent = build_agent(config, router, workspace=tmp_path)
    assert agent.completion_verifier is None
    assert agent.step_verifier is None


def test_model_claim_without_evidence_cannot_pass_verification(tmp_path):
    service = RuntimeVerificationService(
        workspace=tmp_path, transcript=lambda: [Message.user("create notes.md")]
    )
    result = asyncio.run(
        service.verify_completion("create notes.md with a summary", "Done, created it.")
    )
    assert result.passed is False
    assert result.applies is True
    assert not (tmp_path / "notes.md").exists()


def test_deterministic_file_check_establishes_success(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("summary content", encoding="utf-8")
    service = RuntimeVerificationService(workspace=tmp_path, transcript=lambda: [])
    result = asyncio.run(service.verify_completion("create notes.md with a summary", "Done."))
    assert result.passed is True
    assert any(item.startswith("file_exists:") for item in result.evidence)


def test_deletion_objective_accepts_absent_file(tmp_path):
    service = RuntimeVerificationService(workspace=tmp_path, transcript=lambda: [])
    result = asyncio.run(
        service.verify_completion("delete old notes.md from the workspace", "Removed.")
    )
    assert result.passed is True


def test_question_objective_is_not_failed_by_missing_evidence(tmp_path):
    service = RuntimeVerificationService(workspace=tmp_path, transcript=lambda: [])
    result = asyncio.run(service.verify_completion("what is the capital of France?", "Paris."))
    assert result.passed is False
    assert result.applies is False


def test_command_claim_without_execution_fails(tmp_path):
    from eurus.models import ToolResult

    results = [
        ToolResult(call_id="1", name="read_file", output="file contents"),
    ]

    def transcript():
        return [
            Message.assistant("working"),
            Message.tool("1", results[0].output, "read_file"),
        ]

    service = RuntimeVerificationService(workspace=tmp_path, transcript=transcript)
    result = asyncio.run(
        service.verify_completion("run the tests and fix failures", "All tests pass now.")
    )
    assert result.passed is False
    assert result.applies is True


def test_command_claim_with_failing_command_output_fails(tmp_path):

    def transcript():
        return [
            Message.assistant("running"),
            Message.tool("1", "error: 2 failed, 0 passed, exit code 1", "shell"),
        ]

    service = RuntimeVerificationService(workspace=tmp_path, transcript=transcript)
    result = asyncio.run(
        service.verify_completion("run the tests and fix failures", "Tests are green.")
    )
    assert result.passed is False


def test_research_claim_requires_tool_evidence_and_citations(tmp_path):

    def transcript_without_web():
        return [Message.assistant("thinking"), Message.tool("1", "ok", "read_file")]

    service = RuntimeVerificationService(workspace=tmp_path, transcript=transcript_without_web)
    result = asyncio.run(
        service.verify_completion(
            "research the latest rust release and cite sources",
            "Rust 1.90 was released. See https://blog.rust-lang.org/2026/01/15/Rust-1.90/",
        )
    )
    assert result.passed is False
    assert result.applies is True

    def transcript_with_web():
        return [
            Message.assistant("searching"),
            Message.tool("1", "results found", "web_search"),
        ]

    service_with_web = RuntimeVerificationService(
        workspace=tmp_path, transcript=transcript_with_web
    )
    result_with_web = asyncio.run(
        service_with_web.verify_completion(
            "research the latest rust release and cite sources",
            "Rust 1.90 was released. Source: https://blog.rust-lang.org/2026/01/15/Rust-1.90/",
        )
    )
    assert result_with_web.passed is True


def test_step_verifier_rejects_empty_and_failure_marker_output(tmp_path):
    service = RuntimeVerificationService(workspace=tmp_path, transcript=lambda: [])
    empty = asyncio.run(service.verify_step("title", "criteria", "   "))
    assert empty.passed is False
    failing = asyncio.run(
        service.verify_step(
            "run checks", "checks pass", "Traceback (most recent call last):\n boom"
        )
    )
    assert failing.passed is False
    clean = asyncio.run(service.verify_step("read", "read file", "file contents here"))
    assert clean.passed is True


def test_verification_cannot_be_spoofed_by_out_of_workspace_paths(tmp_path):
    from eurus.agent.verification import _safe_relative_candidate

    outside = tmp_path.parent / "outside-secret.md"
    service = RuntimeVerificationService(workspace=tmp_path, transcript=lambda: [])
    assert _safe_relative_candidate(tmp_path, str(outside)) is None
    result = asyncio.run(
        service.verify_completion(f"create {outside} with secrets", "I created it.")
    )
    assert result.passed is False


def test_run_command_creates_durable_run_record(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from eurus.cli import main
    from eurus.storage.events import EventStore
    from eurus.storage.runs import RunStore

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    transport = FakeTransport(streams=[openai_stream("hello there")])
    from eurus.providers.factory import build_router as real_build_router

    monkeypatch.setattr(
        "eurus.cli.common.build_router",
        lambda config: real_build_router(
            config, transport=transport, environ={"GROQ_API_KEY": "k"}
        ),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[agent]\nmodel = "groq/x"\n[permissions]\nworkspace = "."\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(main, ["--config", str(config_file), "run", "say hi"], env={})
    assert result.exit_code == 0, result.output
    runs = RunStore(tmp_path / "data" / "eurus.sqlite3")
    events = EventStore(tmp_path / "data" / "eurus.sqlite3")
    history = runs.list_runs(limit=10)
    assert len(history) == 1
    stored = history[0]
    assert stored.status.value in {"completed", "failed"}
    stored_events = events.list_for_run(stored.run_id, limit=100)
    assert any(event.event_type == "run.started" for event in stored_events)
    assert any(event.event_type == "verification.completed" for event in stored_events)
    assert any(event.event_type == "budget.snapshot" for event in stored_events)
