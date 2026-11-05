from __future__ import annotations

import asyncio

from eurus.models import ToolCall
from eurus.security.approval import SessionScopedApprover, action_fingerprint
from eurus.tools.base import Tool


def _tool(name: str, *, mutates: bool = True) -> Tool:
    return Tool(
        name=name,
        description="test tool",
        parameters={"type": "object"},
        handler=lambda arguments, context: "ok",
        mutates=mutates,
        requires_approval=True,
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


class CountingApprover:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked = 0

    async def __call__(self, call: ToolCall, tool: Tool) -> bool:
        self.asked += 1
        return self.answer


def test_session_scope_reuses_exact_fingerprint_only() -> None:
    base = CountingApprover(True)
    approver = SessionScopedApprover(base, ttl_seconds=60)
    apply_patch = _tool("apply_patch")
    first = asyncio.run(approver(_call("apply_patch", path="a.py", new_content="x"), apply_patch))
    second = asyncio.run(approver(_call("apply_patch", path="a.py", new_content="x"), apply_patch))
    changed = asyncio.run(approver(_call("apply_patch", path="a.py", new_content="y"), apply_patch))
    assert first is True and second is True
    assert changed is True
    assert base.asked == 2
    assert len(approver.active_grants()) == 2


def test_session_scope_expires() -> None:
    base = CountingApprover(True)
    now = {"t": 0.0}
    approver = SessionScopedApprover(base, ttl_seconds=10, clock=lambda: now["t"])
    tool = _tool("apply_patch")
    call = _call("apply_patch", path="a.py", new_content="x")
    assert asyncio.run(approver(call, tool)) is True
    now["t"] = 5.0
    assert asyncio.run(approver(call, tool)) is True
    assert base.asked == 1
    now["t"] = 20.0
    assert asyncio.run(approver(call, tool)) is True
    assert base.asked == 2


def test_session_scope_never_applies_to_shell() -> None:
    base = CountingApprover(True)
    approver = SessionScopedApprover(base)
    shell = _tool("shell")
    call = _call("shell", command="ls")
    assert asyncio.run(approver(call, shell)) is True
    assert asyncio.run(approver(call, shell)) is True
    assert base.asked == 2
    assert approver.active_grants() == []


def test_revocation_kills_grants() -> None:
    base = CountingApprover(True)
    approver = SessionScopedApprover(base)
    tool = _tool("apply_patch")
    call = _call("apply_patch", path="a.py", new_content="x")
    asyncio.run(approver(call, tool))
    fingerprint = action_fingerprint(call)
    assert approver.revoke(fingerprint) is True
    assert asyncio.run(approver(call, tool)) is True
    assert base.asked == 2


def test_denied_actions_never_create_grants() -> None:
    base = CountingApprover(False)
    approver = SessionScopedApprover(base)
    tool = _tool("apply_patch")
    call = _call("apply_patch", path="a.py", new_content="x")
    assert asyncio.run(approver(call, tool)) is False
    assert approver.active_grants() == []


def test_state_change_invalidates_grant_via_fingerprint() -> None:
    base = CountingApprover(True)
    approver = SessionScopedApprover(base)
    tool = _tool("apply_patch")
    before = _call("apply_patch", path="a.py", new_content="x", base_sha256="hash-old")
    after = _call("apply_patch", path="a.py", new_content="x", base_sha256="hash-new")
    asyncio.run(approver(before, tool))
    asyncio.run(approver(after, tool))
    assert base.asked == 2


def test_tui_session_wires_session_scoped_approver_with_grants_command() -> None:
    from eurus.tui.commands import COMMANDS, resolve
    from eurus.tui.session import InteractiveSession

    assert any(command.name == "/grants" for command in COMMANDS)
    assert len(resolve("/grants")) == 1

    class StubRenderer:
        def __init__(self) -> None:
            self.tables: list[tuple[str, list]] = []
            self.messages: list[str] = []

        def table(self, title: str, rows: list) -> None:
            self.tables.append((title, rows))

        def system(self, text: str) -> None:
            self.messages.append(text)

        def error(self, text: str, hint: str = "") -> None:
            self.messages.append(f"error: {text}")

    session = InteractiveSession.__new__(InteractiveSession)
    session.renderer = StubRenderer()
    session.session_approver = SessionScopedApprover(CountingApprover(True), ttl_seconds=60)
    apply_patch = _tool("apply_patch")

    async def grant() -> None:
        await session.session_approver(
            _call("apply_patch", path="a.py", new_content="x"), apply_patch
        )

    asyncio.run(grant())
    grants = session.session_approver.active_grants()
    assert len(grants) == 1
    fingerprint = str(grants[0]["fingerprint"])

    session._grants("")
    title, rows = session.renderer.tables[-1]
    assert title == "Session approval grants"
    assert rows[0][0] == fingerprint

    session._grants(f"revoke {fingerprint}")
    assert session.session_approver.active_grants() == []
    assert any("Revoked" in message for message in session.renderer.messages)

    session._grants("revoke-all")
    assert any("Revoked 0" in message for message in session.renderer.messages)
