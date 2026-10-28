from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from eurus.gateway.approval import ApprovalBroker, ApprovalContext, current_approval_context
from eurus.gateway.tasks import (
    ORPHANABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    DurableTask,
    InvalidTaskTransition,
    TaskStatus,
    TaskStore,
)
from eurus.observability import bind_context, classify_error, elapsed_ms, unbind_context
from eurus.reminders import reset_reminder_context, set_reminder_context
from eurus.runtime.event_bridge import AgentEventBridge, SessionEvent
from eurus.runtime.recovery import FailureKind, classify_failure_kind

log = logging.getLogger(__name__)

TaskRunner = Callable[[DurableTask], Awaitable[str]]
Notifier = Callable[[str, str], Awaitable[None]]
RETRY_BACKOFF_SECONDS = 5.0
MAX_TASK_ATTEMPTS = 5


class LiveResponse(Protocol):
    async def update(self, text: str, *, final: bool = False) -> None: ...


LiveResponseFactory = Callable[[int | str], LiveResponse]


class BackgroundTaskManager:
    def __init__(
        self,
        store: TaskStore,
        runner: TaskRunner,
        notifier: Notifier,
        *,
        max_concurrent: int = 2,
        max_pending_per_owner: int | None = None,
        task_timeout: float = 3600.0,
        event_bridge: AgentEventBridge | None = None,
        live_response_factory: LiveResponseFactory | None = None,
        shutdown_timeout: float = 10.0,
    ) -> None:
        if not 1 <= max_concurrent <= 8:
            raise ValueError("max_concurrent must be between 1 and 8")
        if max_pending_per_owner is not None and max_pending_per_owner < 1:
            raise ValueError("max_pending_per_owner must be positive")
        if task_timeout <= 0:
            raise ValueError("task_timeout must be positive")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        self.store = store
        self.runner = runner
        self.task_timeout = task_timeout
        self.notifier = notifier
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self.max_pending = max_concurrent * 8
        self.max_pending_per_owner = max_pending_per_owner
        self.active: dict[str, asyncio.Task[None]] = {}
        self.cancelled: set[str] = set()
        self._owners: dict[str, str] = {}
        self._submit_lock = asyncio.Lock()
        self.approval_broker: ApprovalBroker | None = None
        self.event_bridge = event_bridge
        self.live_response_factory = live_response_factory
        self.shutdown_timeout = shutdown_timeout
        self._closed = False

    async def submit(
        self,
        owner_id: str,
        session_id: str,
        objective: str,
        *,
        notify_chat_id: int | str | None = None,
        max_attempts: int = 1,
        deadline_at: str | None = None,
    ) -> DurableTask:
        async with self._submit_lock:
            if self._closed:
                raise RuntimeError("background task manager is shut down")
            active_count = sum(not worker.done() for worker in self.active.values())
            if active_count >= self.max_pending:
                raise RuntimeError("background task capacity reached")
            if self.max_pending_per_owner is not None:
                owned = sum(
                    1
                    for task_id, worker in self.active.items()
                    if not worker.done() and self._owners.get(task_id) == owner_id
                )
                if owned >= self.max_pending_per_owner:
                    raise RuntimeError("owner task capacity reached")
            state = {"notify_chat_id": notify_chat_id if notify_chat_id is not None else owner_id}
            task = self.store.create(
                owner_id,
                session_id,
                objective,
                state,
                max_attempts=max(1, min(max_attempts, MAX_TASK_ATTEMPTS)),
                deadline_at=deadline_at,
            )
            self._spawn(task)
            return task

    def _spawn(self, task: DurableTask) -> asyncio.Task[None]:
        worker = asyncio.create_task(self._execute(task), name=f"eurus-task-{task.task_id}")
        self.active[task.task_id] = worker
        owner = self.store.get(task.task_id)
        self._owners[task.task_id] = owner.owner_id if owner is not None else task.owner_id

        def release(_: asyncio.Future[None]) -> None:
            self.active.pop(task.task_id, None)
            self._owners.pop(task.task_id, None)
            self.cancelled.discard(task.task_id)

        worker.add_done_callback(release)
        return worker

    def _task_timeout(self, task: DurableTask) -> float:
        timeout = self.task_timeout
        if task.deadline_at:
            try:
                deadline = datetime.fromisoformat(task.deadline_at)
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                timeout = min(timeout, max(0.0, remaining))
            except ValueError:
                log.warning("task deadline is malformed task_id=%s", task.task_id)
        return timeout

    async def resume_pending(self, *, limit: int | None = None) -> int:
        async with self._submit_lock:
            if self._closed:
                return 0
            queued = self.store.list_by_status("queued", limit=100)
            active_ids = set(self.active)
            dispatchable = [task for task in queued if task.task_id not in active_ids][
                : limit if limit is not None else self.max_pending
            ]
            for task in dispatchable:
                self._spawn(task)
            return len(dispatchable)

    async def pause(self, task_id: str, owner_id: str) -> bool:
        task = self.store.get(task_id, owner_id=owner_id)
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return False
        worker = self.active.get(task_id)
        if worker is not None:
            self.cancelled.add(task_id)
        await self._release_approvals(task_id)
        try:
            self.store.interrupt(task_id, "paused by user")
        except (InvalidTaskTransition, KeyError):
            return False
        if worker is not None:
            worker.cancel()
        return True

    async def resume(
        self, task_id: str, owner_id: str, *, acknowledge_side_effects: bool = False
    ) -> DurableTask | None:
        task = self.store.get(task_id, owner_id=owner_id)
        if task is None or task_id in self.active:
            return None
        try:
            resumed = self.store.requeue_interrupted(
                task_id, owner_id, acknowledge_side_effects=acknowledge_side_effects
            )
        except (InvalidTaskTransition, KeyError):
            return None
        self._spawn(resumed)
        return resumed

    async def _execute(self, task: DurableTask) -> None:
        context_token: contextvars.Token[ApprovalContext | None] | None = None
        subscriber_id: str | None = None
        event_queue: asyncio.Queue[SessionEvent] | None = None
        event_worker: asyncio.Task[None] | None = None
        stream_state: dict[str, Any] = {"response": None, "text": ""}
        started = time.monotonic()
        log_token = bind_context(task_id=task.task_id, session_id=task.session_id)
        try:
            async with self.semaphore:
                claimed = self.store.claim(task.task_id)
                if claimed is None:
                    log.info("Task claim skipped: not queued or already claimed")
                    return
                log.info("Task started attempt=%s", claimed.state.get("attempts"))
                chat_id = task.state.get("notify_chat_id", task.owner_id)
                context_token = current_approval_context.set(
                    ApprovalContext(
                        task_id=task.task_id,
                        owner_id=task.owner_id,
                        chat_id=chat_id,
                    )
                )
                if self.live_response_factory is not None:
                    stream_state["response"] = self.live_response_factory(chat_id)
                    start_response = getattr(stream_state["response"], "start", None)
                    if callable(start_response):
                        try:
                            await start_response()
                        except Exception as exc:
                            log.debug(
                                "Telegram thinking indicator unavailable error_type=%s",
                                type(exc).__name__,
                            )
                if self.event_bridge is not None:
                    subscriber_id, event_queue = self.event_bridge.subscribe(
                        task.session_id, capacity=256, task_id=task.task_id
                    )
                    event_worker = asyncio.create_task(
                        self._forward_progress(chat_id, event_queue, stream_state),
                        name=f"eurus-events-{task.task_id}",
                    )
                reminder_context_token = set_reminder_context(
                    task.owner_id, str(chat_id), "telegram"
                )
                try:
                    result = await asyncio.wait_for(
                        self.runner(task), timeout=self._task_timeout(task)
                    )
                    if task.task_id in self.cancelled:
                        self._settle(task.task_id, "cancelled")
                        log.info("Task cancelled duration_ms=%d", elapsed_ms(started))
                        await self._notify_safely(
                            chat_id, "Your request was cancelled.", task.task_id
                        )
                        return
                    if event_queue is not None:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(event_queue.join(), timeout=3.0)
                    response_stream = stream_state["response"]
                    if response_stream is not None:
                        try:
                            await response_stream.update(result, final=True)
                        except Exception as exc:
                            log.warning(
                                "Telegram response finalization failed task_id=%s error_type=%s",
                                task.task_id,
                                type(exc).__name__,
                            )
                            response_stream = None
                    try:
                        self.store.complete(task.task_id, {"result": result})
                    except (InvalidTaskTransition, KeyError):
                        return
                    log.info("Task completed duration_ms=%d", elapsed_ms(started))
                    if response_stream is None:
                        await self._notify_safely(chat_id, result, task.task_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._release_approvals(task.task_id)
                    kind = classify_failure_kind(error=f"{type(exc).__name__}: {exc}")
                    current = self.store.get(task.task_id)
                    attempts = current.attempts_used if current is not None else 1
                    cap = current.max_attempts if current is not None else 1
                    if kind is FailureKind.TRANSIENT and attempts < cap:
                        state = dict(current.state) if current is not None else {}
                        state["failure_kind"] = kind.value
                        state["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                        self._settle(task.task_id, "retry_wait", state=state)
                        log.info(
                            "Task scheduled for retry attempt=%d cap=%d kind=%s",
                            attempts,
                            cap,
                            kind.value,
                        )
                        retry = asyncio.create_task(
                            self._retry_after(task.task_id, RETRY_BACKOFF_SECONDS)
                        )
                        self.active[task.task_id] = retry
                        self._owners[task.task_id] = task.owner_id

                        def release_retry(_: asyncio.Future[None]) -> None:
                            self.active.pop(task.task_id, None)
                            self._owners.pop(task.task_id, None)

                        retry.add_done_callback(release_retry)
                        await self._notify_safely(
                            chat_id,
                            "Transient failure; the task will retry automatically.",
                            task.task_id,
                        )
                        return
                    self._settle(task.task_id, "failed", error=type(exc).__name__)
                    log.warning(
                        "Background task failed task_id=%s error_type=%s category=%s "
                        "duration_ms=%d",
                        task.task_id,
                        type(exc).__name__,
                        classify_error(exc),
                        elapsed_ms(started),
                    )
                    await self._notify_safely(
                        chat_id, "Request failed. Please try again.", task.task_id
                    )
                finally:
                    reset_reminder_context(reminder_context_token)
        except asyncio.CancelledError:
            await self._release_approvals(task.task_id)
            current = self.store.get(task.task_id)
            if current and current.status in ORPHANABLE_TASK_STATUSES:
                self._settle(task.task_id, "cancelled")
            log.info("Task stopped by cancellation duration_ms=%d", elapsed_ms(started))
            raise
        finally:
            self.cancelled.discard(task.task_id)
            if event_worker is not None:
                event_worker.cancel()
                await asyncio.gather(event_worker, return_exceptions=True)
            if subscriber_id is not None and self.event_bridge is not None:
                self.event_bridge.unsubscribe(task.session_id, subscriber_id)
            if context_token is not None:
                current_approval_context.reset(context_token)
            unbind_context(log_token)

    async def _retry_after(self, task_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            self.store.transition(task_id, "queued")
        except (InvalidTaskTransition, KeyError):
            return
        task = self.store.get(task_id)
        if task is None or task_id in self.active:
            return
        self._spawn(task)

    def _settle(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        state: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        try:
            self.store.transition(task_id, status, state=state, error=error)
        except (InvalidTaskTransition, KeyError):
            return False
        return True

    async def _release_approvals(self, task_id: str) -> None:
        if self.approval_broker is not None:
            await self.approval_broker.cancel_task(task_id)

    async def _notify_safely(self, chat_id: str, text: str, task_id: str) -> None:
        try:
            await self.notifier(chat_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Task notification failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )

    async def _forward_progress(
        self,
        chat_id: str,
        events: asyncio.Queue[SessionEvent],
        state: dict[str, Any],
    ) -> None:
        last_update = 0.0
        pending_update = False
        while True:
            try:
                envelope = await asyncio.wait_for(events.get(), timeout=0.35)
            except TimeoutError:
                if state["response"] is not None:
                    heartbeat = getattr(state["response"], "heartbeat", None)
                    if callable(heartbeat):
                        try:
                            await heartbeat(state["text"])
                        except Exception as exc:
                            log.debug(
                                "Telegram draft heartbeat failed error_type=%s",
                                type(exc).__name__,
                            )
                if pending_update and state["response"] is not None:
                    try:
                        await state["response"].update(state["text"])
                        last_update = time.monotonic()
                        pending_update = False
                    except Exception as exc:
                        log.debug("Telegram stream update failed error_type=%s", type(exc).__name__)
                continue
            try:
                event = envelope.event
                if (
                    event.kind == "tool_call"
                    and event.tool_call is not None
                    and self.live_response_factory is None
                ):
                    try:
                        await self.notifier(
                            chat_id, f"Working with tool: {event.tool_call.name[:80]}"
                        )
                    except Exception as exc:
                        log.debug(
                            "Task progress notification failed error_type=%s",
                            type(exc).__name__,
                        )
                if event.kind == "text_reset":
                    state["text"] = ""
                if event.kind == "text" and event.text:
                    state["text"] += event.text
                    if self.live_response_factory is not None:
                        if state["response"] is None:
                            state["response"] = self.live_response_factory(chat_id)
                            start_response = getattr(state["response"], "start", None)
                            if callable(start_response):
                                try:
                                    await start_response()
                                except Exception as exc:
                                    log.debug(
                                        "Telegram thinking indicator unavailable error_type=%s",
                                        type(exc).__name__,
                                    )
                        now = time.monotonic()
                        if now - last_update >= 1.0:
                            try:
                                await state["response"].update(state["text"])
                                last_update = time.monotonic()
                                pending_update = False
                            except Exception as exc:
                                log.debug(
                                    "Telegram stream update failed error_type=%s",
                                    type(exc).__name__,
                                )
                        else:
                            pending_update = True
            finally:
                events.task_done()

    async def cancel(self, task_id: str, owner_id: str) -> bool:
        task = self.store.get(task_id, owner_id=owner_id)
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return False
        worker = self.active.get(task_id)
        if worker is not None:
            self.cancelled.add(task_id)
        await self._release_approvals(task_id)
        try:
            self.store.transition(task_id, "cancelled", owner_id=owner_id)
        except InvalidTaskTransition:
            return False
        if worker is not None:
            worker.cancel()
        return True

    def health(self) -> dict[str, int | bool]:
        broker = self.approval_broker
        return {
            "active_tasks": sum(not worker.done() for worker in self.active.values()),
            "max_concurrent": self.max_concurrent,
            "max_pending": self.max_pending,
            "pending_approvals": len(broker.pending) if broker is not None else 0,
            "closed": self._closed,
        }

    async def shutdown(self) -> None:
        self._closed = True
        snapshot = self.health()
        log.info(
            "Task manager shutting down active_tasks=%d pending_approvals=%d",
            snapshot["active_tasks"],
            snapshot["pending_approvals"],
        )
        workers = list(self.active.items())
        for task_id, worker in workers:
            await self._release_approvals(task_id)
            with contextlib.suppress(InvalidTaskTransition, KeyError):
                self.store.interrupt(task_id, "gateway shutdown; review before resuming")
            worker.cancel()
        if not workers:
            return
        _, pending = await asyncio.wait(
            [worker for _, worker in workers], timeout=self.shutdown_timeout
        )
        if pending:
            log.warning("Background tasks did not stop within %.1fs", self.shutdown_timeout)
        for _, worker_task in workers:
            if worker_task.done() and not worker_task.cancelled():
                worker_task.exception()
