from __future__ import annotations

import asyncio
import logging

import pytest

from eurus.gateway.telegram.adapter import TelegramNotifier, match_task_id
from eurus.gateway.telegram.approval import (
    ApprovalContext,
    TelegramApprovalBroker,
    current_approval_context,
    parse_approval_timeout,
)
from eurus.models import ToolCall
from eurus.security.redaction import redact_arguments, redact_text, scrub_secrets
from eurus.tools.base import Tool, ToolContext


async def _handler(arguments: dict[str, object], context: ToolContext) -> str:
    return str(arguments)


WRITE_TOOL = Tool("write_file", "Write a file", {}, _handler, mutates=True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, broker: TelegramApprovalBroker) -> None:
        self.broker = broker
        self.messages: list[tuple[int, str]] = []
        broker.bind_notifier(self.notify)

    async def notify(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def start(
        self,
        call: ToolCall | None = None,
        *,
        task_id: str = "task-1",
        owner_id: str = "42",
        chat_id: int = 100,
    ) -> tuple[asyncio.Task[bool], str]:
        baseline = len(self.messages)
        reset = current_approval_context.set(ApprovalContext(task_id, owner_id, chat_id))
        try:
            request = call or ToolCall("c", "write_file", {"path": "a.txt"})
            task = asyncio.create_task(self.broker.approve(request, WRITE_TOOL))
        finally:
            current_approval_context.reset(reset)
        for _ in range(50):
            if len(self.messages) > baseline:
                break
            await asyncio.sleep(0)
        text = self.messages[-1][1]
        return task, text.split("/approve ", 1)[1].splitlines()[0]


@pytest.mark.asyncio
async def test_other_user_cannot_approve_and_owner_still_can() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    task, token = await harness.start()
    assert not await harness.broker.resolve(token, owner_id="99", chat_id=100, allowed=True)
    assert not task.done()
    assert token in harness.broker.pending
    assert await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert await task is True
    await harness.broker.close()


@pytest.mark.asyncio
async def test_approval_is_bound_to_the_requesting_chat() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    task, token = await harness.start(chat_id=100)
    assert not await harness.broker.resolve(token, owner_id="42", chat_id=555, allowed=True)
    assert await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=False)
    assert await task is False
    await harness.broker.close()


@pytest.mark.asyncio
async def test_tokens_cannot_be_replayed_after_use_or_denial() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    task, token = await harness.start()
    assert await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert not await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=False)
    assert await task is True
    assert not await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert harness.broker.pending == {}
    await harness.broker.close()


@pytest.mark.asyncio
async def test_expired_tokens_are_rejected_even_before_the_timer_fires() -> None:
    clock = FakeClock()
    harness = Harness(TelegramApprovalBroker(timeout=60.0, clock=clock))
    task, token = await harness.start()
    clock.now += 61.0
    assert not await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert not task.done()
    await harness.broker.cancel_task("task-1")
    assert await task is False
    await harness.broker.close()


@pytest.mark.asyncio
async def test_malformed_tokens_are_rejected_without_lookup() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0, max_failed_attempts=100))
    task, token = await harness.start()
    for bad in ("", "short", "x" * 65, "has space token", token + "!", "../../etc/passwd"):
        assert not await harness.broker.resolve(bad, owner_id="42", chat_id=100, allowed=True)
    assert not task.done()
    assert await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert await task is True
    await harness.broker.close()


@pytest.mark.asyncio
async def test_repeated_invalid_attempts_lock_out_only_that_user() -> None:
    clock = FakeClock()
    harness = Harness(
        TelegramApprovalBroker(
            timeout=3000.0, clock=clock, max_failed_attempts=3, lockout_seconds=300
        )
    )
    task, token = await harness.start(owner_id="42")
    other_task, other_token = await harness.start(task_id="task-2", owner_id="7", chat_id=200)

    for _ in range(3):
        assert not await harness.broker.resolve(
            "invalidtoken1", owner_id="42", chat_id=100, allowed=True
        )
    assert harness.broker.is_locked_out("42")
    assert not harness.broker.is_locked_out("7")
    assert not await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert not task.done()
    assert await harness.broker.resolve(other_token, owner_id="7", chat_id=200, allowed=True)
    assert await other_task is True

    clock.now += 301.0
    assert not harness.broker.is_locked_out("42")
    assert await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert await task is True
    await harness.broker.close()


