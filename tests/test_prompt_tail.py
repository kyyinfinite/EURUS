"""Phase 1.5: volatile tier moves to a tail message so prefixes stay cache-stable."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eurus.agent.loop import Agent
from eurus.catalog import BUILTIN_PROVIDERS
from eurus.models import Message
from eurus.providers.anthropic import AnthropicProvider
from eurus.providers.base import ChatRequest
from eurus.providers.gemini import GeminiProvider
from eurus.providers.openai_compat import coalesce_system
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import Tool, ToolContext, ToolRegistry


def make_agent(router, **kwargs) -> Agent:
    registry = kwargs.pop("registry", ToolRegistry([]))
    return Agent(
        router,
        registry,
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
        volatile_prompt="Current date: 2026-01-01",
        **kwargs,
    )


def _noop_registry():
    async def handler(arguments, context):
        return "ok"

    return ToolRegistry([Tool("noop", "n", {"type": "object", "properties": {}}, handler)])


def test_volatile_tail_is_last_message_and_user_role():
    router, transport = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router)
    asyncio.run(_consume(agent))
    payload = transport.calls[0].payload
    messages = payload["messages"]
    assert messages[-1]["role"] == "user"
    assert "eurus-tier:volatile" in messages[-1]["content"]
    system_roles = [m for m in messages if m["role"] == "system"]
    assert all("eurus-tier:volatile" not in m["content"] for m in system_roles)


async def _consume(agent):
    async for _ in agent.run("go"):
        pass


def test_prefix_is_byte_identical_across_steps():
    router, transport = scripted_router(
        streams=[
            openai_stream("", calls=[("c1", "noop", {})]),
            openai_stream("two"),
        ]
    )
    agent = make_agent(router, registry=_noop_registry())
    asyncio.run(_consume(agent))
    first = transport.calls[0].payload["messages"]
    second = transport.calls[1].payload["messages"]
    # The volatile tail is the last message on both requests; the stable
    # prefix (system + context + history) grows only by appending, so the
    # first request's messages are a byte-identical prefix of the second's.
    tail = second[-1]
    assert "eurus-tier:volatile" in tail["content"]
    prefix = second[:-1]
    assert prefix[: len(first) - 1] == first[:-1]


def test_wall_clock_hidden_until_close_to_limit():
    from eurus.runtime.budgets import BudgetSpec
    from eurus.runtime.models import Run
    from eurus.runtime.run_state import render_run_state

    run = Run(owner_id="l", objective="o")
    agent = make_agent(scripted_router()[0])
    agent.deadline_at = datetime.now(UTC) + timedelta(seconds=300)
    far = render_run_state(run, BudgetSpec(), agent)
    assert "Wall-clock remaining" not in far

    agent.deadline_at = datetime.now(UTC) + timedelta(seconds=45)
    near = render_run_state(run, BudgetSpec(), agent)
    assert "Wall-clock remaining: <60s" in near

    agent.deadline_at = datetime.now(UTC) + timedelta(seconds=5)
    close = render_run_state(run, BudgetSpec(), agent)
    assert "Wall-clock remaining: <30s" in close


def test_nvidia_coalesce_keeps_volatile_tail_last():
    messages = [
        Message.system("stable"),
        Message.system("context"),
        Message.user("hello"),
        Message.user("<eurus-tier:volatile>\nIter 1\n</eurus-tier:volatile>"),
    ]
    coalesced = coalesce_system(messages)
    assert coalesced[0].role == "system"
    assert "eurus-tier:volatile" not in coalesced[0].content
    assert coalesced[-1].role == "user"
    assert "eurus-tier:volatile" in coalesced[-1].content


def test_anthropic_keeps_tail_in_wire_not_system():
    provider = AnthropicProvider(BUILTIN_PROVIDERS["anthropic"], "k", _FakeTransport())
    request = ChatRequest(
        model="m",
        messages=(
            Message.system("stable"),
            Message.user("hello"),
            Message.user("<eurus-tier:volatile>\nIter 1\n</eurus-tier:volatile>"),
        ),
        max_tokens=64,
    )
    payload = provider._payload(request, stream=False)
    assert "Iter 1" not in (payload.get("system") or "")
    wire = payload["messages"]
    assert wire[-1]["role"] == "user"
    assert "Iter 1" in wire[-1]["content"]


def test_gemini_keeps_tail_in_contents_not_system():
    provider = GeminiProvider(BUILTIN_PROVIDERS["gemini"], "k", _FakeTransport())
    request = ChatRequest(
        model="m",
        messages=(
            Message.system("stable"),
            Message.user("hello"),
            Message.user("<eurus-tier:volatile>\nIter 1\n</eurus-tier:volatile>"),
        ),
    )
    payload = provider._payload(request)
    system = payload.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
    assert "Iter 1" not in system
    contents = payload["contents"]
    assert contents[-1]["role"] == "user"
    assert "Iter 1" in contents[-1]["parts"][0]["text"]


class _FakeTransport:
    async def post_json(self, url, headers, payload):
        raise AssertionError("network disabled in tests")

    async def stream_sse(self, url, headers, payload):
        raise AssertionError("network disabled in tests")
        yield ""

    async def aclose(self) -> None:
        return None
