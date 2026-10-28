from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from eurus.gateway.approval import ApprovalBroker
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.limits import RateLimiter
from eurus.gateway.manager import BackgroundTaskManager, TaskRunner
from eurus.gateway.router import ACCESS_DENIED, GatewayRouter, match_task_id
from eurus.gateway.security import GatewayAccessPolicy, permits_message
from eurus.gateway.tasks import InvalidTaskTransition, TaskStore, scope_key
from eurus.runtime.event_bridge import AgentEventBridge
from eurus.security.redaction import scrub_secrets

log = logging.getLogger(__name__)
MAX_TELEGRAM_TEXT = 3500

__all__ = [
    "BackgroundTaskManager",
    "TelegramAdapter",
    "TelegramDeliveryError",
    "TelegramMessage",
    "TelegramNotifier",
    "match_task_id",
    "run_telegram_polling",
    "split_telegram_text",
    "token_from_environment",
]


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    user_id: int
    chat_id: int
    message_id: int
    update_id: int
    text: str
    is_group: bool = False


MessageSender = Callable[[int, str], Awaitable[object]]


class TelegramDeliveryError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        sender: MessageSender,
        *,
        max_attempts: int = 3,
        global_interval: float = 1 / 30,
        chat_interval: float = 1.0,
        group_interval: float = 3.0,
        max_retry_after: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if global_interval < 0 or chat_interval < 0 or group_interval < 0:
            raise ValueError("rate-limit intervals cannot be negative")
        if max_retry_after < 0:
            raise ValueError("max_retry_after cannot be negative")
        self.sender = sender
        self.max_attempts = max_attempts
        self.global_interval = global_interval
        self.chat_interval = chat_interval
        self.group_interval = group_interval
        self.max_retry_after = max_retry_after
        self.sleep = sleep
        self.clock = clock
        self._schedule_lock = asyncio.Lock()
        self._next_global_send = 0.0
        self._next_chat_send: dict[int, float] = {}

    async def __call__(self, chat_id: int, text: str) -> None:
        clean_text = scrub_secrets(text)
        if clean_text.strip() == "Eurus is working on your request." or clean_text.startswith(
            "Working with tool:"
        ):
            return
        for chunk in split_telegram_text(clean_text):
            await self._send_chunk(chat_id, chunk)

    async def _reserve_send_slot(self, chat_id: int) -> None:
        async with self._schedule_lock:
            now = self.clock()
            next_chat = self._next_chat_send.get(chat_id, now)
            chat_interval = self.group_interval if chat_id < 0 else self.chat_interval
            scheduled = max(now, self._next_global_send, next_chat)
            self._next_global_send = scheduled + self.global_interval
            self._next_chat_send[chat_id] = scheduled + chat_interval
            if len(self._next_chat_send) > 2048:
                self._next_chat_send = {
                    key: value for key, value in self._next_chat_send.items() if value > now
                }
                self._next_chat_send[chat_id] = scheduled + chat_interval
        delay = scheduled - now
        if delay > 0:
            await self.sleep(delay)

    async def _send_chunk(self, chat_id: int, text: str) -> None:
        for attempt in range(self.max_attempts):
            await self._reserve_send_slot(chat_id)
            try:
                await self.sender(chat_id, text)
                return
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                error_name = type(exc).__name__
                retryable = retry_after is not None or error_name in {
                    "TelegramNetworkError",
                    "TelegramServerError",
                    "TimeoutError",
                    "ConnectionError",
                    "OSError",
                }
                if not retryable:
                    raise TelegramDeliveryError("Telegram message delivery failed") from exc
                if attempt + 1 >= self.max_attempts:
                    raise TelegramDeliveryError(
                        "Telegram message delivery retries exhausted"
                    ) from exc
                if retry_after is not None:
                    try:
                        delay = max(0.0, float(retry_after))
                    except (TypeError, ValueError) as conversion_error:
                        raise TelegramDeliveryError(
                            "Telegram returned an invalid retry delay"
                        ) from conversion_error
                    if delay > self.max_retry_after:
                        raise TelegramDeliveryError(
                            "Telegram retry delay exceeds the configured limit"
                        ) from exc
                else:
                    delay = min(0.5 * (2**attempt), self.max_retry_after)
                await self.sleep(delay)