@pytest.mark.asyncio
async def test_failures_outside_the_window_do_not_trigger_lockout() -> None:
    clock = FakeClock()
    broker = TelegramApprovalBroker(clock=clock, max_failed_attempts=3, failure_window=10.0)
    for _ in range(5):
        assert not await broker.resolve("invalidtoken1", owner_id="42", chat_id=1, allowed=True)
        clock.now += 11.0
    assert not broker.is_locked_out("42")
    await broker.close()


@pytest.mark.asyncio
async def test_pending_approval_limits_deny_excess_requests_immediately() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0, max_pending_per_owner=2, max_pending=3))
    first, _ = await harness.start(task_id="t1")
    second, _ = await harness.start(task_id="t2")
    sent = len(harness.messages)

    reset = current_approval_context.set(ApprovalContext("t3", "42", 100))
    try:
        assert await harness.broker.approve(ToolCall("c", "write_file", {}), WRITE_TOOL) is False
    finally:
        current_approval_context.reset(reset)
    assert len(harness.messages) == sent
    assert len(harness.broker.pending) == 2

    third, _ = await harness.start(task_id="t4", owner_id="7", chat_id=300)
    reset = current_approval_context.set(ApprovalContext("t5", "8", 400))
    try:
        assert await harness.broker.approve(ToolCall("c", "write_file", {}), WRITE_TOOL) is False
    finally:
        current_approval_context.reset(reset)
    await harness.broker.close()
    assert [await first, await second, await third] == [False, False, False]


@pytest.mark.asyncio
async def test_notifier_failure_denies_and_cleans_up() -> None:
    states: list[tuple[str, bool]] = []

    async def state(task_id: str, waiting: bool) -> None:
        states.append((task_id, waiting))

    async def broken(chat_id: int, text: str) -> None:
        raise OSError("network down")

    broker = TelegramApprovalBroker(timeout=5.0, state_callback=state)
    broker.bind_notifier(broken)
    reset = current_approval_context.set(ApprovalContext("task-1", "42", 100))
    try:
        assert await broker.approve(ToolCall("c", "write_file", {}), WRITE_TOOL) is False
    finally:
        current_approval_context.reset(reset)
    assert broker.pending == {}
    assert states == [("task-1", True), ("task-1", False)]
    await broker.close()


@pytest.mark.asyncio
async def test_state_callback_failure_does_not_mask_the_decision_cleanup() -> None:
    async def state(task_id: str, waiting: bool) -> None:
        if not waiting:
            raise RuntimeError("store unavailable")

    broker = TelegramApprovalBroker(timeout=0.01, state_callback=state)
    harness = Harness(broker)
    reset = current_approval_context.set(ApprovalContext("task-1", "42", 100))
    try:
        assert await broker.approve(ToolCall("c", "write_file", {}), WRITE_TOOL) is False
    finally:
        current_approval_context.reset(reset)
    assert broker.pending == {}
    assert harness.messages
    await broker.close()


@pytest.mark.asyncio
async def test_prompt_redacts_secrets_and_identifies_the_action() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    call = ToolCall(
        "c",
        "write_file",
        {
            "path": "deploy.env",
            "api_key": "sk-supersecretvalue123",
            "content": "TOKEN=abc123456 and ghp_abcdefghijklmnopqrstuvwxyz",
        },
    )
    task, token = await harness.start(call)
    prompt = harness.messages[-1][1]
    assert "sk-supersecretvalue123" not in prompt
    assert "abc123456" not in prompt
    assert "ghp_abc" not in prompt
    assert "REDACTED" in prompt and "deploy.env" in prompt
    assert "Tool: write_file" in prompt
    assert "Action: " in prompt
    assert harness.broker.pending[token].fingerprint
    await harness.broker.cancel_task("task-1")
    assert await task is False
    await harness.broker.close()


@pytest.mark.asyncio
async def test_prompt_discloses_when_arguments_are_truncated() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    call = ToolCall("c", "write_file", {"content": "x" * 5000})
    task, _ = await harness.start(call)
    assert "more characters not shown" in harness.messages[-1][1]
    small_task, _ = await harness.start(ToolCall("c2", "write_file", {"path": "a"}), task_id="t2")
    assert "not shown" not in harness.messages[-1][1]
    await harness.broker.close()
    assert [await task, await small_task] == [False, False]


