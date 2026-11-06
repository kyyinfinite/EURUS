from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from eurus.coding.git_tools import _run_git, git_environment
from eurus.coding.workspace_manager import WorkspaceManager
from eurus.tools.base import ToolContext


def test_git_environment_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-value")
    env = git_environment()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "PATH" in env


def test_git_subprocess_uses_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("EUROPEAN_SECRET", "hidden-secret")
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0
        stdout = b"ok"
        stderr = b""

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout, self.stderr

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["env"] = kwargs.get("env")
        captured["args"] = args
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    context = ToolContext(workspace=repo)
    output = asyncio.run(_run_git(context, "status"))
    assert output == "ok"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "hidden-secret" not in set(env.values())
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_workspace_manager_rejects_cross_owner_reuse(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    monkey_repo = tmp_path
    (monkey_repo / ".git").mkdir()
    subprocess.run(["git", "init", "-q", str(monkey_repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(monkey_repo),
            "-c",
            "user.name=Eurus CI",
            "-c",
            "user.email=eurus-ci@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    allocation = manager.allocate("assignment-1", owner_id="owner-a")
    with pytest.raises(Exception, match="another owner"):
        manager.allocate("assignment-1", owner_id="owner-b")
    assert manager.allocate("assignment-1", owner_id="owner-a") is allocation
    manager.release_all()


def test_workspace_manager_rejects_unsafe_assignment_ids(tmp_path: Path) -> None:
    from eurus.coding.worktree import WorktreeError

    manager = WorkspaceManager(tmp_path)
    for bad in ("", "  ", "../escape", "has/slash", "x" * 65):
        with pytest.raises(WorktreeError):
            manager.allocate(bad, owner_id="owner-a")


def test_browser_url_guard_rejects_embedded_credentials() -> None:
    from eurus.browser.security import BrowserPolicyError, check_url_syntax

    with pytest.raises(BrowserPolicyError, match="credentials"):
        check_url_syntax("https://user:pass@example.com/page")
    assert check_url_syntax("https://example.com/page").startswith("https://")


def test_browser_url_guard_rejects_out_of_range_ports() -> None:
    from eurus.browser.security import BrowserPolicyError, check_url_syntax

    with pytest.raises(BrowserPolicyError, match="port"):
        check_url_syntax("http://example.com:99999/")


def test_browser_download_requires_approval(tmp_path: Path) -> None:
    from eurus.browser.manager import BrowserManager, BrowserSettings
    from eurus.browser.tools import browser_tools

    settings = BrowserSettings(enabled=True, backend="cdp")
    manager = BrowserManager(settings, workspace=tmp_path)
    tools = {tool.name: tool for tool in browser_tools(manager)}
    download = tools["browser_download"]
    assert download.mutates is True
    assert download.requires_approval is True