class TelegramAdapter:
    def __init__(
        self,
        notifier: Callable[[int, str], Awaitable[None]],
        *,
        poll: Callable[[], Awaitable[object]],
        close: Callable[[], Awaitable[object]],
    ) -> None:
        self._notifier = notifier
        self._poll = poll
        self._close = close

    @property
    def platform(self) -> str:
        return "telegram"

    async def send_text(self, conversation_id: str, text: str) -> None:
        try:
            chat_id = int(conversation_id)
        except ValueError as exc:
            raise TelegramDeliveryError("invalid Telegram chat id") from exc
        await self._notifier(chat_id, text)

    async def start(self) -> None:
        await self._poll()

    async def stop(self) -> None:
        await self._close()


def split_telegram_text(text: str, limit: int = MAX_TELEGRAM_TEXT) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def normalize_rich_markdown(text: str, *, partial: bool = False) -> str:
    normalized = scrub_secrets(text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\x00", "")
    normalized = re.sub(r"[ \t]+$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    lines = normalized.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            "|" in line
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
            output.append("| " + " | ".join(headers) + " |")
            separators = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
            normalized_separators = []
            for cell in separators[: len(headers)]:
                left = cell.startswith(":")
                right = cell.endswith(":")
                normalized_separators.append((":" if left else "") + "---" + (":" if right else ""))
            normalized_separators.extend(["---"] * (len(headers) - len(normalized_separators)))
            output.append("| " + " | ".join(normalized_separators) + " |")
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                cells = (cells + [""] * len(headers))[: len(headers)]
                output.append("| " + " | ".join(cells) + " |")
                index += 1
            output.append("")
            continue
        output.append(line)
        index += 1
    normalized = "\n".join(output).strip()
    if partial and normalized.count("```") % 2:
        normalized += "\n```"
    return normalized


def _markdown_tables_to_readable_text(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if (
            "|" in lines[index]
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            headers = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
            index += 2
            row_number = 0
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                cells = (cells + [""] * len(headers))[: len(headers)]
                output.append(
                    "\n".join(
                        f"{header}: {value}"
                        for header, value in zip(headers, cells, strict=False)
                        if header or value
                    )
                )
                output.append("")
                row_number += 1
                index += 1
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output).strip()


def markdown_to_telegram_html(text: str, *, partial: bool = False) -> str:
    text = _markdown_tables_to_readable_text(text)
    if partial and text.count("```") % 2:
        text = text + "\n```"
    code_blocks: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        language = match.group(1).strip().split(maxsplit=1)[0] if match.group(1).strip() else ""
        code = html.escape(match.group(2).strip("\n"))
        if language and re.fullmatch(r"[a-zA-Z0-9_+#.-]{1,32}", language):
            rendered = f'<pre><code class="language-{language}">{code}</code></pre>'
        else:
            rendered = f"<pre>{code}</pre>"
        code_blocks.append(rendered)
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(r"```([^\n`]*)\n([\s\S]*?)```", stash_code, text)
    inline_codes: list[str] = []

    def stash_inline(match: re.Match[str]) -> str:
        inline_codes.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash_inline, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+(.+)$", r"• \1", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+(.+)$", r"• \1", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda match: f'<a href="{html.escape(match.group(2), quote=True)}">{match.group(1)}</a>',
        text,
    )
    for index, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{index}\x00", code)
    for index, code in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{index}\x00", code)
    return text


async def run_telegram_polling(
    *,
    token: str,
    policy: GatewayAccessPolicy,
    store: TaskStore,
    runner: TaskRunner,
    max_concurrent: int = 2,
    approval_broker: ApprovalBroker | None = None,
    event_bridge: AgentEventBridge | None = None,
    reminder_store: Any | None = None,
) -> None:
    try:
        from aiogram import Bot, Dispatcher, F
        from aiogram.filters import Command
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
    except ImportError as exc:
        raise RuntimeError(
            "Telegram gateway requires the optional dependency: pip install 'eurus-agent[telegram]'"
        ) from exc

    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()

    async def send_formatted_message(chat_id: int, text: str) -> object:
        return await bot.send_message(chat_id=chat_id, text=markdown_to_telegram_html(text))

    notifier = TelegramNotifier(send_formatted_message)
    adapter = TelegramAdapter(
        notifier,
        poll=lambda: dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        ),
        close=bot.session.close,
    )
    manager = BackgroundTaskManager(
        store,
        runner,
        adapter.send_text,
        live_response_factory=lambda chat_id: TelegramLiveMessage(bot, int(chat_id)),
        max_concurrent=max_concurrent,
        max_pending_per_owner=max(1, max_concurrent * 4),
        event_bridge=event_bridge,
    )
    broker = approval_broker or ApprovalBroker(persistence_path=store.path)
    if getattr(broker, "persistence_path", None) is None:
        broker.persistence_path = str(store.path)
        broker._initialize_persistence()

    async def approval_notifier(conversation_id: str, text: str) -> None:
        match = re.search(
            r"Approve: /approve ([A-Za-z0-9_-]{8,64})\s*\nDeny: /deny [A-Za-z0-9_-]{8,64}",
            text,
        )
        if match is None:
            await adapter.send_text(conversation_id, text)
            return
        token = match.group(1)
        visible = re.sub(
            r"\nApprove: /approve [A-Za-z0-9_-]{8,64}\nDeny: /deny [A-Za-z0-9_-]{8,64}",
            "",
            text,
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Approve", callback_data=f"approval:yes:{token}"),
                    InlineKeyboardButton(text="Reject", callback_data=f"approval:no:{token}"),
                ]
            ]
        )
        await bot.send_message(
            chat_id=int(conversation_id),
            text=markdown_to_telegram_html(visible),
            reply_markup=keyboard,
        )

    broker.bind_notifier(approval_notifier)

    from eurus.reminders import ReminderDispatcher, ReminderStore

    reminder_dispatcher = ReminderDispatcher(
        reminder_store or ReminderStore(store.path), adapter.send_text
    )
    reminder_task = asyncio.create_task(reminder_dispatcher.run(), name="eurus-reminder-dispatcher")

    async def approval_state(task_id: str, waiting: bool) -> None:
        current = store.get(task_id)
        if current is None:
            return
        try:
            if waiting and current.status == "running":
                store.transition(task_id, "waiting_approval")
            elif not waiting and current.status == "waiting_approval":
                store.transition(task_id, "running")
        except InvalidTaskTransition:
            return

    broker.state_callback = approval_state
    manager.approval_broker = broker
    router = GatewayRouter(
        adapter=adapter,
        access=lambda message: permits_message(policy, message),
        store=store,
        manager=manager,
        broker=broker,
        submit_limiter=RateLimiter(max_events=12, window_seconds=60.0),
    )

    async def dispatch(message: Message) -> None:
        sender = message.from_user
        if sender is None or message.chat is None:
            log.warning("Telegram message without sender or chat ignored")
            await message.answer(ACCESS_DENIED)
            return
        await router.handle(
            GatewayMessage(
                platform=adapter.platform,
                user_id=str(sender.id),
                conversation_id=str(message.chat.id),
                message_id=str(message.message_id),
                text=message.text or "",
                is_group=message.chat.type in {"group", "supergroup"},
            )
        )

    @dispatcher.message(
        Command(
            "start",
            "help",
            "new",
            "status",
            "cancel",
            "lock",
            "unlock",
            "approve",
            "deny",
            "pause",
            "resume",
        )
    )
    async def command_handler(message: Message) -> None:
        await dispatch(message)

    @dispatcher.callback_query(F.data.startswith("approval:"))
    async def approval_callback(query: Any) -> None:
        callback_data = query.data or ""
        parts = callback_data.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"yes", "no"}:
            await query.answer("Invalid approval action.", show_alert=True)
            return
        if query.from_user is None or query.message is None:
            await query.answer("This approval cannot be resolved here.", show_alert=True)
            return
        allowed = parts[1] == "yes"
        resolved = await broker.resolve(
            parts[2],
            owner_id=scope_key("telegram", str(query.from_user.id)),
            chat_id=str(query.message.chat.id),
            allowed=allowed,
        )
        if not resolved:
            await query.answer("Expired or unauthorized approval.", show_alert=True)
            return
        await query.answer("Approved." if allowed else "Rejected.")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                text=("<b>Approval accepted.</b>" if allowed else "<b>Action rejected.</b>"),
                reply_markup=None,
            )

    @dispatcher.message(F.text)
    async def text_handler(message: Message) -> None:
        await dispatch(message)

    store.interrupt_orphaned()
    store.prune_updates()
    redispatched = await manager.resume_pending()
    if redispatched:
        log.info("Redispatched %d durable task(s) after restart", redispatched)
    try:
        await adapter.start()
    finally:
        await reminder_dispatcher.stop()
        reminder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminder_task
        await manager.shutdown()
        await broker.close()
        await adapter.stop()


