from __future__ import annotations

import asyncio

from eurus.agent.verification import (
    RuntimeVerificationService,
    _command_intent,
    _command_output_failure,
    _has_success_indicator,
    _safe_relative_candidate,
    _transcript_tool_results,
)
from eurus.models import Message, ToolResult


def _service(tmp_path, transcript=None):
    return RuntimeVerificationService(workspace=tmp_path, transcript=transcript or (lambda: []))


def _with_shell_output(output: str, is_error: bool = False):
    def transcript():
        return [
            Message.assistant("running"),
            Message.tool("1", output, "shell"),
        ]

    return transcript


def test_exit_code_underscore_format_fails_verification(tmp_path) -> None:
    service = _service(tmp_path, _with_shell_output("exit_code: 1"))
    result = asyncio.run(service.verify_completion("run the tests", "All green."))
    assert result.passed is False
    assert result.applies is True
    assert result.evidence[0] == "exit_status:1"


def test_failed_exit_run_checks_format_fails(tmp_path) -> None:
    service = _service(tmp_path, _with_shell_output("[mypy] failed (exit 1) in 3.1s"))
    result = asyncio.run(service.verify_completion("run the tests", "Done."))
    assert result.passed is False


def test_mixed_run_checks_output_with_result_line_fails(tmp_path) -> None:
    output = (
        "[tests] passed in 2.0s\n\n1 passed in 0.02s\n\n"
        "[mypy] failed (exit 1) in 3.1s\n\nerror: 2\n\n"
        "result: 1 failed"
    )
    service = _service(tmp_path, _with_shell_output(output))
    result = asyncio.run(service.verify_completion("run the checks", "Checks pass."))
    assert result.passed is False


def test_result_summary_line_takes_priority_over_passed_words(tmp_path) -> None:
    output = "[tests] passed in 2.0s\n\n2 passed, 1 failed\n\nresult: 1 failed"
    assert _command_output_failure(output) is not None
    service = _service(tmp_path, _with_shell_output(output))
    result = asyncio.run(service.verify_completion("run the tests", "Green."))
    assert result.passed is False


def test_all_checks_passed_summary_passes(tmp_path) -> None:
    output = "[tests] passed in 2.0s\n\n3 passed\n\nresult: all checks passed"
    assert _command_output_failure(output) is None
    service = _service(tmp_path, _with_shell_output(output))
    result = asyncio.run(service.verify_completion("run the tests", "Green."))
    assert result.passed is True


def test_success_indicators_use_word_boundaries() -> None:
    assert _has_success_indicator("looks broken now") is False
    assert _has_success_indicator("please look at the token usage") is False
    assert _has_success_indicator("the broker okayed nothing") is False
    assert _has_success_indicator("2 passed") is True
    assert _has_success_indicator("exit_code: 0") is True
    assert _has_success_indicator("exit code 0") is True
    assert _has_success_indicator("0 failed") is True
    assert _has_success_indicator("no errors") is True


def test_check_the_weather_is_not_failed(tmp_path) -> None:
    def transcript():
        return [
            Message.assistant("searching"),
            Message.tool("1", "sunny, 31C", "web_search"),
        ]

    service = _service(tmp_path, transcript)
    result = asyncio.run(
        service.verify_completion("check the weather in Jakarta", "It is sunny, 31C.")
    )
    assert result.passed is False
    assert result.applies is False


def test_command_intent_classification() -> None:
    assert _command_intent("run the tests and fix failures") == "strong"
    assert _command_intent("run the test suite") == "strong"
    assert _command_intent("execute the deploy script") == "strong"
    assert _command_intent("build the project") == "strong"
    assert _command_intent("all tests must pass before you finish") == "strong"
    assert _command_intent("check the weather") == "ambiguous"
    assert _command_intent("run a command for me") == "ambiguous"
    assert _command_intent("what tests did you run?") == "none"
    assert _command_intent("write a summary of the design") == "none"


def test_ambiguous_intent_declines_instead_of_failing(tmp_path) -> None:
    service = _service(tmp_path, _with_shell_output("1 failed"))
    result = asyncio.run(service.verify_completion("run something", "Done."))
    assert result.passed is False
    assert result.applies is False
    assert result.evidence == ("objective_only_claim",)


def test_rename_objective_accepts_absent_source(tmp_path) -> None:
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    service = _service(tmp_path)
    result = asyncio.run(service.verify_completion("rename a.py to b.py", "Renamed."))
    assert result.passed is True


def test_rename_objective_requires_destination(tmp_path) -> None:
    service = _service(tmp_path)
    result = asyncio.run(service.verify_completion("rename a.py to b.py", "Renamed."))
    assert result.passed is False
    assert result.evidence[0] == "missing_file:b.py"


def test_move_objective_accepts_absent_source_with_existing_target(tmp_path) -> None:
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "notes.md").write_text("data", encoding="utf-8")
    service = _service(tmp_path)
    result = asyncio.run(service.verify_completion("move notes.md to archive/notes.md", "Moved."))
    assert result.passed is True


def test_rename_source_still_present_is_not_a_failure(tmp_path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("new\n", encoding="utf-8")
    service = _service(tmp_path)
    result = asyncio.run(service.verify_completion("rename a.py to b.py", "Renamed."))
    assert result.passed is True


def test_safe_relative_candidate_rejects_sibling_prefix_paths(tmp_path) -> None:
    sibling = tmp_path.parent / f"{tmp_path.name}-evil"
    sibling.mkdir(exist_ok=True)
    try:
        assert _safe_relative_candidate(tmp_path, str(sibling / "x.md")) is None
        assert _safe_relative_candidate(tmp_path, str(tmp_path / "x.md")) is not None
    finally:
        (sibling / "x.md").unlink(missing_ok=True)
        sibling.rmdir()


def test_transcript_tool_results_keeps_names_and_limit() -> None:
    messages = [
        Message.assistant("turn"),
        Message.tool("1", "one", "shell"),
        Message.tool("2", "two", "read_file"),
        Message.assistant("plain"),
        Message.tool("3", "three", "run_checks"),
    ]
    results = _transcript_tool_results(messages, limit=2)
    assert [item.name for item in results] == ["read_file", "run_checks"]
    assert [item.output for item in results] == ["two", "three"]


def test_shell_error_tool_result_fails(tmp_path) -> None:
    service = _service(tmp_path)
    failed = service._check_command_evidence(
        "run the tests",
        [ToolResult(call_id="1", name="shell", output="boom", is_error=True)],
    )
    assert len(failed) == 1
    assert failed[0].passed is False
    assert failed[0].evidence[0] == "command_error:shell"
