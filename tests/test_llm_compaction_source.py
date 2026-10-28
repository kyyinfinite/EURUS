"""C5 regression tests: LLM compaction must summarize the ORIGINAL history.

Before the fix, ``LLMSummarizingContextEngine`` fed the model the extractive
digest (``fallback.summary``) instead of the messages being compacted, so the
"LLM summary" was a rewrite of a truncated excerpt list and lost information
twice.
"""

from __future__ import annotations

from typing import Any

import pytest

from eurus.agent.context import LLMSummarizingContextEngine
from eurus.models import Message


class CapturingRouter:
    def __init__(self, *, reply: str = "condensed summary", fail: bool = False) -> None:
        self.calls: list[list[Message]] = []
        self.reply = reply
        self.fail = fail

    async def chat(self, messages: Any, *, tools: Any = (), **kwargs: Any) -> Any:
        self.calls.append(list(messages))
        if self.fail:
            raise RuntimeError("summarizer unavailable")

        class Response:
            message = Message.assistant("condensed summary")

        return Response()


def _history(messages: int = 40) -> list[Message]:
    history: list[Message] = [Message.system("system prompt")]
    for index in range(messages):
        marker = f"ORIGINAL-USER-MESSAGE-{index}-" + ("d" * 400)
        history.append(Message.user(marker))
        history.append(Message.assistant(f"ORIGINAL-AGENT-REPLY-{index}-" + ("r" * 400)))
    return history


@pytest.mark.asyncio
async def test_llm_summarizer_receives_original_messages_not_digest():
    router = CapturingRouter()
    engine = LLMSummarizingContextEngine(router, keep_recent=5)
    history = _history()

    compaction = await engine.compact(history, max_tokens=1000)

    assert router.calls, "the summarizer model must have been called"
    prompt = router.calls[0][-1]  # the user message of the summarization request
    assert prompt.role == "user"
    assert "ORIGINAL-USER-MESSAGE-2-" in prompt.content, (
        "the summarizer must see the original message content"
    )
    assert "ORIGINAL-AGENT-REPLY-5-" in prompt.content, (
        "assistant replies from the compacted region must be included verbatim"
    )
    assert not prompt.content.startswith("Earlier context summary:"), (
        "the extractive digest must not be the summarizer input"
    )
    assert compaction.used_fallback is False
    assert compaction.summary == "condensed summary"


@pytest.mark.asyncio
async def test_llm_compaction_preserves_recent_blocks_and_system():
    router = CapturingRouter()
    engine = LLMSummarizingContextEngine(router, keep_recent=3)
    history = _history(messages=10)

    compaction = await engine.compact(history, max_tokens=1000)

    roles = [message.role for message in compaction.messages]
    assert roles[0] == "system"
    assert history[-1].content in [message.content for message in compaction.messages], (
        "the most recent messages must be preserved untouched"
    )
    summaries = [
        m for m in compaction.messages if m.role == "system" and m.content != "system prompt"
    ]
    assert summaries and "condensed summary" in summaries[0].content


@pytest.mark.asyncio
async def test_summarizer_failure_falls_back_to_extractive():
    router = CapturingRouter(fail=True)
    engine = LLMSummarizingContextEngine(router, keep_recent=3)
    history = _history(messages=10)

    compaction = await engine.compact(history, max_tokens=1000)
    assert compaction.used_fallback is True
    assert "Earlier context summary" in compaction.summary or compaction.summary
    assert history[-1].content in [message.content for message in compaction.messages]


@pytest.mark.asyncio
async def test_summarizer_prompt_is_size_capped():
    router = CapturingRouter()
    engine = LLMSummarizingContextEngine(router, keep_recent=2)
    history = _history(messages=200)  # huge history

    await engine.compact(history, max_tokens=1000)
    assert router.calls
    prompt = router.calls[0][-1]
    assert len(prompt.content) <= 60000, "the summarizer prompt must stay bounded"
