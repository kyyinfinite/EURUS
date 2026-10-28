from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

from eurus.gateway.approval import ApprovalBroker
from eurus.gateway.contracts import GatewayAdapter, GatewayMessage
from eurus.gateway.limits import RateLimiter
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.tasks import RecoveryBlocked, TaskStore, scope_key
from eurus.observability import bind_context, unbind_context

log = logging.getLogger(__name__)

TASK_PREFIX_PATTERN = re.compile(r"[0-9a-f]{6,32}")
SESSION_COMMANDS = frozenset(
    {"/start", "/help", "/new", "/status", "/cancel", "/lock", "/unlock", "/pause", "/resume"}
)
APPROVAL_COMMANDS = frozenset({"/approve", "/deny"})
ACCESS_DENIED = "Access denied."
ACCESS_DENIED_COMMAND = (
    "Access denied. Ask the Eurus administrator to configure your numeric user ID."
)
NEW_SESSION_TEXT = "**Fresh conversation started.**\nYour earlier session history is preserved."
HELP_TEXT = (
    "**Eurus Assistant**\n\n"
    "Chat with me naturally. I can help with coding, research, writing, "
    "planning, and working with files.\n\n"
    "**Quick commands**\n"
    "`/new`  Start a fresh conversation\n"
    "`/status`  View recent tasks\n"
    "`/cancel TASK_ID`  Cancel a task\n"
    "`/pause TASK_ID`  Pause a durable task\n"
    "`/resume TASK_ID`  Resume a paused or interrupted task\n"
    "`/lock` / `/unlock`  Pause or resume chat access\n\n"
    "Send a message whenever you are ready."
)

AccessCheck = Callable[[GatewayMessage], bool]


def match_task_id(task_ids: Sequence[str], prefix: str) -> tuple[str | None, str]:
    candidate = prefix.strip().lower()
    if not TASK_PREFIX_PATTERN.fullmatch(candidate):
        return None, "Task ID must be at least 6 hexadecimal characters."
    matches = [task_id for task_id in task_ids if task_id.startswith(candidate)]
    if not matches:
        return None, "Task not found or cannot be cancelled."
    if len(matches) > 1:
        return None, "Task ID is ambiguous. Use more characters."
    return matches[0], ""


def parse_command(text: str) -> str | None:
    head = text.split(maxsplit=1)[0] if text else ""
    if not head.startswith("/"):
        return None
    return head.split("@", 1)[0].lower()


