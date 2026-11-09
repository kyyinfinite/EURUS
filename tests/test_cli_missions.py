from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from eurus.cli import main

TASK_UI = '{"task_key": "ui", "objective": "build the page", "preferred_role": "frontend"}'
TASK_API = '{"task_key": "api", "objective": "build the API", "preferred_role": "backend"}'


def _invoke(args: list[str], tmp_path: Path) -> object:
    return CliRunner().invoke(main, args, env={"EURUS_HOME": str(tmp_path)})


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def _create_mission(home: Path, objective: str = "Ship the feature") -> str:
    result = _invoke(["missions", "create", objective], home)
    assert result.exit_code == 0, result.output
    return str(result.output).split()[1]


def test_create_lists_and_shows(home: Path) -> None:
    mission_id = _create_mission(home, "Ship the settings feature")

    listed = _invoke(["missions", "list"], home)
    assert listed.exit_code == 0
    assert mission_id[:12] in listed.output
    assert "created" in listed.output

    shown = _invoke(["missions", "show", mission_id], home)
    assert shown.exit_code == 0
    assert "Ship the settings feature" in shown.output
    assert "total=0" in shown.output


def test_create_with_tasks_and_plan_flow(home: Path) -> None:
    result = _invoke(
        ["missions", "create", "Feature", "--task", TASK_UI, "--task", TASK_API],
        home,
    )
    assert result.exit_code == 0, result.output
    mission_id = str(result.output).split()[1]

    tasks = _invoke(["missions", "tasks", mission_id], home)
    assert tasks.exit_code == 0
    assert "ui" in tasks.output and "api" in tasks.output
    assert "pending" in tasks.output

    started = _invoke(["missions", "start", mission_id], home)
    assert started.exit_code == 0, started.output
    assert "running" in started.output

    tasks = _invoke(["missions", "tasks", mission_id], home)
    assert "ready" in tasks.output


def test_plan_command_on_existing_mission(home: Path) -> None:
    mission_id = _create_mission(home)
    planned = _invoke(
        ["missions", "plan", mission_id, "--task", '{"task_key": "solo", "objective": "work"}'],
        home,
    )
    assert planned.exit_code == 0, planned.output
    shown = _invoke(["missions", "show", mission_id], home)
    assert "planned" in shown.output


def test_cancel_reason_is_recorded(home: Path) -> None:
    mission_id = _create_mission(home)
    cancelled = _invoke(["missions", "cancel", mission_id, "--reason", "scope change"], home)
    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled" in cancelled.output

    resumed = _invoke(["missions", "resume", mission_id], home)
    assert resumed.exit_code == 1, "terminal missions refuse resume via CLI"
    assert "is terminal (cancelled)" in resumed.output


def test_finalize_reports_blocked_with_reasons(home: Path) -> None:
    mission_id = _create_mission(home)
    _invoke(
        ["missions", "plan", mission_id, "--task", '{"task_key": "solo", "objective": "w"}'],
        home,
    )
    _invoke(["missions", "start", mission_id], home)
    finalized = _invoke(["missions", "finalize", mission_id], home)
    assert finalized.exit_code == 0, finalized.output
    assert "decision=blocked" in finalized.output
    assert "reason:" in finalized.output


def test_unknown_mission_fails_cleanly(home: Path) -> None:
    result = _invoke(["missions", "show", "does-not-exist"], home)
    assert result.exit_code == 1
    assert "not found" in result.output


def test_invalid_task_json_is_rejected(home: Path) -> None:
    result = _invoke(["missions", "create", "Bad", "--task", "{not json"], home)
    assert result.exit_code == 1
    assert "valid JSON" in result.output


def test_help_lists_all_commands(home: Path) -> None:
    result = _invoke(["missions", "--help"], home)
    assert result.exit_code == 0
    commands = (
        "create",
        "list",
        "show",
        "tasks",
        "plan",
        "start",
        "cancel",
        "resume",
        "dispatch",
        "finalize",
    )
    for command in commands:
        assert command in result.output