class TelegramLiveMessage:
    def __init__(self, bot: Any, chat_id: int, *, interval: float = 0.9) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.interval = interval
        self.message_id: int | None = None
        self.last_text = ""
        self.last_update = 0.0
        self.draft_id = secrets.randbelow(2_000_000_000) + 1
        self.rich_drafts = chat_id > 0 and callable(getattr(bot, "send_rich_message_draft", None))
        self.rich_messages = callable(getattr(bot, "send_rich_message", None))
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self.rich_drafts:
            try:
                from aiogram.types import InputRichBlockThinking, InputRichMessage

                await self.bot.send_rich_message_draft(
                    chat_id=self.chat_id,
                    draft_id=self.draft_id,
                    rich_message=InputRichMessage(blocks=[InputRichBlockThinking(text="Thinking")]),
                )
                self.last_update = time.monotonic()
                return
            except Exception as exc:
                log.debug("Telegram rich draft unavailable error_type=%s", type(exc).__name__)
                self.rich_drafts = False
        message = await self.bot.send_message(chat_id=self.chat_id, text="Thinking…")
        self.message_id = getattr(message, "message_id", None)
        self.last_update = time.monotonic()

    async def heartbeat(self, text: str = "") -> None:
        if not self.rich_drafts or time.monotonic() - self.last_update < 8.0:
            return
        from aiogram.types import InputRichBlockThinking, InputRichMessage

        if text.strip():
            rich_message = InputRichMessage(markdown=normalize_rich_markdown(text, partial=True))
        else:
            rich_message = InputRichMessage(blocks=[InputRichBlockThinking(text="Thinking")])
        try:
            await self.bot.send_rich_message_draft(
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                rich_message=rich_message,
            )
            self.last_update = time.monotonic()
        except Exception as exc:
            log.debug("Telegram draft heartbeat failed error_type=%s", type(exc).__name__)
            self.rich_drafts = False

    async def update(self, text: str, *, final: bool = False) -> None:
        clean = normalize_rich_markdown(scrub_secrets(text), partial=not final)
        if not clean:
            if final:
                clean = "I couldn't generate a response. Please try again."
            else:
                return
        if not self.started:
            await self.start()
        now = time.monotonic()
        if not final and self.last_text:
            if clean == self.last_text or now - self.last_update < self.interval:
                return
            if len(clean) - len(self.last_text) < 24 and len(clean) < MAX_TELEGRAM_TEXT:
                return
        if self.rich_drafts:
            try:
                from aiogram.types import InputRichMessage

                if final:
                    if self.rich_messages:
                        await self.bot.send_rich_message(
                            chat_id=self.chat_id,
                            rich_message=InputRichMessage(markdown=normalize_rich_markdown(text)),
                        )
                    else:
                        await self._send_legacy_final(clean)
                else:
                    await self.bot.send_rich_message_draft(
                        chat_id=self.chat_id,
                        draft_id=self.draft_id,
                        rich_message=InputRichMessage(markdown=clean),
                    )
                self.last_text = clean
                self.last_update = time.monotonic()
                return
            except Exception as exc:
                log.debug("Telegram rich message unavailable error_type=%s", type(exc).__name__)
                self.rich_drafts = False
                if final and self.rich_messages:
                    try:
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=markdown_to_telegram_html(clean),
                        )
                        self.last_text = clean
                        return
                    except Exception as exc:
                        log.warning(
                            "Telegram fallback send failed; falling back to legacy update "
                            "error_type=%s",
                            type(exc).__name__,
                        )
        await self._update_legacy(clean, final=final)

    async def _send_legacy_final(self, text: str) -> None:
        chunks = split_telegram_text(text)
        for chunk in chunks:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=markdown_to_telegram_html(chunk),
            )

    async def _update_legacy(self, clean: str, *, final: bool) -> None:
        chunks = split_telegram_text(clean)
        visible = chunks[0]
        rendered = markdown_to_telegram_html(visible, partial=not final)
        if self.message_id is None:
            message = await self.bot.send_message(
                chat_id=self.chat_id, text=rendered or "Thinking…"
            )
            self.message_id = message.message_id
        elif visible != self.last_text or final:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=rendered or "Thinking…",
                )
            except Exception as exc:
                if (
                    type(exc).__name__ != "TelegramBadRequest"
                    or "message is not modified" not in str(exc).lower()
                ):
                    raise
        self.last_text = visible
        self.last_update = time.monotonic()
        if final and len(chunks) > 1:
            for chunk in chunks[1:]:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=markdown_to_telegram_html(chunk),
                )


def token_from_environment() -> str:
    token = os.environ.get("EURUS_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("EURUS_TELEGRAM_BOT_TOKEN is required")
    return token
