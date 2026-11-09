from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

import eurus.gateway.manager as manager_module
import eurus.gateway.telegram.adapter as telegram_adapter
import eurus.gateway.telegram.approval as telegram_approval
from eurus.gateway.approval import ApprovalBroker
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.router import (
    ACCESS_DENIED,
    ACCESS_DENIED_COMMAND,
    GatewayRouter,
    match_task_id,
    parse_command,
)
from eurus.gateway.security import GatewayAccessPolicy, permits_message
from eurus.gateway.tasks import DurableTask, TaskStore, scope_key
from eurus.gateway.telegram.adapter import TelegramAdapter, TelegramDeliveryError
from eurus.models import ToolCall
from eurus.tools.base import Tool, ToolContext

Runner = Callable[[DurableTask], Awaitable[str]]


async def _noop(arguments: dict[str, object], context: ToolContext) -> str:
    return ""


INVALID_TOKEN_REPLY = "Approval token is invalid, expired, or belongs to another chat."
GUARDED_TOOL = Tool("write_file", "Write a file", {}, _noop, mutates=True)


class RecordingAdapter:
    def __init__(self, platform: str = "test") -> None:
        self._platform = platform
        self.sent: list[tuple[str, str]] = []
        self.fail = False
        self.started = 0
        self.stopped = 0

    @property
    def platform(self) -> str:
        return self._platform

    async def send_text(self, conversation_id: str, text: str) -> None:
        if self.fail:
            raise OSError("delivery down")
        self.sent.append((conversation_id, text))

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        allowed: set[str] | None = None,
        runner: Runner | None = None,
        max_concurrent: int = 2,
        platform: str = "test",
        store: TaskStore | None = None,
        adapter: RecordingAdapter | None = None,
    ) -> None:
        self.adapter = adapter or RecordingAdapter(platform)
        self.store = store or TaskStore(tmp_path / "tasks.sqlite3")
        self.allowed = allowed if allowed is not None else {"1", "2"}
        self.counter = itertools.count(1)

        async def default_runner(task: DurableTask) -> str:
            return f"done:{task.objective}"

        self.manager = BackgroundTaskManager(
            self.store,
            runner or default_runner,
            self.adapter.send_text,
            max_concurrent=max_concurrent,
        )
        self.broker = ApprovalBroker(timeout=30.0)
        self.broker.bind_notifier(self.adapter.send_text)
        self.manager.approval_broker = self.broker
        self.router = GatewayRouter(
            adapter=self.adapter,
            access=lambda message: message.user_id in self.allowed,
            store=self.store,
            manager=self.manager,
            broker=self.broker,
        )

    def message(
        self,
        text: str,
        *,
        user: str = "1",
        conversation: str = "10",
        message_id: str | None = None,
        platform: str | None = None,
        group: bool = False,
    ) -> GatewayMessage:
        return GatewayMessage(
            platform or self.adapter.platform,
            user,
            conversation,
            message_id or str(next(self.counter)),
            text,
            group,
        )

    async def say(self, text: str, **options: object) -> None:
        await self.router.handle(self.message(text, **options))

    async def settle(self) -> None:
        workers = list(self.manager.active.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def close(self) -> None:
        await self.manager.shutdown()
        await self.broker.close()


def test_parse_command_normalizes_bot_mentions_and_case() -> None:
    assert parse_command("/Status@EurusBot extra") == "/status"
    assert parse_command("/new") == "/new"
    assert parse_command("hello /new") is None
    assert parse_command("") is None


def test_scope_key_keeps_legacy_telegram_ids_and_namespaces_other_platforms() -> None:
    assert scope_key("telegram", "42") == "42"
    assert scope_key("discord", "42") == "discord:42"
    with pytest.raises(ValueError):
        scope_key("", "42")
    with pytest.raises(ValueError):
        scope_key("discord", " ")


def test_store_keeps_telegram_session_ids_stable_and_isolates_platforms(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    assert store.current_session("42", "100") == "telegram:42:100"
    assert store.current_session("42", "100", "discord") == "discord:42:100"
    assert store.reset_session("42", "100").startswith("telegram:42:100:")
    assert store.current_session("42", "100", "discord") == "discord:42:100"
    store.set_chat_locked("42", "100", True, "discord")
    assert store.is_chat_locked("42", "100", "discord")
    assert not store.is_chat_locked("42", "100")
    store.set_chat_locked("42", "100", True)
    store.set_chat_locked("42", "100", False, "discord")
    assert store.is_chat_locked("42", "100")
    assert not store.is_chat_locked("42", "100", "discord")


def test_permits_message_parses_numeric_ids_and_fails_closed() -> None:
    policy = GatewayAccessPolicy({42}, {-100})
    assert permits_message(policy, GatewayMessage("telegram", "42", "42", "1", "hi"))
    assert not permits_message(policy, GatewayMessage("telegram", "43", "43", "1", "hi"))
    assert not permits_message(policy, GatewayMessage("telegram", "abc", "42", "1", "hi"))
    assert not permits_message(policy, GatewayMessage("telegram", "42", "x", "1", "hi"))
    assert permits_message(policy, GatewayMessage("telegram", "42", "-100", "1", "hi", True))
    assert not permits_message(policy, GatewayMessage("telegram", "42", "-200", "1", "hi", True))


def test_compatibility_aliases_point_at_the_shared_implementations() -> None:
    assert telegram_adapter.BackgroundTaskManager is manager_module.BackgroundTaskManager
    assert telegram_approval.TelegramApprovalBroker is ApprovalBroker
    assert telegram_adapter.match_task_id is match_task_id


@pytest.mark.asyncio
async def test_authorized_text_runs_a_task_and_delivers_the_result(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.say("write a poem")
    await rig.settle()

    assert rig.adapter.sent == [("10", "done:write a poem")]
    tasks = rig.store.list_owner(scope_key("test", "1"))
    assert [task.status for task in tasks] == ["completed"]
    assert tasks[0].session_id == "test:1:10"
    await rig.close()


@pytest.mark.asyncio
async def test_unauthorized_users_are_denied_everywhere_and_create_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, allowed={"1"})
    await rig.say("run something", user="9")
    await rig.say("/new", user="9")
    await rig.say("/approve sometoken123", user="9")
    await rig.say("/unknowncommand", user="9")

    assert rig.adapter.texts() == [
        ACCESS_DENIED,
        ACCESS_DENIED_COMMAND,
        ACCESS_DENIED,
        ACCESS_DENIED,
    ]
    assert rig.store.list_owner(scope_key("test", "9")) == []
    assert rig.manager.active == {}
    await rig.close()


@pytest.mark.asyncio
async def test_empty_messages_and_unknown_commands_are_ignored_for_authorized_users(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    await rig.say("   ")
    await rig.say("/unknowncommand arg")
    assert rig.adapter.sent == []
    assert rig.store.list_owner(scope_key("test", "1")) == []
    await rig.close()


@pytest.mark.asyncio
async def test_duplicate_deliveries_of_one_message_start_one_task(tmp_path: Path) -> None:
    calls: list[str] = []

    async def runner(task: DurableTask) -> str:
        calls.append(task.objective)
        return "ok"

    rig = Rig(tmp_path, runner=runner)
    await rig.say("once", message_id="m-1")
    await rig.say("once", message_id="m-1")
    await rig.settle()
    assert calls == ["once"]
    assert rig.adapter.texts() == ["ok"]
    await rig.close()


@pytest.mark.asyncio
async def test_locked_chat_refuses_work_until_unlocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.say("/lock")
    await rig.say("blocked")
    assert rig.adapter.texts()[-1] == "This conversation is paused. Use `/unlock` to continue."
    assert rig.store.list_owner(scope_key("test", "1")) == []
    await rig.say("/unlock")
    await rig.say("allowed")
    await rig.settle()
    assert [task.objective for task in rig.store.list_owner(scope_key("test", "1"))] == ["allowed"]
    await rig.close()


@pytest.mark.asyncio
async def test_new_command_switches_session_and_keeps_history_attributable(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    await rig.say("first")
    await rig.settle()
    await rig.say("/new")
    await rig.say("second")
    await rig.settle()
    sessions = {
        task.objective: task.session_id for task in rig.store.list_owner(scope_key("test", "1"))
    }
    assert sessions["first"] == "test:1:10"
    assert sessions["second"].startswith("test:1:10:")
    assert sessions["second"] != sessions["first"]
    await rig.close()


@pytest.mark.asyncio
async def test_status_only_lists_the_callers_tasks(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.say("alpha job", user="1")
    await rig.say("beta job", user="2", conversation="20")
    await rig.settle()
    rig.adapter.sent.clear()
    await rig.say("/status", user="1")
    status = rig.adapter.texts()[0]
    assert "alpha job" in status and "beta job" not in status
    await rig.close()


@pytest.mark.asyncio
async def test_cancel_requires_a_unique_prefix_and_ownership(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def runner(task: DurableTask) -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    rig = Rig(tmp_path, runner=runner)
    await rig.say("long job", user="1")
    await started.wait()
    owner = scope_key("test", "1")
    task = rig.store.list_owner(owner)[0]
    rig.adapter.sent.clear()

    await rig.say("/cancel", user="1")
    await rig.say("/cancel a", user="1")
    await rig.say(f"/cancel {task.task_id[:8]}", user="2")
    assert rig.adapter.texts() == [
        "Usage: /cancel TASK_ID",
        "Task ID must be at least 6 hexadecimal characters.",
        "Task not found or cannot be cancelled.",
    ]
    assert rig.store.get(task.task_id) is not None

    await rig.say(f"/cancel {task.task_id[:8]}", user="1")
    await rig.settle()
    cancelled = rig.store.get(task.task_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert rig.adapter.texts()[3] == "**Cancellation requested.**"
    await rig.close()


@pytest.mark.asyncio
async def test_background_capacity_is_reported_to_the_user(tmp_path: Path) -> None:
    gate = asyncio.Event()

    async def runner(task: DurableTask) -> str:
        await gate.wait()
        return "ok"

    rig = Rig(tmp_path, runner=runner, max_concurrent=1)
    for index in range(8):
        await rig.say(f"job {index}")
    await rig.say("one too many")
    assert rig.adapter.texts()[-1] == "Eurus is at its background task capacity. Try again shortly."
    gate.set()
    await rig.settle()
    await rig.close()


@pytest.mark.asyncio
async def test_reply_delivery_failure_never_breaks_message_handling(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.adapter.fail = True
    await rig.say("/help")
    await rig.say("still works")
    await rig.settle()
    assert rig.store.list_owner(scope_key("test", "1"))[0].status in {"completed", "failed"}
    await rig.close()


@pytest.mark.asyncio
async def test_approval_flow_runs_end_to_end_through_the_router(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        allowed = await rig.broker.approve(ToolCall("c", "write_file", {"path": "a"}), GUARDED_TOOL)
        return f"approved:{allowed}"

    rig = Rig(tmp_path, runner=runner)
    await rig.say("needs approval", user="1")
    for _ in range(100):
        if any("Owner approval required" in text for text in rig.adapter.texts()):
            break
        await asyncio.sleep(0.01)
    prompt = next(text for text in rig.adapter.texts() if "Owner approval required" in text)
    token = prompt.split("/approve ", 1)[1].splitlines()[0]

    rig.adapter.sent.clear()
    await rig.say(f"/approve {token}", user="2", conversation="10")
    assert rig.adapter.texts() == [INVALID_TOKEN_REPLY]
    await rig.say(f"/approve {token}", user="1", conversation="999")
    assert rig.adapter.texts()[-1] == INVALID_TOKEN_REPLY

    await rig.say(f"/approve {token}", user="1", conversation="10")
    assert rig.adapter.texts()[-1] == "**Approval accepted.**"
    await rig.settle()
    assert ("10", "approved:True") in rig.adapter.sent
    await rig.say(f"/approve {token}", user="1", conversation="10")
    assert rig.adapter.texts()[-1] == INVALID_TOKEN_REPLY
    await rig.close()


@pytest.mark.asyncio
async def test_deny_and_usage_and_lockout_messages(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        allowed = await rig.broker.approve(ToolCall("c", "write_file", {}), GUARDED_TOOL)
        return f"approved:{allowed}"

    rig = Rig(tmp_path, runner=runner)
    await rig.say("needs approval")
    for _ in range(100):
        if any("Owner approval required" in text for text in rig.adapter.texts()):
            break
        await asyncio.sleep(0.01)
    prompt = next(text for text in rig.adapter.texts() if "Owner approval required" in text)
    token = prompt.split("/deny ", 1)[1].splitlines()[0]

    await rig.say("/approve")
    assert rig.adapter.texts()[-1] == "Usage: /approve TOKEN or /deny TOKEN"
    await rig.say(f"/deny {token}")
    assert rig.adapter.texts()[-1] == "**Action declined.**"
    await rig.settle()
    assert ("10", "approved:False") in rig.adapter.sent

    for attempt in range(5):
        await rig.say(f"/approve badtoken{attempt}x")
    await rig.say("/approve anothertoken1")
    assert rig.adapter.texts()[-1] == "Too many invalid approval attempts. Try again later."
    await rig.close()


@pytest.mark.asyncio
async def test_two_platforms_with_the_same_raw_ids_never_share_state(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    telegram = Rig(tmp_path, platform="telegram", store=store)
    discord = Rig(tmp_path, platform="discord", store=store)

    await telegram.say("telegram job", user="1", conversation="10")
    await discord.say("discord job", user="1", conversation="10")
    await telegram.settle()
    await discord.settle()

    telegram_tasks = store.list_owner(scope_key("telegram", "1"))
    discord_tasks = store.list_owner(scope_key("discord", "1"))
    assert [task.objective for task in telegram_tasks] == ["telegram job"]
    assert [task.objective for task in discord_tasks] == ["discord job"]
    assert telegram_tasks[0].session_id == "telegram:1:10"
    assert discord_tasks[0].session_id == "discord:1:10"

    await telegram.say("/lock", user="1", conversation="10")
    await discord.say("still open", user="1", conversation="10")
    await discord.settle()
    assert len(store.list_owner(scope_key("discord", "1"))) == 2
    await telegram.close()
    await discord.close()


@pytest.mark.asyncio
async def test_other_platform_cannot_resolve_an_approval_with_the_same_raw_ids(
    tmp_path: Path,
) -> None:
    async def runner(task: DurableTask) -> str:
        allowed = await telegram.broker.approve(ToolCall("c", "write_file", {}), GUARDED_TOOL)
        return f"approved:{allowed}"

    store = TaskStore(tmp_path / "tasks.sqlite3")
    telegram = Rig(tmp_path, platform="telegram", store=store, runner=runner)
    spoofer = Rig(tmp_path, platform="discord", store=store)
    spoofer.broker = telegram.broker
    spoofer.router = GatewayRouter(
        adapter=spoofer.adapter,
        access=lambda message: True,
        store=store,
        manager=spoofer.manager,
        broker=telegram.broker,
    )

    await telegram.say("needs approval", user="1", conversation="10")
    for _ in range(100):
        if any("Owner approval required" in text for text in telegram.adapter.texts()):
            break
        await asyncio.sleep(0.01)
    prompt = next(text for text in telegram.adapter.texts() if "Owner approval" in text)
    token = prompt.split("/approve ", 1)[1].splitlines()[0]

    await spoofer.say(f"/approve {token}", user="1", conversation="10")
    assert spoofer.adapter.texts()[-1] == INVALID_TOKEN_REPLY
    assert token in telegram.broker.pending
    await telegram.say(f"/deny {token}", user="1", conversation="10")
    await telegram.settle()
    await telegram.close()
    await spoofer.close()


def test_match_task_id_is_reexported_with_strict_semantics() -> None:
    assert match_task_id(["abcdef0123456789abcdef0123456789"], "abcdef")[0] is not None
    assert match_task_id(["abcdef0123456789abcdef0123456789"], "abc")[0] is None


@pytest.mark.asyncio
async def test_telegram_adapter_implements_the_gateway_contract() -> None:
    sent: list[tuple[int, str]] = []
    events: list[str] = []

    async def notifier(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    async def poll() -> None:
        events.append("poll")

    async def close() -> None:
        events.append("close")

    adapter = TelegramAdapter(notifier, poll=poll, close=close)
    assert adapter.platform == "telegram"
    await adapter.send_text("-1001", "hello")
    assert sent == [(-1001, "hello")]
    with pytest.raises(TelegramDeliveryError):
        await adapter.send_text("not-a-number", "hello")
    await adapter.start()
    await adapter.stop()
    assert events == ["poll", "close"]


@pytest.mark.asyncio
async def test_manager_passes_string_conversation_ids_to_the_notifier(tmp_path: Path) -> None:
    seen: list[object] = []

    async def notify(conversation_id: str, text: str) -> None:
        seen.append(conversation_id)

    async def runner(task: DurableTask) -> str:
        return "ok"

    manager = BackgroundTaskManager(TaskStore(tmp_path / "tasks.sqlite3"), runner, notify)
    task = await manager.submit("discord:5", "s", "go", notify_chat_id="channel-9")
    await manager.active[task.task_id]
    assert seen == ["channel-9"]
    fallback = await manager.submit("owner-7", "s", "go")
    await manager.active[fallback.task_id]
    assert seen == ["channel-9", "owner-7"]
    await manager.shutdown()
