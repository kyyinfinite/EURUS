from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eurus.agent.events import AgentEvent
from eurus.gateway.tasks import DurableTask, TaskStore
from eurus.gateway.telegram.adapter import BackgroundTaskManager
from eurus.models import Message, ToolCall
from eurus.runtime import event_bridge
from eurus.runtime.application import AgentRuntime
from eurus.runtime.event_bridge import AgentEventBridge, SessionEvent
from eurus.sessions import SessionStore


class StubAgent:
    def __init__(self, session_id: str, workspace: Path, delay: float = 0.0) -> None:
        self.session_id = session_id
        self.context_store = None
        self.context = SimpleNamespace(workspace=workspace)
        self.history = [Message.system("system")]
        self.delay = delay

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta(f"{self.session_id}:{text}:start")
        await asyncio.sleep(self.delay)
        self.history.append(Message.assistant(f"reply:{text}"))
        yield AgentEvent.text_delta(f"{self.session_id}:{text}:end")


def make_runtime(tmp_path: Path, *, delay: float = 0.0, **options: Any) -> AgentRuntime:
    return AgentRuntime(
        lambda session_id: StubAgent(session_id, tmp_path, delay),
        SessionStore(tmp_path / "sessions.sqlite3"),
        **options,
    )


async def drain(runtime: AgentRuntime, session_id: str, text: str, task_id: str | None) -> None:
    async for _ in runtime.run(session_id, text, task_id=task_id):
        pass


def collect(queue: asyncio.Queue[SessionEvent]) -> list[SessionEvent]:
    items: list[SessionEvent] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_runtime_event_name_is_an_alias_of_session_event() -> None:
    assert event_bridge.RuntimeEvent is SessionEvent


@pytest.mark.asyncio
async def test_task_scoped_subscribers_only_receive_their_own_task_events() -> None:
    bridge = AgentEventBridge()
    _, first = bridge.subscribe("session-a", task_id="task-1")
    _, second = bridge.subscribe("session-a", task_id="task-2")
    _, everything = bridge.subscribe("session-a")

    await bridge.publish("session-a", AgentEvent.text_delta("one"), task_id="task-1")
    await bridge.publish("session-a", AgentEvent.text_delta("two"), task_id="task-2")
    await bridge.publish("session-a", AgentEvent.text_delta("untagged"))

    assert [item.event.text for item in collect(first)] == ["one", "untagged"]
    assert [item.event.text for item in collect(second)] == ["two", "untagged"]
    assert [item.event.text for item in collect(everything)] == ["one", "two", "untagged"]


@pytest.mark.asyncio
async def test_bridge_never_delivers_events_across_sessions() -> None:
    bridge = AgentEventBridge()
    _, queue_a = bridge.subscribe("session-a")
    _, queue_b = bridge.subscribe("session-b")

    await bridge.publish("session-a", AgentEvent.text_delta("secret-a"))

    assert [item.session_id for item in collect(queue_a)] == ["session-a"]
    assert queue_b.empty()


@pytest.mark.asyncio
async def test_bridge_counts_dropped_events_per_subscriber() -> None:
    bridge = AgentEventBridge(default_capacity=1)
    slow_id, slow = bridge.subscribe("session-a")
    fast_id, fast = bridge.subscribe("session-a", capacity=8)

    for index in range(3):
        await bridge.publish("session-a", AgentEvent.text_delta(str(index)))

    assert bridge.dropped_for("session-a", slow_id) == 2
    assert bridge.dropped_for("session-a", fast_id) == 0
    assert bridge.dropped_events == 2
    assert [item.event.text for item in collect(slow)] == ["2"]
    assert len(collect(fast)) == 3
    assert bridge.dropped_for("session-a", "missing") == 0


def test_bridge_enforces_subscriber_limit_and_validates_arguments() -> None:
    bridge = AgentEventBridge(max_subscribers=1)
    subscriber_id, _ = bridge.subscribe("session-a")
    with pytest.raises(RuntimeError):
        bridge.subscribe("session-b")
    with pytest.raises(ValueError):
        bridge.subscribe("session-a", task_id=" ")
    assert bridge.unsubscribe("session-a", subscriber_id)
    assert bridge.subscriber_count == 0
    bridge.subscribe("session-b")
    with pytest.raises(ValueError):
        AgentEventBridge(max_subscribers=0)


@pytest.mark.asyncio
async def test_runtime_tags_events_with_task_id_and_keeps_sessions_isolated(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, delay=0.01)
    _, queue_a = runtime.event_bridge.subscribe("session-a")
    _, queue_b = runtime.event_bridge.subscribe("session-b")

    await asyncio.gather(
        drain(runtime, "session-a", "alpha", "task-a"),
        drain(runtime, "session-b", "beta", "task-b"),
    )

    events_a = collect(queue_a)
    events_b = collect(queue_b)
    assert {item.task_id for item in events_a} == {"task-a"}
    assert {item.task_id for item in events_b} == {"task-b"}
    assert all("alpha" in item.event.text for item in events_a)
    assert all("beta" in item.event.text for item in events_b)
    saved_a = [message.content for message in runtime.session_store.load_messages("session-a")]
    saved_b = [message.content for message in runtime.session_store.load_messages("session-b")]
    assert saved_a == ["alpha", "reply:alpha"]
    assert saved_b == ["beta", "reply:beta"]
    await runtime.close()


