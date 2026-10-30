"""Phase 1.1: per-session reuse of router, agent and engine stores."""

from __future__ import annotations

import asyncio

from rich.console import Console

from eurus.agent.events import AgentEvent
from eurus.config import load_config
from eurus.models import Message, Usage
from eurus.tui import InteractiveSession, TerminalRenderer


class FakeRouter:
    def __init__(self) -> None:
        self.closed = False
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.closed = True


class FakeAgent:
    def __init__(self) -> None:
        self.history = [Message.system("system")]
        self.usage = Usage()

    async def run(self, text):
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta("working")
        self.history.append(Message.assistant("done"))
        yield AgentEvent.finished("completed", Usage(10, 5))


def make_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={"agent": {"model": "groq/llama"}},
    )
    console = Console(record=True, width=60, force_terminal=False)
    session = InteractiveSession(config, TerminalRenderer(console))
    routers: list[FakeRouter] = []
    agents: list[FakeAgent] = []

    def build_router(config):
        router = FakeRouter()
        routers.append(router)
        return router

    def build_agent(config, router, **kwargs):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    monkeypatch.setattr("eurus.tui.session.build_router", build_router)
    monkeypatch.setattr("eurus.tui.session.build_agent", build_agent)
    return session, routers, agents


def test_router_and_agent_reused_across_turns(tmp_path, monkeypatch):
    session, routers, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        await session.submit("second")
        await session.close()

    asyncio.run(scenario())
    assert len(routers) == 1 and len(agents) == 1
    assert routers[0].aclose_calls == 1, "router closes once, at session close"
    assert [m.content for m in session.history] == ["first", "done", "second", "done"]


def test_agent_history_not_duplicated_on_reuse(tmp_path, monkeypatch):
    session, _, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        await session.submit("second")
        await session.close()

    asyncio.run(scenario())
    agent = agents[0]
    contents = [message.content for message in agent.history]
    assert contents.count("first") == 1
    assert contents.count("second") == 1


def test_usage_reset_per_turn_on_reuse(tmp_path, monkeypatch):
    session, _, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        agent = agents[0]
        assert agent.usage.input_tokens == 0, "usage must be reset before the next turn"
        await session.submit("second")
        await session.close()

    asyncio.run(scenario())
    # session usage accumulates both turns exactly once each
    assert session.usage.input_tokens == 20 and session.usage.output_tokens == 10


def test_model_switch_rebuilds_router_and_agent(tmp_path, monkeypatch):
    session, routers, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        await session._model("openai/gpt-test")
        await session.submit("second")
        await session.close()

    asyncio.run(scenario())
    assert len(routers) == 2 and len(agents) == 2
    assert routers[0].aclose_calls == 1, "stale router is closed after invalidation"
    assert routers[1].aclose_calls == 1, "current router closes at session close"


def test_clear_keeps_router_but_rebuilds_agent(tmp_path, monkeypatch):
    session, routers, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        await session.submit("/clear")
        await session.submit("second")
        await session.close()

    asyncio.run(scenario())
    assert len(routers) == 1
    assert len(agents) == 2
    assert [message.content for message in session.history] == ["second", "done"]
    assert all(message.content != "first" for message in agents[1].history)


def test_engine_stores_share_one_database(tmp_path, monkeypatch):
    session, _, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        engines = [session._engine_for_agent(agents[0]) for _ in range(2)]
        await session.close()
        return engines

    engines = asyncio.run(scenario())
    assert engines[0].run_store is engines[1].run_store
    assert engines[0].run_store.database is engines[1].event_store.database
    assert session._engine_database is None, "close() must release the shared database"


def test_close_drains_background_tasks_and_closes_resources(tmp_path, monkeypatch):
    session, routers, agents = make_session(tmp_path, monkeypatch)

    async def scenario():
        await session.submit("first")
        await session.close()

    asyncio.run(scenario())
    assert routers[0].closed
    assert session._cached_agent is None
    assert session._cached_router is None
