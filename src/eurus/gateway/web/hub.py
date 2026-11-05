from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from eurus.runtime.models import TERMINAL_STATUSES, Run, RunStatus
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore

log = logging.getLogger(__name__)

WS_SEND_TIMEOUT_SECONDS = 5.0

ACTIVE_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_INPUT,
        RunStatus.RETRY_WAIT,
        RunStatus.VERIFYING,
    }
)

ACTIVITY_WINDOW_SECONDS = 2 * 3600.0
TAIL_INTERVAL_SECONDS = 0.6
SWEEP_INTERVAL_SECONDS = 2.0


def is_active(run: Run, *, now: float | None = None) -> bool:
    if run.status not in ACTIVE_STATUSES:
        return False
    moment = time.time() if now is None else now
    updated = _parse_ts(run.updated_at)
    if updated is None:
        return True
    return (moment - updated) <= ACTIVITY_WINDOW_SECONDS


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value) if value is not None else ""
    if not text:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


@dataclass(slots=True)
class WebRunHandle:
    run_id: str
    session_id: str
    started_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class HubBackpressure:
    clients: int = 0
    watched_sessions: int = 0
    tracked_runs: int = 0
    broadcasts_total: int = 0
    sends_total: int = 0
    send_timeouts_total: int = 0
    send_failures_total: int = 0
    dropped_clients_total: int = 0
    in_flight_sends: int = 0
    slowest_send_seconds: float = 0.0
    send_timeout_seconds: float = WS_SEND_TIMEOUT_SECONDS

    def to_dict(self) -> dict[str, int | float]:
        return {
            "clients": self.clients,
            "watched_sessions": self.watched_sessions,
            "tracked_runs": self.tracked_runs,
            "broadcasts_total": self.broadcasts_total,
            "sends_total": self.sends_total,
            "send_timeouts_total": self.send_timeouts_total,
            "send_failures_total": self.send_failures_total,
            "dropped_clients_total": self.dropped_clients_total,
            "in_flight_sends": self.in_flight_sends,
            "slowest_send_seconds": round(self.slowest_send_seconds, 4),
            "send_timeout_seconds": self.send_timeout_seconds,
        }


