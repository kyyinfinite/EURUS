from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from eurus.agent.events import AgentEvent
from eurus.models import Message
from eurus.runtime.application import AgentRuntime
from eurus.sessions import SessionStore


class ScopedAgent:
    def __init__(self, workspace: Path | None = None, gate: asyncio.Event | None = None) -> None:
        self.history = [Message.system("system")]
        self.gate = gate
        if workspace is not None:
            self.context = SimpleNamespace(workspace=workspace)

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta("first")
        if self.gate is not None:
            await self.gate.wait()
        self.history.append(Message.assistant(f"reply:{text}"))
        yield AgentEvent.text_delta("second")


def make_runtime(tmp_path: Path) -> AgentRuntime:
    def refuse(session_id: str) -> ScopedAgent:
        raise AssertionError("cached agents must not be built for supplied agents")

    return AgentRuntime(refuse, SessionStore(tmp_path / "sessions.sqlite3"))


async def consume(runtime: AgentRuntime, session_id: str, agent: ScopedAgent, text: str) -> None:
    async for _ in runtime.run_agent(session_id, agent, text):
        pass


@pytest.mark.asyncio
async def test_supplied_agent_events_reach_the_bridge_and_agent_is_not_cached(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path)
    _, queue = runtime.event_bridge.subscribe("tui-session")
    agent = ScopedAgent()

    events = [event async for event in runtime.run_agent("tui-session", agent, "hello")]

    assert [event.text for event in events] == ["first", "second"]
    assert [queue.get_nowait().event.text, queue.get_nowait().event.text] == ["first", "second"]
    assert runtime.agents == {}
    assert runtime.locks == {}
    await runtime.close()


@pytest.mark.asyncio
async def test_persist_flag_controls_whether_history_is_saved(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)

    async for _ in runtime.run_agent("quiet", ScopedAgent(), "hello", persist=False):
        pass
    assert runtime.session_store.load_messages("quiet") == []

    await consume(runtime, "saved", ScopedAgent(), "hello")
    saved = runtime.session_store.load_messages("saved")
    assert [message.content for message in saved] == ["hello", "reply:hello"]
    await runtime.close()


@pytest.mark.asyncio
async def test_supplied_agent_with_workspace_is_saved_under_that_project(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    await consume(runtime, "scoped", ScopedAgent(workspace=tmp_path), "hello")
    assert [m.content for m in runtime.session_store.load_messages("scoped")] == [
        "hello",
        "reply:hello",
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_supplied_and_cached_runs_share_the_session_lock(tmp_path: Path) -> None:
    gate = asyncio.Event()
    runtime = make_runtime(tmp_path)
    order: list[str] = []

    async def first() -> None:
        async for event in runtime.run_agent("shared", ScopedAgent(gate=gate), "one"):
            order.append(f"one:{event.text}")

    async def second() -> None:
        async for event in runtime.run_agent("shared", ScopedAgent(), "two"):
            order.append(f"two:{event.text}")

    first_task = asyncio.create_task(first())
    await asyncio.sleep(0.01)
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert order == ["one:first"]
    gate.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["one:first", "one:second", "two:first", "two:second"]
    await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_supplied_run_releases_accounting_and_skips_save_when_not_persisting(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    runtime = make_runtime(tmp_path)

    async def run() -> None:
        async for _ in runtime.run_agent("c", ScopedAgent(gate=gate), "hello", persist=False):
            pass

    worker = asyncio.create_task(run())
    await asyncio.sleep(0.01)
    assert runtime.active_runs == 1
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert runtime.active_runs == 0
    assert runtime._session_users == {}
    assert runtime.session_store.load_messages("c") == []
    await runtime.close()


@pytest.mark.asyncio
async def test_close_waits_for_supplied_runs(tmp_path: Path) -> None:
    gate = asyncio.Event()
    runtime = make_runtime(tmp_path)
    worker = asyncio.create_task(consume(runtime, "w", ScopedAgent(gate=gate), "hello"))
    await asyncio.sleep(0.01)
    closer = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.01)
    assert not closer.done()
    gate.set()
    await asyncio.gather(worker, closer)
    assert runtime.closed


@pytest.mark.asyncio
async def test_run_agent_validates_input_and_closed_state(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    with pytest.raises(ValueError):
        await consume(runtime, " ", ScopedAgent(), "hello")
    with pytest.raises(ValueError):
        await consume(runtime, "s", ScopedAgent(), " ")
    await runtime.close()
    with pytest.raises(RuntimeError):
        await consume(runtime, "s", ScopedAgent(), "hello")