class GatewayRouter:
    def __init__(
        self,
        *,
        adapter: GatewayAdapter,
        access: AccessCheck,
        store: TaskStore,
        manager: BackgroundTaskManager,
        broker: ApprovalBroker,
        submit_limiter: RateLimiter | None = None,
    ) -> None:
        self.submit_limiter = submit_limiter
        self.adapter = adapter
        self.access = access
        self.store = store
        self.manager = manager
        self.broker = broker

    async def handle(self, message: GatewayMessage) -> None:
        token = bind_context(
            platform=message.platform,
            correlation_id=f"{message.platform}:{message.message_id}",
        )
        try:
            await self._handle(message)
        finally:
            unbind_context(token)

    async def _handle(self, message: GatewayMessage) -> None:
        text = message.text.strip()
        if not text:
            return
        command = parse_command(text)
        if not self.access(message):
            log.warning(
                "Gateway access denied platform=%s user_id=%s conversation_id=%s group=%s",
                message.platform,
                message.user_id,
                message.conversation_id,
                message.is_group,
            )
            denial = ACCESS_DENIED_COMMAND if command in SESSION_COMMANDS else ACCESS_DENIED
            await self._reply(message, denial)
            return
        if command in SESSION_COMMANDS:
            await self._session_command(message, command, text)
        elif command in APPROVAL_COMMANDS:
            await self._approval_command(message, command, text)
        elif command is None:
            await self._submit_task(message, text)

    async def _reply(self, message: GatewayMessage, text: str) -> None:
        try:
            await self.adapter.send_text(message.conversation_id, text)
        except Exception as exc:
            log.warning(
                "Gateway reply failed platform=%s error_type=%s",
                message.platform,
                type(exc).__name__,
            )

    async def _session_command(self, message: GatewayMessage, command: str, text: str) -> None:
        platform = message.platform
        user_id = message.user_id
        conversation_id = message.conversation_id
        owner = scope_key(platform, user_id)
        if command in {"/start", "/help"}:
            await self._reply(message, HELP_TEXT)
        elif command == "/new":
            self.store.reset_session(user_id, conversation_id, platform)
            await self._reply(message, NEW_SESSION_TEXT)
        elif command == "/lock":
            self.store.set_chat_locked(user_id, conversation_id, True, platform)
            await self._reply(
                message, "**Chat paused.** Use `/unlock` when you are ready to continue."
            )
        elif command == "/unlock":
            self.store.set_chat_locked(user_id, conversation_id, False, platform)
            await self._reply(message, "**You’re back.** Send me a message whenever you are ready.")
        elif command == "/status":
            items = self.store.list_owner(owner, 10)
            lines = [
                f"{item.task_id[:8]} · {item.status} · {item.objective[:90]}" for item in items
            ]
            await self._reply(message, "\n".join(lines) or "No tasks yet.")
        elif command == "/cancel":
            await self._cancel(message, owner, text)
        elif command == "/pause":
            await self._pause(message, owner, text)
        elif command == "/resume":
            await self._resume(message, owner, text)

    async def _pause(self, message: GatewayMessage, owner: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self._reply(message, "Usage: /pause TASK_ID")
            return
        candidate, problem = match_task_id(
            [item.task_id for item in self.store.list_owner(owner, 100)], parts[1]
        )
        if candidate is None:
            await self._reply(message, problem)
        elif await self.manager.pause(candidate, owner):
            await self._reply(
                message, "**Task paused.** Use `/resume TASK_ID` to continue it later."
            )
        else:
            await self._reply(message, "Task not found or cannot be paused.")

    async def _resume(self, message: GatewayMessage, owner: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self._reply(message, "Usage: /resume TASK_ID [--ack-side-effects]")
            return
        acknowledge = parts[1].endswith("--ack-side-effects")
        identifier = parts[1].replace("--ack-side-effects", "").strip()
        candidate, problem = match_task_id(
            [item.task_id for item in self.store.list_owner(owner, 100)], identifier
        )
        if candidate is None:
            await self._reply(message, problem)
            return
        try:
            resumed = await self.manager.resume(
                candidate, owner, acknowledge_side_effects=acknowledge
            )
        except RecoveryBlocked as exc:
            await self._reply(message, str(exc))
            return
        if resumed is None:
            await self._reply(
                message,
                "Task cannot be resumed: it may already be running or require acknowledgement "
                "of possible side effects (--ack-side-effects).",
            )
            return
        await self._reply(message, "**Task resumed.**")

    async def _cancel(self, message: GatewayMessage, owner: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self._reply(message, "Usage: /cancel TASK_ID")
            return
        candidate, problem = match_task_id(
            [item.task_id for item in self.store.list_owner(owner, 100)], parts[1]
        )
        if candidate is None:
            await self._reply(message, problem)
        elif await self.manager.cancel(candidate, owner):
            await self._reply(message, "**Cancellation requested.**")
        else:
            await self._reply(message, "Task not found or cannot be cancelled.")

    async def _approval_command(self, message: GatewayMessage, command: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self._reply(message, "Usage: /approve TOKEN or /deny TOKEN")
            return
        owner = scope_key(message.platform, message.user_id)
        if self.broker.is_locked_out(owner):
            await self._reply(message, "Too many invalid approval attempts. Try again later.")
            return
        resolved = await self.broker.resolve(
            parts[1].strip(),
            owner_id=owner,
            chat_id=message.conversation_id,
            allowed=command == "/approve",
        )
        if resolved:
            await self._reply(
                message,
                "**Approval accepted.**" if command == "/approve" else "**Action declined.**",
            )
        else:
            await self._reply(
                message, "Approval token is invalid, expired, or belongs to another chat."
            )

    async def _submit_task(self, message: GatewayMessage, text: str) -> None:
        platform = message.platform
        user_id = message.user_id
        conversation_id = message.conversation_id
        if self.store.is_chat_locked(user_id, conversation_id, platform):
            await self._reply(message, "This conversation is paused. Use `/unlock` to continue.")
            return
        if not self.store.claim_update(platform, f"{conversation_id}:{message.message_id}"):
            return
        owner = scope_key(platform, user_id)
        if self.submit_limiter is not None and not self.submit_limiter.allow(owner):
            wait = int(self.submit_limiter.retry_after(owner)) + 1
            await self._reply(message, f"Too many requests. Try again in {wait}s.")
            return
        session_id = self.store.current_session(user_id, conversation_id, platform)
        try:
            await self.manager.submit(
                owner,
                session_id,
                text,
                notify_chat_id=conversation_id,
            )
        except RuntimeError:
            await self._reply(
                message, "Eurus is at its background task capacity. Try again shortly."
            )
            return
