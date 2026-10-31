from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable

from eurus.agent.events import AgentEvent
from eurus.agent.loop import Agent
from eurus.observability import classify_error, elapsed_ms
from eurus.runtime.engine import AgentEngine
from eurus.runtime.event_bridge import AgentEventBridge
from eurus.runtime.models import Run
from eurus.sessions import SessionStore

AgentFactory = Callable[[str], Agent]
CloseCallback = Callable[[], Awaitable[None]]
EngineFactory = Callable[[Agent], AgentEngine]
RunFactory = Callable[[str], Run]

log = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        agent_factory: AgentFactory,
        session_store: SessionStore,
        *,
        close_callback: CloseCallback | None = None,
        event_bridge: AgentEventBridge | None = None,
        max_sessions: int = 64,
        max_concurrent_runs: int | None = None,
        close_timeout: float = 30.0,
        engine_factory: EngineFactory | None = None,
        require_engine: bool = False,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        if max_concurrent_runs is not None and max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        if require_engine and engine_factory is None:
            raise ValueError("require_engine=True demands an engine_factory")
        self.agent_factory = agent_factory
        self.session_store = session_store
        self.close_callback = close_callback
        self.event_bridge = event_bridge or AgentEventBridge()
        self.max_sessions = max_sessions
        self.close_timeout = close_timeout
        self.engine_factory = engine_factory
        self.require_engine = require_engine
        self.agents: OrderedDict[str, Agent] = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}
        self.closed = False
        self._run_slots = (
            asyncio.Semaphore(max_concurrent_runs) if max_concurrent_runs is not None else None
        )
        self._session_users: dict[str, int] = {}
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._active_runs = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active_runs(self) -> int:
        return self._active_runs

    def health(self) -> dict[str, int | bool]:
        return {
            "active_runs": self._active_runs,
            "cached_sessions": len(self.agents),
            "session_locks": len(self.locks),
            "subscribers": self.event_bridge.subscriber_count,
            "dropped_events": self.event_bridge.dropped_events,
            "closed": self.closed,
        }

    def _agent(self, session_id: str) -> Agent:
        agent = self.agents.get(session_id)
        if agent is not None:
            self.agents.move_to_end(session_id)
            return agent
        agent = self.agent_factory(session_id)
        agent.history.extend(self.session_store.load_messages(session_id))
        agent.session_id = session_id
        agent.context_store = self.session_store
        self.agents[session_id] = agent
        return agent

    def _evict_idle_sessions(self) -> None:
        for session_id in list(self.agents):
            if len(self.agents) <= self.max_sessions:
                return
            if self._session_users.get(session_id, 0) == 0:
                del self.agents[session_id]

    def _release_session(self, session_id: str) -> None:
        remaining = self._session_users.get(session_id, 1) - 1
        if remaining > 0:
            self._session_users[session_id] = remaining
            return
        self._session_users.pop(session_id, None)
        lock = self.locks.get(session_id)
        if lock is not None and not lock.locked():
            del self.locks[session_id]
        self._evict_idle_sessions()

    def run(
        self,
        session_id: str,
        text: str,
        *,
        task_id: str | None = None,
        run_factory: RunFactory | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(session_id, text, task_id, None, True, run_factory)

    def run_agent(
        self,
        session_id: str,
        agent: Agent,
        text: str,
        *,
        task_id: str | None = None,
        persist: bool = True,
        run_factory: RunFactory | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(session_id, text, task_id, agent, persist, run_factory)

    async def _stream(
        self,
        session_id: str,
        text: str,
        task_id: str | None,
        supplied: Agent | None,
        persist: bool,
        run_factory: RunFactory | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not session_id.strip():
            raise ValueError("session id must not be empty")
        if not text.strip():
            raise ValueError("message text must not be empty")
        async with self._lifecycle_lock:
            if self.closed:
                raise RuntimeError("agent runtime is closed")
            self._active_runs += 1
            self._idle.clear()
            self._session_users[session_id] = self._session_users.get(session_id, 0) + 1
            current = asyncio.current_task()
            if current is not None:
                self._active_tasks.add(current)
        try:
            lock = self.locks.setdefault(session_id, asyncio.Lock())
            async with lock, contextlib.AsyncExitStack() as stack:
                if self._run_slots is not None:
                    await stack.enter_async_context(self._run_slots)
                agent = supplied if supplied is not None else self._agent(session_id)
                started = time.monotonic()
                events = 0
                correlation = {"session_id": session_id, "task_id": task_id or ""}
                try:
                    if self.engine_factory is not None:
                        run = run_factory(text) if run_factory is not None else None
                        async for event in self._stream_durable(
                            session_id, text, task_id, agent, run
                        ):
                            events += 1
                            yield event
                    elif self.require_engine:
                        raise RuntimeError(
                            "durable execution requires an engine factory; ephemeral "
                            "execution is not permitted for this runtime"
                        )
                    else:
                        async for event in agent.run(text):
                            events += 1
                            await self.event_bridge.publish(session_id, event, task_id=task_id)
                            yield event
                    log.info(
                        "Run finished events=%d duration_ms=%d",
                        events,
                        elapsed_ms(started),
                        extra={"correlation": correlation},
                    )
                except Exception as exc:
                    log.warning(
                        "Run failed category=%s events=%d duration_ms=%d",
                        classify_error(exc),
                        events,
                        elapsed_ms(started),
                        extra={"correlation": correlation},
                    )
                    raise
                finally:
                    if persist:
                        self._persist(session_id, agent)
        finally:
            async with self._lifecycle_lock:
                self._release_session(session_id)
                self._active_runs -= 1
                current = asyncio.current_task()
                if current is not None:
                    self._active_tasks.discard(current)
                if self._active_runs == 0:
                    self._idle.set()

    async def _stream_durable(
        self,
        session_id: str,
        text: str,
        task_id: str | None,
        agent: Agent,
        run: Run | None = None,
    ) -> AsyncIterator[AgentEvent]:
        engine = self.engine_factory(agent) if self.engine_factory is not None else None
        if engine is None:
            if self.require_engine:
                raise RuntimeError(
                    "durable execution requires the engine factory to return an AgentEngine"
                )
            async for event in agent.run(text):
                await self.event_bridge.publish(session_id, event, task_id=task_id)
                yield event
            return
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

        async def forward(event: AgentEvent) -> None:
            await self.event_bridge.publish(session_id, event, task_id=task_id)
            queue.put_nowait(event)

        engine_task = asyncio.create_task(
            engine.run(text, session_id=session_id, on_event=forward, run=run)
        )
        try:
            while True:
                get_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {engine_task, get_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if engine_task in done:
                    get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                    while not queue.empty():
                        drained: AgentEvent | None = queue.get_nowait()
                        if drained is not None:
                            yield drained
                    engine_task.result()
                    return
                streamed: AgentEvent | None = get_task.result()
                if streamed is None:
                    continue
                yield streamed
        except BaseException:
            engine_task.cancel()
            await asyncio.gather(engine_task, return_exceptions=True)
            raise

    def _persist(self, session_id: str, agent: Agent) -> None:
        context = getattr(agent, "context", None)
        workspace = getattr(context, "workspace", None)
        scope = str(workspace.resolve()) if workspace is not None else "global"
        self.session_store.save_messages(session_id, agent.history, project_scope=scope)

    async def close(self) -> None:
        async with self._close_lock:
            if self.closed:
                return
            async with self._lifecycle_lock:
                self.closed = True
            try:
                await self._drain()
                if self.close_callback is not None:
                    await self.close_callback()
            finally:
                self.agents.clear()
                self.locks.clear()
                self.event_bridge.close()

    async def _drain(self) -> None:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self.close_timeout)
            return
        except TimeoutError:
            log.warning("Agent runtime drain timed out; cancelling %d runs", self._active_runs)
        current = asyncio.current_task()
        stragglers = [task for task in self._active_tasks if task is not current]
        for task in stragglers:
            task.cancel()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self.close_timeout)
        except TimeoutError:
            log.warning("Agent runtime still has %d active runs after cancel", self._active_runs)

    async def __aenter__(self) -> AgentRuntime:
        if self.closed:
            raise RuntimeError("agent runtime is closed")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()
