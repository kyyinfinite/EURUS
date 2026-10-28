from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from eurus.agent.coder_tools import (
    WorktreeConfinedApprover,
    build_coder_registry,
    coder_delegation_tools,
)
from eurus.coding.worktree import (
    WorktreeError,
    create_worktree,
    review_patch,
)
from eurus.errors import ToolError
from eurus.models import Message, ToolCall, Usage
from eurus.providers.base import ChatResponse
from eurus.providers.router import Router
from eurus.providers.testing import FakeTransport, openai_stream
from eurus.tools.base import Tool, ToolContext
from eurus.tools.builtin import default_registry


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_worktree_isolation_contains_writes(repo: Path) -> None:
    worktree = create_worktree(repo)
    try:
        assert worktree.path.exists()
        (worktree.path / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        assert not (repo / "new_file.py").exists()
        worktree.add_all()
        diff = worktree.diff_against_head()
        assert "new_file.py" in diff
        assert "app.py" not in diff.replace("new_file.py", "")
    finally:
        assert worktree.cleanup() == "cleaned"
        assert not worktree.path.exists()


def test_worktree_unavailable_outside_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        create_worktree(plain)


def test_review_patch_flags_dangerous_content() -> None:
    traversal = "+++ b/../../etc/passwd\n--- a/x\n"
    assert review_patch(traversal).approved is False
    symlink = "diff --git a/l b/l\nnew file mode 120000\n"
    assert review_patch(symlink).approved is False
    binary = "GIT binary patch\nliteral 10\n"
    assert review_patch(binary).approved is False
    secrets = "+++ b/config.py\n+key = 'sk-abcdefghijklmnopqrst'\n"
    assert review_patch(secrets).approved is False
    git_internals = "+++ b/.git/hooks/pre-commit\n"
    assert review_patch(git_internals).approved is False
    clean = "+++ b/app.py\n--- a/app.py\n+return a + b\n"
    assert review_patch(clean).approved is True


def test_confined_approver_blocks_outside_paths(repo: Path) -> None:
    worktree = create_worktree(repo)
    try:
        approver = WorktreeConfinedApprover(worktree.path)
        inside = ToolCall(
            id="1",
            name="apply_patch",
            arguments={"path": "app.py", "new_content": "x"},
        )
        outside = ToolCall(
            id="2",
            name="apply_patch",
            arguments={
                "path": str(repo / "app.py"),
                "new_content": "x",
            },
        )
        traversal = ToolCall(
            id="3",
            name="apply_patch",
            arguments={"path": "../escape.py", "new_content": "x"},
        )
        read_only = ToolCall(id="4", name="read_file", arguments={"path": "app.py"})
        mutating_tool = Tool(
            "apply_patch",
            "d",
            {},
            lambda a, c: "ok",
            mutates=True,
            requires_approval=True,
        )
        read_tool = Tool("read_file", "d", {}, lambda a, c: "ok")
        assert asyncio.run(approver(inside, mutating_tool)) is True
        assert asyncio.run(approver(outside, mutating_tool)) is False
        assert asyncio.run(approver(traversal, mutating_tool)) is False
        assert asyncio.run(approver(read_only, read_tool)) is True
    finally:
        worktree.cleanup()


def test_coder_registry_excludes_shell_and_web() -> None:
    parent = default_registry(include_capability_tools=True)
    registry = build_coder_registry(parent)
    names = registry.names()
    assert "apply_patch" in names
    assert "read_file" in names
    assert "shell" not in names
    assert "web_search" not in names
    assert "remember_memory" not in names
    assert "delegate_task" not in names


class _ScriptedCoderRouter(Router):
    def __init__(self, transport: FakeTransport) -> None:
        from eurus.catalog import BUILTIN_PROVIDERS
        from eurus.models import ModelRef
        from eurus.providers.base import RetryPolicy
        from eurus.providers.openai_compat import OpenAICompatProvider
        from eurus.providers.testing import RecordingSleep

        provider = OpenAICompatProvider(
            BUILTIN_PROVIDERS["groq"], "k", transport, RetryPolicy(max_retries=0), RecordingSleep()
        )
        super().__init__({"groq": provider}, [ModelRef.parse("groq/test")])

    async def chat(self, messages, tools=(), **kwargs):
        return ChatResponse(
            message=Message.assistant("I patched app.py in the worktree."),
            finish_reason="stop",
            usage=Usage(),
        )


def test_delegate_code_task_returns_reviewed_patch(repo: Path) -> None:
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = _ScriptedCoderRouter(transport)
    parent = default_registry(include_capability_tools=True)
    tools = {tool.name: tool for tool in coder_delegation_tools(router, parent)}
    context = ToolContext(workspace=repo, max_output=20000, timeout=60.0)

    original = (repo / "app.py").read_text(encoding="utf-8")

    async def scripted_run(router, parent_registry, context, worktree, task, **kwargs):
        (worktree.path / "app.py").write_text(
            "def add(a, b):\n    return a + b + 1\n", encoding="utf-8"
        )
        return {
            "status": "completed",
            "verified": False,
            "summary": "patched",
            "patch": "",
            "review": {"approved": True, "findings": []},
            "approvals": [],
            "iterations_used": 1,
            "instructions": "apply via apply_patch",
        }

    from eurus.agent import coder_tools

    original_run = coder_tools.run_coder_subagent
    coder_tools.run_coder_subagent = scripted_run
    try:
        result = asyncio.run(tools["delegate_code_task"].handler({"task": "fix add()"}, context))
    finally:
        coder_tools.run_coder_subagent = original_run
    payload = json.loads(result)
    assert payload["verified"] is False
    assert "apply via apply_patch" in payload["instructions"]
    assert payload["worktree_cleanup"] == "cleaned"
    assert (repo / "app.py").read_text(encoding="utf-8") == original
    assert "return a + b + 1" not in (repo / "app.py").read_text(encoding="utf-8")


def test_delegate_code_task_requires_approval(repo: Path) -> None:
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = _ScriptedCoderRouter(transport)
    parent = default_registry(include_capability_tools=True)
    tools = coder_delegation_tools(router, parent)
    delegate = next(tool for tool in tools if tool.name == "delegate_code_task")
    assert delegate.requires_approval is True
    assert delegate.mutates is True


def test_delegate_code_task_refuses_without_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = _ScriptedCoderRouter(transport)
    parent = default_registry(include_capability_tools=True)
    tools = {tool.name: tool for tool in coder_delegation_tools(router, parent)}
    context = ToolContext(workspace=plain, max_output=20000, timeout=60.0)
    with pytest.raises(ToolError, match="not a git repository"):
        asyncio.run(tools["delegate_code_task"].handler({"task": "change something"}, context))


def test_confined_approver_rejects_unconfinable_approval_tools(repo: Path) -> None:
    worktree = create_worktree(repo)
    try:
        approver = WorktreeConfinedApprover(worktree.path)
        run_checks = Tool(
            "run_checks",
            "d",
            {},
            lambda a, c: "ok",
            requires_approval=True,
        )
        shell_like = Tool("shell", "d", {}, lambda a, c: "ok", requires_approval=True)
        decision = asyncio.run(
            approver(
                ToolCall(id="1", name="run_checks", arguments={"checks": ["tests"]}),
                run_checks,
            )
        )
        assert decision is False
        assert approver.decisions[-1]["reason"] == "unconfinable"
        assert (
            asyncio.run(
                approver(
                    ToolCall(id="2", name="shell", arguments={"command": "ls"}),
                    shell_like,
                )
            )
            is False
        )
    finally:
        worktree.cleanup()


def test_confined_approver_rejects_symlink_escape(repo: Path) -> None:
    worktree = create_worktree(repo)
    try:
        secret = repo / "secret.txt"
        secret.write_text("outside", encoding="utf-8")
        link = worktree.path / "link.py"
        link.symlink_to(secret)
        approver = WorktreeConfinedApprover(worktree.path)
        mutating = Tool(
            "apply_patch", "d", {}, lambda a, c: "ok", mutates=True, requires_approval=True
        )
        decision = asyncio.run(
            approver(
                ToolCall(id="1", name="apply_patch", arguments={"path": "link.py"}),
                mutating,
            )
        )
        assert decision is False
        assert approver.decisions[-1]["reason"] == "outside_worktree"
    finally:
        worktree.cleanup()


def test_coder_registry_excludes_run_checks() -> None:
    parent = default_registry(include_capability_tools=True, include_workflow_tools=True)
    assert "run_checks" in parent.names()
    registry = build_coder_registry(parent)
    assert "run_checks" not in registry.names()
    assert "apply_patch" in registry.names()


def test_worktree_cleanup_removes_parent_temp_directory(repo: Path) -> None:
    worktree = create_worktree(repo)
    parent = worktree.path.parent
    assert parent.exists()
    assert worktree.cleanup() == "cleaned"
    assert not parent.exists()
    assert not worktree.path.exists()


def test_delegate_code_task_documents_head_only_visibility(repo: Path) -> None:
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = _ScriptedCoderRouter(transport)
    parent = default_registry(include_capability_tools=True)
    tools = coder_delegation_tools(router, parent)
    delegate = tools[0]
    assert "uncommitted changes" in delegate.description
    assert "HEAD" in delegate.description
