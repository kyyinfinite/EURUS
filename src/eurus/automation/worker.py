from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeAlias

from eurus.storage.jobs import Job, JobStore

log = logging.getLogger(__name__)

WORKER_CANCEL_GRACE_SECONDS = 10.0

JobHandler: TypeAlias = Callable[[Job], Awaitable[str]]
FinishHandler: TypeAlias = Callable[[Job, str, str, str | None], Awaitable[None] | None]
RetryDecider: TypeAlias = Callable[[Job, BaseException], Awaitable[tuple[bool, float, str]]]
ClaimFunction: TypeAlias = Callable[..., Job | None]


def _retrieve_task_outcome(task: asyncio.Future[str]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("abandoned job handler failed during shutdown: %s", exc)


class AutomationWorkerPool:
    def __init__(
        self,
        store: JobStore,
        handler: JobHandler,
        *,
        workers: int = 2,
        poll_seconds: float = 1.0,
        lease_seconds: int = 60,
        on_finish: FinishHandler | None = None,
        retry_decider: RetryDecider | None = None,
        claim_function: ClaimFunction | None = None,
        defer_side_effect_marking: bool = False,
        recovery_owner_id: str | None = None,
        recover_expired_on_poll: bool = True,
    ) -> None:
        if not 1 <= workers <= 64:
            raise ValueError("workers must be between 1 and 64")
        if not 0.05 <= poll_seconds <= 300:
            raise ValueError("poll_seconds must be between 0.05 and 300")
        if not 5 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 5 and 86400")
        self.store = store
        self.handler = handler
        self.workers = workers
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.cancel_poll_seconds = min(1.0, max(0.05, poll_seconds))
        self.on_finish = on_finish
        self.retry_decider = retry_decider
        self.claim_function = claim_function
        self.defer_side_effect_marking = defer_side_effect_marking
        self.recovery_owner_id = recovery_owner_id
        self.recover_expired_on_poll = recover_expired_on_poll
        self._stopping = asyncio.Event()
        self.jobs_retried = 0
        self.jobs_finished = 0
        self.jobs_failed = 0
        self.commits_fenced = 0
        self._worker_last_seen: dict[str, float] = {}
        self._worker_idle_since: dict[str, float] = {}

    def liveness(self) -> dict[str, dict[str, float]]:
        current = time.monotonic()
        liveness: dict[str, dict[str, float]] = {}
        for worker_id, last_seen in self._worker_last_seen.items():
            liveness[worker_id] = {
                "last_seen_age_seconds": round(current - last_seen, 3),
                "idle": float(self._worker_idle_since.get(worker_id, 0.0) > 0),
            }
        return liveness

    def active_workers(self) -> int:
        current = time.monotonic()
        threshold = max(5.0, self.poll_seconds * 10)
        return sum(
            1
            for worker_id, last_seen in self._worker_last_seen.items()
            if current - last_seen <= threshold
        )

    async def run_once(self, worker_id: str | None = None, *, now: datetime | None = None) -> bool:
        identifier = worker_id or f"worker-{uuid.uuid4().hex}"
        if self.claim_function is not None:
            job = self.claim_function(identifier, now=now)
        else:
            job = self.store.claim(
                identifier,
                lease_seconds=self.lease_seconds,
                now=now,
                owner_id=self.recovery_owner_id,
            )
        if job is None:
            return False
        if not self.defer_side_effect_marking and not self.store.mark_side_effect_started(
            job.job_id, identifier, job.lease_version
        ):
            log.warning(
                "worker %s lost the race for job %s before execution started",
                identifier,
                job.job_id,
            )
            return True
        task: asyncio.Future[str] = asyncio.ensure_future(self.handler(job))
        heartbeat = asyncio.create_task(self._heartbeat(job, identifier, task))
        watcher = asyncio.create_task(self._watch_cancellation(job, task))
        status = "completed"
        result = ""
        error: str | None = None
        propagated_cancel: asyncio.CancelledError | None = None
        try:
            result = await task
        except asyncio.CancelledError as cancel_exc:
            status = "cancelled"
            error = "worker_cancelled"
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                propagated_cancel = cancel_exc
        except Exception as exc:
            current = self.store.get(job.job_id)
            retry_allowed = not self.defer_side_effect_marking or (
                current is not None and not current.side_effect_started
            )
            if retry_allowed and self.retry_decider is not None:
                should_retry, delay, reason = await self.retry_decider(job, exc)
                if should_retry:
                    retried = self.store.schedule_retry(
                        job.job_id,
                        identifier,
                        job.lease_version,
                        delay_seconds=delay,
                        error=reason,
                        require_clean_side_effect=self.defer_side_effect_marking,
                    )
                    if retried:
                        self.jobs_retried += 1
                        heartbeat.cancel()
                        watcher.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                        with contextlib.suppress(asyncio.CancelledError):
                            await watcher
                        return True
                    log.warning(
                        "retry scheduling lost the lease for job %s; recovery will classify it",
                        job.job_id,
                    )
            elif not retry_allowed:
                log.warning(
                    "job %s failed after crossing the side-effect boundary; "
                    "refusing automatic retry to avoid duplicate mutations",
                    job.job_id,
                )
            status = "failed"
            error = type(exc).__name__
            result = f"Job failed: {error}"
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task}, timeout=WORKER_CANCEL_GRACE_SECONDS)
            if not task.done():
                task.add_done_callback(_retrieve_task_outcome)
                task.cancel()
            heartbeat.cancel()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        committed = self.store.finish(
            job.job_id,
            identifier,
            job.lease_version,
            status=status,
            result=result,
            error=error,
        )
        if not committed:
            self.commits_fenced += 1
            log.warning(
                "commit for job %s was fenced (lease lost or state moved on); "
                "outcome=%s left to reconciliation",
                job.job_id,
                status,
            )
            return True
        if self.on_finish is not None:
            outcome = self.on_finish(job, status, result, error)
            if asyncio.iscoroutine(outcome):
                await outcome
        self.jobs_finished += 1
        if status == "failed":
            self.jobs_failed += 1
        if propagated_cancel is not None:
            raise propagated_cancel
        return True

    async def run_forever(self, worker_id: str | None = None) -> None:
        identifier = worker_id or f"worker-{uuid.uuid4().hex}"
        self._worker_last_seen[identifier] = time.monotonic()
        self._worker_idle_since[identifier] = time.monotonic()
        try:
            while not self._stopping.is_set():
                self._worker_last_seen[identifier] = time.monotonic()
                if self.recover_expired_on_poll:
                    try:
                        self.store.recover_expired(owner_id=self.recovery_owner_id)
                    except Exception:
                        log.exception("expired-lease recovery failed in worker poll loop")
                worked = await self.run_once(identifier)
                if worked:
                    self._worker_idle_since.pop(identifier, None)
                else:
                    self._worker_idle_since[identifier] = time.monotonic()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
        finally:
            self._worker_last_seen.pop(identifier, None)
            self._worker_idle_since.pop(identifier, None)

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self.run_forever(f"worker-{index + 1}"))
            for index in range(self.workers)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._stopping.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping.set()

    async def _heartbeat(self, job: Job, worker_id: str, task: asyncio.Future[str]) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not task.done():
            await asyncio.sleep(interval)
            if task.done():
                return
            self._worker_last_seen[worker_id] = time.monotonic()
            if not self.store.heartbeat(
                job.job_id, worker_id, job.lease_version, lease_seconds=self.lease_seconds
            ):
                log.warning(
                    "lease renewal failed for job %s by worker %s; cancelling execution",
                    job.job_id,
                    worker_id,
                )
                task.cancel()
                return

    async def _watch_cancellation(self, job: Job, task: asyncio.Future[str]) -> None:
        while not task.done():
            await asyncio.sleep(self.cancel_poll_seconds)
            if task.done():
                return
            current = self.store.get(job.job_id)
            if current is not None and current.cancel_requested:
                log.info("cancellation requested for job %s", job.job_id)
                task.cancel()
                return
