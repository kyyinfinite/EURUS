from __future__ import annotations

import asyncio

import pytest

from eurus.gateway.telegram.approval import (
    ApprovalContext,
    TelegramApprovalBroker,
    current_approval_context,
)
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext


async def _handler(arguments: dict[str, object], context: ToolContext) -> str:
    return str(arguments)


@pytest.mark.asyncio
async def test_approval_requires_matching_owner_and_chat() -> None:
    broker = TelegramApprovalBroker(timeout=1.0)
    messages: list[tuple[int, str]] = []

    async def notify(chat_id: int, text: str) -> None:
        messages.append((chat_id, text))

    broker.bind_notifier(notify)
    context_token = current_approval_context.set(ApprovalContext("task-1", "42", 100))
    tool = Tool("write_file", "Write a file", {}, _handler, mutates=True)
    approval = asyncio.create_task(
        broker.approve(ToolCall("call-1", "write_file", {"path": "a.txt"}), tool)
    )
    try:
        for _ in range(20):
            if messages:
                break
            await asyncio.sleep(0)
        token = messages[0][1].split("/approve ", 1)[1].splitlines()[0]
        assert await broker.resolve(token, owner_id="7", chat_id=100, allowed=True) is False
        assert await broker.resolve(token, owner_id="42", chat_id=101, allowed=True) is False
        assert await broker.resolve(token, owner_id="42", chat_id=100, allowed=True) is True
        assert await approval is True
        assert await broker.resolve(token, owner_id="42", chat_id=100, allowed=True) is False
    finally:
        current_approval_context.reset(context_token)
        await broker.close()


@pytest.mark.asyncio
async def test_approval_fails_closed_without_task_context() -> None:
    broker = TelegramApprovalBroker(timeout=0.1)
    tool = Tool("write_file", "Write a file", {}, _handler, mutates=True)
    assert await broker.approve(ToolCall("call-1", "write_file"), tool) is False
    await broker.close()


@pytest.mark.asyncio
async def test_approval_timeout_denies_action() -> None:
    broker = TelegramApprovalBroker(timeout=0.01)

    async def notify(chat_id: int, text: str) -> None:
        return None

    broker.bind_notifier(notify)
    context_token = current_approval_context.set(ApprovalContext("task-2", "42", 100))
    tool = Tool("write_file", "Write a file", {}, _handler, mutates=True)
    try:
        result = await broker.approve(ToolCall("call-2", "write_file"), tool)
        assert result is False
        assert broker.pending == {}
    finally:
        current_approval_context.reset(context_token)
        await broker.close()


@pytest.mark.asyncio
async def test_persisted_approval_decision_is_recorded(tmp_path) -> None:
    import sqlite3

    database = tmp_path / "gateway.sqlite3"
    broker = TelegramApprovalBroker(timeout=1.0, persistence_path=database)
    messages: list[tuple[str, str]] = []

    async def notify(chat_id: str, text: str) -> None:
        messages.append((chat_id, text))

    broker.bind_notifier(notify)
    context_token = current_approval_context.set(
        ApprovalContext("task-persist", "owner-1", "chat-1")
    )
    tool = Tool("write_file", "Write a file", {}, _handler, mutates=True)
    approval = asyncio.create_task(
        broker.approve(ToolCall("call-persist", "write_file", {"path": "safe.txt"}), tool)
    )
    try:
        for _ in range(30):
            if messages:
                break
            await asyncio.sleep(0)
        assert messages
        token = messages[0][1].split("/approve ", 1)[1].splitlines()[0]
        assert await broker.resolve(token, owner_id="owner-1", chat_id="chat-1", allowed=True)
        assert await approval is True
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT status, decision, fingerprint FROM gateway_approval_requests"
            ).fetchone()
        assert row is not None
        assert row[0] == "approved"
        assert row[1] == 1
        assert len(row[2]) == 64
    finally:
        current_approval_context.reset(context_token)
        await broker.close()


@pytest.mark.asyncio
async def test_restart_invalidates_orphaned_approval_requests(tmp_path) -> None:
    import sqlite3

    database = tmp_path / "gateway.sqlite3"
    broker = TelegramApprovalBroker(timeout=1.0, persistence_path=database)
    context_token = current_approval_context.set(
        ApprovalContext("task-restart", "owner-2", "chat-2")
    )

    async def notify(chat_id: str, text: str) -> None:
        return None

    broker.bind_notifier(notify)
    tool = Tool("write_file", "Write a file", {}, _handler, mutates=True)
    try:
        approval = asyncio.create_task(
            broker.approve(ToolCall("call-restart", "write_file", {"path": "x"}), tool)
        )
        for _ in range(30):
            if broker.pending:
                break
            await asyncio.sleep(0)
        assert broker.pending
        await broker.close()
        await asyncio.gather(approval, return_exceptions=True)
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE gateway_approval_requests SET status='pending' WHERE status='cancelled'"
            )
        TelegramApprovalBroker(timeout=1.0, persistence_path=database)
        with sqlite3.connect(database) as db:
            status = db.execute("SELECT status FROM gateway_approval_requests").fetchone()[0]
        assert status == "interrupted"
    finally:
        current_approval_context.reset(context_token)
