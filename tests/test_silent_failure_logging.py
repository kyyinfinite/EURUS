"""C6b regression tests: swallowed failures must be logged, not silent.

Before the fix, ``capability_tools._session_search`` and the Telegram live
message fallback swallowed exceptions with bare ``except Exception: pass``,
hiding provider/gateway failures from operators.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from eurus.tools.base import ToolContext


@pytest.mark.asyncio
async def test_session_search_logs_summarizer_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
):
    from eurus.agent import capability_tools

    class FakeStore:
        def search_session_messages(self, query, *, limit, project_scope):
            return [
                {
                    "session_id": "s1",
                    "sequence": 3,
                    "role": "user",
                    "content": "the printer is on fire",
                }
            ]

    class FakeMemory:
        store = FakeStore()

    monkeypatch.setattr(capability_tools, "_memory", lambda: FakeMemory())

    class FailingRouter:
        async def chat(self, messages: Any, **kwargs: Any) -> Any:
            raise RuntimeError("summarizer down")

    with caplog.at_level(logging.WARNING, logger="eurus.agent.capability_tools"):
        result = await capability_tools._session_search(
            {"query": "printer", "limit": 5}, ToolContext(workspace=Path(".")), FailingRouter()
        )

    assert "the printer is on fire" in result, "raw evidence must still be returned"
    assert any(record.levelno >= logging.WARNING for record in caplog.records), (
        "the swallowed summarizer failure must be logged"
    )


@pytest.mark.asyncio
async def test_telegram_fallback_failure_is_logged(monkeypatch: pytest.MonkeyPatch, caplog):
    import sys
    import types

    from eurus.gateway.telegram.adapter import TelegramLiveMessage

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

    class FlakyBot:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.edited: list[dict[str, object]] = []
            self.rich_failures = 0
            self.send_failures = 0

        async def send_rich_message_draft(self, **kwargs: object) -> bool:
            return True

        async def send_rich_message(self, **kwargs: object) -> object:
            self.rich_failures += 1
            raise RuntimeError("rich message rejected")

        async def send_message(self, **kwargs: object) -> object:
            text = str(kwargs.get("text", ""))
            if "answer" in text and self.send_failures == 0:
                self.send_failures += 1
                raise RuntimeError("send failed once")
            self.sent.append(kwargs)
            return type("Msg", (), {"message_id": 1})()

        async def edit_message_text(self, **kwargs: object) -> object:
            self.edited.append(kwargs)
            return type("Msg", (), {"message_id": 1})()

    bot = FlakyBot()
    stream = TelegramLiveMessage(bot, 123, interval=0)

    with caplog.at_level(logging.DEBUG, logger="eurus.gateway.telegram.adapter"):
        await stream.update("answer text", final=True)

    assert bot.sent or bot.edited, "legacy fallback must still deliver the message"
    assert bot.rich_failures == 1
    debug_records = [
        record
        for record in caplog.records
        if "send failed once" in record.getMessage()
        or record.getMessage().startswith("Telegram fallback")
    ]
    assert debug_records, "the swallowed fallback failure must be logged"