class SessionHub:
    def __init__(self, run_store: RunStore, event_store: EventStore) -> None:
        self.run_store = run_store
        self.event_store = event_store
        self.web_runs: dict[str, WebRunHandle] = {}
        self._subscriptions: dict[Any, set[str]] = {}
        self._cursors: dict[str, int] = {}
        self._known_external: set[str] = set()
        self._tailer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.send_timeout_seconds = WS_SEND_TIMEOUT_SECONDS
        self._broadcasts_total = 0
        self._sends_total = 0
        self._send_timeouts_total = 0
        self._send_failures_total = 0
        self._dropped_clients_total = 0
        self._in_flight_sends = 0
        self._slowest_send_seconds = 0.0

    async def attach(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions[websocket] = set()

    async def detach(self, websocket: Any) -> None:
        async with self._lock:
            self._subscriptions.pop(websocket, None)

    def subscribe(self, websocket: Any, session_ids: list[str]) -> None:
        known = self._subscriptions.get(websocket)
        if known is None:
            return
        known.update(session_ids)

    def unsubscribe(self, websocket: Any, session_ids: list[str]) -> None:
        known = self._subscriptions.get(websocket)
        if known is None:
            return
        known.difference_update(session_ids)

    @property
    def client_count(self) -> int:
        return len(self._subscriptions)

    def watched_sessions(self) -> set[str]:
        sessions: set[str] = set()
        for subscribed in self._subscriptions.values():
            sessions.update(subscribed)
        return sessions

    def backpressure(self) -> HubBackpressure:
        return HubBackpressure(
            clients=self.client_count,
            watched_sessions=len(self.watched_sessions()),
            tracked_runs=len(self._cursors),
            broadcasts_total=self._broadcasts_total,
            sends_total=self._sends_total,
            send_timeouts_total=self._send_timeouts_total,
            send_failures_total=self._send_failures_total,
            dropped_clients_total=self._dropped_clients_total,
            in_flight_sends=self._in_flight_sends,
            slowest_send_seconds=self._slowest_send_seconds,
            send_timeout_seconds=self.send_timeout_seconds,
        )

    async def broadcast(self, session_id: str | None, payload: dict[str, Any]) -> None:
        targets = [
            ws
            for ws, sessions in self._subscriptions.items()
            if session_id is None or session_id in sessions
        ]
        if not targets:
            return
        self._broadcasts_total += 1
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for websocket in targets:
            started = time.perf_counter()
            self._in_flight_sends += 1
            try:
                await asyncio.wait_for(websocket.send_text(text), timeout=self.send_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._send_timeouts_total += 1
                self._dropped_clients_total += 1
                log.debug("websocket send timed out; dropping client")
                with contextlib.suppress(Exception):
                    await websocket.close()
                self._subscriptions.pop(websocket, None)
            except Exception:
                self._send_failures_total += 1
                self._dropped_clients_total += 1
                log.debug("websocket send failed; dropping client", exc_info=True)
                with contextlib.suppress(Exception):
                    await websocket.close()
                self._subscriptions.pop(websocket, None)
            finally:
                self._in_flight_sends -= 1
                elapsed = time.perf_counter() - started
                self._sends_total += 1
                if elapsed > self._slowest_send_seconds:
                    self._slowest_send_seconds = elapsed

    def register_web_run(self, handle: WebRunHandle) -> None:
        self.web_runs[handle.run_id] = handle
        self._cursors[handle.run_id] = 0

    def release_web_run(self, run_id: str) -> None:
        self.web_runs.pop(run_id, None)
        self._cursors.pop(run_id, None)

    async def publish_agent_event(self, run_id: str, session_id: str, event: Any) -> None:
        sequence = self._cursors.get(run_id, 0) + 1
        self._cursors[run_id] = sequence
        await self.broadcast(
            session_id,
            {
                "type": "agent_event",
                "session_id": session_id,
                "run_id": run_id,
                "origin": "web",
                "sequence": sequence,
                "event": _agent_event_view(event),
            },
        )

    async def publish_run_status(
        self,
        run_id: str,
        session_id: str,
        status: str,
        *,
        stop_reason: str | None = None,
        origin: str = "web",
    ) -> None:
        await self.broadcast(
            session_id,
            {
                "type": "run_status",
                "session_id": session_id,
                "run_id": run_id,
                "status": status,
                "stop_reason": stop_reason,
                "origin": origin,
            },
        )

    def start_tailer(self) -> None:
        if self._tailer is None or self._tailer.done():
            self._tailer = asyncio.create_task(self._tail_loop(), name="eurus-web-tailer")

    async def stop_tailer(self) -> None:
        tailer, self._tailer = self._tailer, None
        if tailer is not None:
            tailer.cancel()
            await asyncio.gather(tailer, return_exceptions=True)

    async def _tail_loop(self) -> None:
        while True:
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("event tail sweep failed", exc_info=True)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    async def _sweep(self) -> None:
        sessions = self.watched_sessions()
        if not sessions:
            return
        try:
            active = await asyncio.to_thread(
                self.run_store.list_runs, statuses=tuple(ACTIVE_STATUSES), limit=1000
            )
        except Exception:
            log.debug("run sweep query failed", exc_info=True)
            return
        moment = time.time()
        external = [
            run
            for run in active
            if run.session_id in sessions
            and run.run_id not in self.web_runs
            and is_active(run, now=moment)
        ]
        if not external:
            return
        for run in external:
            await self._tail_run(run)

    async def _tail_run(self, run: Run) -> None:
        cursor = self._cursors.get(run.run_id, 0)
        try:
            events = await asyncio.to_thread(
                self.event_store.list_for_run, run.run_id, after_sequence=cursor, limit=500
            )
        except Exception:
            log.debug("event tail failed run_id=%s", run.run_id, exc_info=True)
            return
        for event in events:
            self._cursors[run.run_id] = event.sequence_no
            await self.broadcast(
                run.session_id,
                {
                    "type": "run_event",
                    "session_id": run.session_id,
                    "run_id": run.run_id,
                    "origin": "external",
                    "runtime_event": {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "sequence_no": event.sequence_no,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat(),
                        "step_id": event.step_id,
                        "attempt_id": event.attempt_id,
                    },
                },
            )
        self._known_external.add(run.run_id)
        refreshed = await asyncio.to_thread(self.run_store.get, run.run_id)
        if refreshed is not None and refreshed.status in TERMINAL_STATUSES:
            await self.publish_run_status(
                run.run_id,
                run.session_id or "",
                refreshed.status.value,
                stop_reason=refreshed.stop_reason,
                origin="external",
            )
            self._cursors.pop(run.run_id, None)
            self._known_external.discard(run.run_id)


def _agent_event_view(event: Any) -> dict[str, Any]:
    view: dict[str, Any] = {"kind": event.kind, "step": event.step}
    if event.text:
        view["text"] = event.text
    if event.tool_call is not None:
        view["tool_call"] = {
            "id": event.tool_call.id,
            "name": event.tool_call.name,
            "arguments": _redact_deep(event.tool_call.arguments),
        }
    if event.tool_result is not None:
        view["tool_result"] = {
            "call_id": event.tool_result.call_id,
            "name": event.tool_result.name,
            "output": event.tool_result.output[:2000],
            "is_error": event.tool_result.is_error,
            "error_code": event.tool_result.error_code,
        }
    if event.usage is not None:
        view["usage"] = {
            "input_tokens": event.usage.input_tokens,
            "output_tokens": event.usage.output_tokens,
            "cache_creation_tokens": event.usage.cache_creation_tokens,
            "cache_read_tokens": event.usage.cache_read_tokens,
        }
    if event.stop_reason:
        view["stop_reason"] = event.stop_reason
    return view


def _redact_deep(value: Any) -> Any:
    from eurus.security.redaction import redact_arguments

    try:
        return redact_arguments(value)
    except Exception:
        return value
