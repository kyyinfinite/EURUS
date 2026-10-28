from __future__ import annotations

from dataclasses import dataclass

import pytest

from eurus.gateway.telegram.adapter import (
    TelegramLiveMessage,
    markdown_to_telegram_html,
    normalize_rich_markdown,
)


@dataclass
class SentMessage:
    message_id: int


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> SentMessage:
        self.sent.append(kwargs)
        return SentMessage(len(self.sent))

    async def edit_message_text(self, **kwargs: object) -> SentMessage:
        self.edited.append(kwargs)
        return SentMessage(int(kwargs["message_id"]))


@pytest.mark.asyncio
async def test_live_response_edits_one_message_and_finalizes_rich_text() -> None:
    bot = FakeBot()
    stream = TelegramLiveMessage(bot, 123, interval=0)

    await stream.update("Hello")
    await stream.update("Hello there, **world**")
    await stream.update("Hello there, **world**", final=True)

    assert len(bot.sent) == 1
    assert len(bot.edited) == 2
    assert bot.edited[-1]["text"] == "Hello there, <b>world</b>"
    assert bot.edited[-1]["message_id"] == 1


@pytest.mark.asyncio
async def test_live_response_splits_long_final_answer() -> None:
    bot = FakeBot()
    stream = TelegramLiveMessage(bot, 123, interval=0)
    answer = ("A" * 1800) + "\n" + ("B" * 1800) + "\n" + ("C" * 300)

    await stream.update(answer, final=True)

    assert len(bot.sent) == 2
    assert len(bot.sent[0]["text"]) <= 3500
    assert len(bot.sent[1]["text"]) <= 3500
    assert len(bot.edited) == 1
    assert len(stream.last_text) <= 3500


def test_partial_fenced_code_is_formatted_without_unclosed_html() -> None:
    rendered = markdown_to_telegram_html("Example:\n```python\nprint('<hi>')", partial=True)

    assert '<pre><code class="language-python">' in rendered
    assert "&lt;hi&gt;" in rendered
    assert rendered.count("<pre>") == rendered.count("</pre>") == 1


@pytest.mark.asyncio
async def test_task_manager_streams_agent_deltas_and_does_not_send_duplicate_answer(
    tmp_path,
) -> None:
    from eurus.agent.events import AgentEvent
    from eurus.gateway.manager import BackgroundTaskManager
    from eurus.gateway.tasks import TaskStore
    from eurus.runtime.event_bridge import AgentEventBridge

    bridge = AgentEventBridge()
    notifications: list[str] = []
    updates: list[tuple[str, bool]] = []

    class FakeLiveResponse:
        async def update(self, text: str, *, final: bool = False) -> None:
            updates.append((text, final))

    async def runner(task: object) -> str:
        await bridge.publish("session", AgentEvent.text_delta("Hello "), task_id=task.task_id)
        await bridge.publish("session", AgentEvent.text_delta("from Eurus"), task_id=task.task_id)
        return "Hello from Eurus"

    async def notify(chat_id: int, text: str) -> None:
        notifications.append(text)

    manager = BackgroundTaskManager(
        TaskStore(tmp_path / "tasks.sqlite3"),
        runner,
        notify,
        event_bridge=bridge,
        live_response_factory=lambda chat_id: FakeLiveResponse(),
    )
    task = await manager.submit("owner", "session", "hello", notify_chat_id=123)
    await manager.active[task.task_id]

    assert updates
    assert updates[-1] == ("Hello from Eurus", True)
    assert notifications == []
    await manager.shutdown()
    bridge.close()


def test_rich_markdown_normalizes_gfm_tables() -> None:
    markdown = """## Inventory

| Item | Count |
|:---|---:|
| Keyboard | 5 |
| Mouse | 3 |
"""
    normalized = normalize_rich_markdown(markdown)

    assert "| Item | Count |" in normalized
    assert "| :--- | ---: |" in normalized
    assert "| Keyboard | 5 |" in normalized


@pytest.mark.asyncio
async def test_rich_draft_thinking_then_persists_final_rich_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    class InputRichMessage:
        def __init__(self, **kwargs: object) -> None:
            self.data = kwargs

    class InputRichBlockThinking:
        def __init__(self, **kwargs: object) -> None:
            self.data = kwargs

    aiogram_types = types.ModuleType("aiogram.types")
    aiogram_types.InputRichMessage = InputRichMessage
    aiogram_types.InputRichBlockThinking = InputRichBlockThinking
    monkeypatch.setitem(sys.modules, "aiogram.types", aiogram_types)

    class RichBot:
        def __init__(self) -> None:
            self.drafts: list[dict[str, object]] = []
            self.rich: list[dict[str, object]] = []

        async def send_rich_message_draft(self, **kwargs: object) -> bool:
            self.drafts.append(kwargs)
            return True

        async def send_rich_message(self, **kwargs: object) -> object:
            self.rich.append(kwargs)
            return object()

        async def send_message(self, **kwargs: object) -> SentMessage:
            raise AssertionError("legacy sender should not be used")

    bot = RichBot()
    stream = TelegramLiveMessage(bot, 123, interval=0)
    await stream.start()
    await stream.update("## Inventory\n\n| Item | Count |\n| --- | ---: |\n| Mouse | 3 |")
    await stream.update(
        "## Inventory\n\n| Item | Count |\n| --- | ---: |\n| Mouse | 3 |", final=True
    )

    assert len(bot.drafts) >= 2
    assert isinstance(bot.drafts[0]["rich_message"], InputRichMessage)
    assert "blocks" in bot.drafts[0]["rich_message"].data
    assert len(bot.rich) == 1
    final_message = bot.rich[0]["rich_message"]
    assert isinstance(final_message, InputRichMessage)
    assert "markdown" in final_message.data
    assert "| Mouse | 3 |" in final_message.data["markdown"]