@pytest.mark.asyncio
async def test_different_calls_get_different_fingerprints_and_tokens() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    first, token_a = await harness.start(ToolCall("c", "write_file", {"path": "a"}), task_id="a")
    second, token_b = await harness.start(ToolCall("c", "write_file", {"path": "b"}), task_id="b")
    assert token_a != token_b
    pending = harness.broker.pending
    assert pending[token_a].fingerprint != pending[token_b].fingerprint
    await harness.broker.close()
    assert [await first, await second] == [False, False]


@pytest.mark.asyncio
async def test_task_cancellation_and_shutdown_leave_no_pending_approvals() -> None:
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    first, _ = await harness.start(task_id="t1")
    second, _ = await harness.start(task_id="t2")
    await harness.broker.cancel_task("t1")
    assert await first is False
    assert not second.done()
    await harness.broker.close()
    assert await second is False
    assert harness.broker.pending == {}


@pytest.mark.asyncio
async def test_audit_log_never_contains_tokens_or_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="eurus.gateway.approval")
    harness = Harness(TelegramApprovalBroker(timeout=5.0))
    call = ToolCall("c", "write_file", {"api_key": "sk-supersecretvalue123", "path": "a"})
    task, token = await harness.start(call)
    await harness.broker.resolve(token, owner_id="42", chat_id=100, allowed=True)
    assert await task is True
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "task_id=task-1" in joined and "allowed=True" in joined
    assert token not in joined
    assert "sk-supersecretvalue123" not in joined
    await harness.broker.close()


def test_approval_timeout_parsing_and_limits() -> None:
    assert parse_approval_timeout("120") == 120.0
    assert parse_approval_timeout("0.5") == 0.5
    for bad in ("abc", "", "0", "-5", "3601", "nan"):
        with pytest.raises(ValueError):
            parse_approval_timeout(bad)
    with pytest.raises(ValueError):
        TelegramApprovalBroker(timeout=3601)
    with pytest.raises(ValueError):
        TelegramApprovalBroker(max_pending=0)
    with pytest.raises(ValueError):
        TelegramApprovalBroker(max_failed_attempts=0)


def test_scrub_secrets_masks_known_token_formats_only() -> None:
    bot_token = "123456789:" + "A" * 35
    text = f"key sk-abcdefghijk bot {bot_token} and ghp_{'b' * 24} normal token: value"
    scrubbed = scrub_secrets(text)
    assert "sk-abcdefghijk" not in scrubbed
    assert bot_token not in scrubbed
    assert "ghp_" not in scrubbed
    assert "normal token: value" in scrubbed
    assert redact_text("token: value") == "token:REDACTED"


def test_redact_arguments_masks_nested_sensitive_values() -> None:
    redacted = redact_arguments({"headers": {"Authorization": "Bearer abcdefghij"}, "n": 3})
    assert redacted == {"headers": {"Authorization": "REDACTED"}, "n": 3}


@pytest.mark.asyncio
async def test_notifier_scrubs_secrets_from_every_outgoing_message() -> None:
    sent: list[str] = []

    async def sender(chat_id: int, text: str) -> None:
        sent.append(text)

    notifier = TelegramNotifier(sender, global_interval=0, chat_interval=0, group_interval=0)
    await notifier(1, "result: sk-abcdefghijklmnop and done")
    assert sent == ["result: REDACTED and done"]


def test_cancel_prefix_matching_is_strict_and_unambiguous() -> None:
    ids = ["abcdef0123456789abcdef0123456789", "abcdef9999999999abcdef9999999999", "12345678" * 4]
    assert match_task_id(ids, "123456") == (ids[2], "")
    assert match_task_id(ids, " 123456 ")[0] == ids[2]
    assert match_task_id(ids, "ABCDEF01")[0] == ids[0]
    assert match_task_id(ids, "abcdef")[0] is None
    assert "ambiguous" in match_task_id(ids, "abcdef")[1]
    assert match_task_id(ids, "a")[0] is None
    assert match_task_id(ids, "")[0] is None
    assert match_task_id(ids, "zzzzzz")[0] is None
    assert match_task_id(ids, "ffffff") == (None, "Task not found or cannot be cancelled.")
    assert match_task_id([], "abcdef")[0] is None