@pytest.mark.asyncio
async def test_same_session_runs_are_serialized_and_events_stay_with_their_task(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, delay=0.01)
    _, first = runtime.event_bridge.subscribe("session-a", task_id="task-1")
    _, second = runtime.event_bridge.subscribe("session-a", task_id="task-2")

    await asyncio.gather(
        drain(runtime, "session-a", "one", "task-1"),
        drain(runtime, "session-a", "two", "task-2"),
    )

    assert all("one" in item.event.text for item in collect(first))
    assert all("two" in item.event.text for item in collect(second))
    saved = [message.content for message in runtime.session_store.load_messages("session-a")]
    assert saved in (
        ["one", "reply:one", "two", "reply:two"],
        ["two", "reply:two", "one", "reply:one"],
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_idle_session_locks_and_counters_are_released(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    await asyncio.gather(*(drain(runtime, f"session-{n}", "hi", None) for n in range(5)))
    assert runtime.locks == {}
    assert runtime._session_users == {}
    assert runtime.active_runs == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_agent_cache_is_bounded_and_evicted_sessions_reload_from_store(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, max_sessions=2)
    for index in range(5):
        await drain(runtime, f"session-{index}", f"turn-{index}", None)
    assert len(runtime.agents) <= 2
    assert "session-0" not in runtime.agents

    await drain(runtime, "session-0", "again", None)
    contents = [message.content for message in runtime.agents["session-0"].history]
    assert contents == ["system", "turn-0", "reply:turn-0", "again", "reply:again"]
    await runtime.close()


@pytest.mark.asyncio
async def test_running_sessions_are_never_evicted(tmp_path: Path) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()

    class GatedAgent(StubAgent):
        async def run(self, text: str) -> AsyncIterator[AgentEvent]:
            if self.session_id == "busy":
                entered.set()
                await gate.wait()
            async for event in super().run(text):
                yield event

    runtime = AgentRuntime(
        lambda session_id: GatedAgent(session_id, tmp_path),
        SessionStore(tmp_path / "sessions.sqlite3"),
        max_sessions=1,
    )
    long_run = asyncio.create_task(drain(runtime, "busy", "long", None))
    await entered.wait()
    await drain(runtime, "other", "quick", None)
    assert "busy" in runtime.agents
    gate.set()
    await long_run
    assert len(runtime.agents) <= 1
    await runtime.close()


@pytest.mark.asyncio
async def test_max_concurrent_runs_applies_backpressure(tmp_path: Path) -> None:
    active = 0
    peak = 0

    class CountingAgent(StubAgent):
        async def run(self, text: str) -> AsyncIterator[AgentEvent]:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                async for event in super().run(text):
                    yield event
            finally:
                active -= 1

    runtime = AgentRuntime(
        lambda session_id: CountingAgent(session_id, tmp_path, 0.02),
        SessionStore(tmp_path / "sessions.sqlite3"),
        max_concurrent_runs=2,
    )
    await asyncio.gather(*(drain(runtime, f"session-{n}", "go", None) for n in range(6)))
    assert peak == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_close_cancels_stuck_runs_after_timeout_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    closed: list[bool] = []

    class StuckAgent(StubAgent):
        async def run(self, text: str) -> AsyncIterator[AgentEvent]:
            started.set()
            await asyncio.Event().wait()
            yield AgentEvent.text_delta("unreachable")

    async def mark_closed() -> None:
        closed.append(True)

    runtime = AgentRuntime(
        lambda session_id: StuckAgent(session_id, tmp_path),
        SessionStore(tmp_path / "sessions.sqlite3"),
        close_callback=mark_closed,
        close_timeout=0.05,
    )
    worker = asyncio.create_task(drain(runtime, "session-a", "stuck", "task-1"))
    await started.wait()

    await asyncio.wait_for(runtime.close(), timeout=2.0)

    assert closed == [True]
    assert runtime.closed and runtime.event_bridge.closed
    assert runtime.active_runs == 0
    assert runtime.agents == {} and runtime.locks == {}
    with pytest.raises(asyncio.CancelledError):
        await worker


@pytest.mark.asyncio
async def test_closed_runtime_rejects_new_runs(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    await runtime.close()
    with pytest.raises(RuntimeError):
        await drain(runtime, "session-a", "late", None)


def test_runtime_rejects_invalid_limits(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")

    def factory(session_id: str) -> StubAgent:
        return StubAgent(session_id, tmp_path)

    for options in ({"max_sessions": 0}, {"max_concurrent_runs": 0}, {"close_timeout": 0}):
        with pytest.raises(ValueError):
            AgentRuntime(factory, store, **options)


@pytest.mark.asyncio
async def test_background_tasks_on_one_session_only_forward_their_own_progress(
    tmp_path: Path,
) -> None:
    bridge = AgentEventBridge()
    notifications: dict[int, list[str]] = {100: [], 200: []}
    ready = asyncio.Event()
    started = 0

    async def runner(task: DurableTask) -> str:
        nonlocal started
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()
        await bridge.publish(
            "shared-session",
            AgentEvent.tool_started(ToolCall("call", task.objective)),
            task_id=task.task_id,
        )
        return f"done:{task.objective}"

    async def notify(chat_id: int, text: str) -> None:
        notifications[chat_id].append(text)

    manager = BackgroundTaskManager(
        TaskStore(tmp_path / "tasks.sqlite3"),
        runner,
        notify,
        max_concurrent=2,
        event_bridge=bridge,
    )
    first = await manager.submit("1", "shared-session", "alpha", notify_chat_id=100)
    second = await manager.submit("1", "shared-session", "beta", notify_chat_id=200)
    await asyncio.gather(manager.active[first.task_id], manager.active[second.task_id])

    assert "Working with tool: alpha" in notifications[100]
    assert "Working with tool: beta" not in notifications[100]
    assert "Working with tool: beta" in notifications[200]
    assert "Working with tool: alpha" not in notifications[200]
    assert bridge.subscriber_count == 0
    await manager.shutdown()
    bridge.close()
