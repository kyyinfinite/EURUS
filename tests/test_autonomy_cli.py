from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from eurus.cli import main


def run(tmp_path: Path, *args: str) -> object:
    return CliRunner().invoke(main, list(args), env={"EURUS_HOME": str(tmp_path)})


def bootstrap(tmp_path: Path) -> None:
    result = run(tmp_path, "bootstrap", "--yes", "repair")
    assert result.exit_code == 0, result.output


def test_autonomy_help_lists_groups(tmp_path: Path) -> None:
    result = run(tmp_path, "autonomy", "--help")
    assert result.exit_code == 0
    for group in ("goals", "workflows", "agents", "events", "jobs", "daemon", "health"):
        assert group in result.output


def test_autonomy_goal_lifecycle_commands(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    result = run(
        tmp_path,
        "autonomy",
        "goals",
        "create",
        "Track the weekly digest",
        "--owner",
        "alice",
        "--criteria",
        "digest exists;sources cited",
    )
    assert result.exit_code == 0, result.output
    assert "created goal" in result.output
    goal_id = result.output.split("created goal ")[1].split(" ")[0]

    listed = run(tmp_path, "autonomy", "goals", "list")
    assert listed.exit_code == 0
    assert "Track the weekly digest" in listed.output

    shown = run(tmp_path, "autonomy", "goals", "show", goal_id)
    assert shown.exit_code == 0
    assert "created" in shown.output

    paused = run(tmp_path, "autonomy", "goals", "pause", goal_id, "--owner", "alice")
    assert paused.exit_code == 0
    assert "waiting" in paused.output

    resumed = run(tmp_path, "autonomy", "goals", "resume", goal_id, "--owner", "alice")
    assert resumed.exit_code == 0
    assert "active" in resumed.output

    cancelled = run(tmp_path, "autonomy", "goals", "cancel", goal_id, "--owner", "alice")
    assert cancelled.exit_code == 0
    assert "cancelled" in cancelled.output


def test_autonomy_workflow_commands(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    definition = {
        "workflow_id": "digest-flow",
        "name": "Digest flow",
        "required_inputs": ["topic"],
        "nodes": [
            {
                "node_id": "gather",
                "node_type": "agent_task",
                "config": {"objective": "Gather material on {{inputs.topic}}"},
            },
            {
                "node_id": "gate",
                "node_type": "approval",
                "depends_on": ["gather"],
                "config": {"prompt": "Approve the digest?"},
            },
        ],
    }
    definition_file = tmp_path / "workflow.json"
    definition_file.write_text(json.dumps(definition), encoding="utf-8")
    result = run(tmp_path, "autonomy", "workflows", "define", str(definition_file))
    assert result.exit_code == 0, result.output
    assert "registered workflow digest-flow version 1" in result.output

    listed = run(tmp_path, "autonomy", "workflows", "list")
    assert listed.exit_code == 0
    assert "digest-flow" in listed.output

    started = run(
        tmp_path,
        "autonomy",
        "workflows",
        "start",
        "digest-flow",
        "--owner",
        "alice",
        "--input",
        "topic=agents",
    )
    assert started.exit_code == 0, started.output
    assert "started instance" in started.output
    instance_id = started.output.split("started instance ")[1].split(" ")[0]

    shown = run(tmp_path, "autonomy", "workflows", "show", instance_id)
    assert shown.exit_code == 0
    assert "gather" in shown.output

    approvals = run(tmp_path, "autonomy", "workflows", "approvals")
    assert approvals.exit_code == 0

    cancelled = run(tmp_path, "autonomy", "workflows", "cancel", instance_id, "--owner", "alice")
    assert cancelled.exit_code == 0
    assert "cancelled" in cancelled.output

    invalid = {
        "workflow_id": "bad-flow",
        "name": "Bad",
        "nodes": [
            {
                "node_id": "a",
                "node_type": "agent_task",
                "depends_on": ["b"],
                "config": {"objective": "A"},
            },
            {
                "node_id": "b",
                "node_type": "agent_task",
                "depends_on": ["a"],
                "config": {"objective": "B"},
            },
        ],
    }
    invalid_file = tmp_path / "bad.json"
    invalid_file.write_text(json.dumps(invalid), encoding="utf-8")
    rejected = run(tmp_path, "autonomy", "workflows", "define", str(invalid_file))
    assert rejected.exit_code != 0
    assert "cycle" in rejected.output


def test_autonomy_agent_commands(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    registered = run(
        tmp_path,
        "autonomy",
        "agents",
        "register",
        "researcher",
        "--description",
        "Finds sources",
        "--capability",
        "research",
        "--tool",
        "web_search",
    )
    assert registered.exit_code == 0, registered.output
    assert "registered specialist researcher" in registered.output

    listed = run(tmp_path, "autonomy", "agents", "list")
    assert listed.exit_code == 0
    assert "researcher" in listed.output

    dispatched = run(
        tmp_path,
        "autonomy",
        "agents",
        "dispatch",
        "Research agent runtimes",
        "researcher",
        "--owner",
        "alice",
    )
    assert dispatched.exit_code == 0, dispatched.output
    assert "dispatched assignment" in dispatched.output

    assignments = run(tmp_path, "autonomy", "agents", "assignments")
    assert assignments.exit_code == 0
    assert "researcher" in assignments.output

    disabled = run(tmp_path, "autonomy", "agents", "disable", "researcher")
    assert disabled.exit_code == 0
    blocked = run(
        tmp_path,
        "autonomy",
        "agents",
        "dispatch",
        "Research again",
        "researcher",
        "--owner",
        "alice",
    )
    assert blocked.exit_code != 0

    enabled = run(tmp_path, "autonomy", "agents", "enable", "researcher")
    assert enabled.exit_code == 0


def test_autonomy_event_commands(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    subscribed = run(
        tmp_path,
        "autonomy",
        "events",
        "subscribe",
        "file-watcher",
        "--type",
        "FILE_CREATED",
        "--subject-type",
        "file",
        "--objective",
        "Analyze {{payload.path}}",
    )
    assert subscribed.exit_code == 0, subscribed.output
    assert "subscription file-watcher" in subscribed.output

    published = run(
        tmp_path,
        "autonomy",
        "events",
        "publish",
        "FILE_CREATED",
        "--subject-type",
        "file",
        "--subject-id",
        "file-1",
        "--payload",
        '{"path": "/tmp/report.md"}',
    )
    assert published.exit_code == 0, published.output
    assert "published FILE_CREATED" in published.output

    duplicate = run(
        tmp_path,
        "autonomy",
        "events",
        "publish",
        "FILE_CREATED",
        "--dedup-key",
        "file-1-v1",
    )
    assert duplicate.exit_code == 0
    again = run(
        tmp_path,
        "autonomy",
        "events",
        "publish",
        "FILE_CREATED",
        "--dedup-key",
        "file-1-v1",
    )
    assert again.exit_code == 0
    assert "deduplicated" in again.output

    listed = run(tmp_path, "autonomy", "events", "list")
    assert listed.exit_code == 0
    assert "FILE_CREATED" in listed.output

    stats = run(tmp_path, "autonomy", "events", "stats")
    assert stats.exit_code == 0


def test_autonomy_job_commands(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    listed = run(tmp_path, "autonomy", "jobs", "list")
    assert listed.exit_code == 0
    dead = run(tmp_path, "autonomy", "jobs", "dead-letters")
    assert dead.exit_code == 0


def test_autonomy_health(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    result = run(tmp_path, "autonomy", "health")
    assert result.exit_code == 0, result.output
    assert "jobs_queued" in result.output


def test_schedule_daemon_uses_unified_runtime(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    result = run(tmp_path, "schedule", "daemon", "--help")
    assert result.exit_code == 0
    assert "--workers" in result.output
    assert "--owner" in result.output


def test_autonomy_daemon_help(tmp_path: Path) -> None:
    result = run(tmp_path, "autonomy", "daemon", "--help")
    assert result.exit_code == 0
    for flag in ("--no-goals", "--no-workflows", "--no-events"):
        assert flag in result.output


def _unused(cli_module: object) -> None:
    return None
