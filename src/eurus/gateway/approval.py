from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eurus.models import ToolCall
from eurus.security.approval import action_fingerprint
from eurus.security.redaction import redact_arguments
from eurus.storage.sqlite import managed
from eurus.tools.base import Tool

log = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
MAX_DISPLAYED_ARGUMENT_CHARS = 1800
MAX_APPROVAL_TIMEOUT = 3600.0


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    task_id: str
    owner_id: str
    chat_id: str


@dataclass(slots=True)
class PendingApproval:
    token: str
    task_id: str
    owner_id: str
    chat_id: str
    tool_name: str
    arguments_json: str
    future: asyncio.Future[bool]
    fingerprint: str = ""
    expires_at: float = float("inf")
    display_notice: str = ""


Notifier = Callable[[str, str], Awaitable[None]]
ApprovalStateCallback = Callable[[str, bool], Awaitable[None]]

current_approval_context: contextvars.ContextVar[ApprovalContext | None] = contextvars.ContextVar(
    "eurus_approval_context", default=None
)


def parse_approval_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("approval timeout must be a number of seconds") from exc
    if not 0 < timeout <= MAX_APPROVAL_TIMEOUT:
        raise ValueError(f"approval timeout must be between 0 and {int(MAX_APPROVAL_TIMEOUT)}")
    return timeout


class ApprovalBroker:
    def __init__(
        self,
        *,
        timeout: float = 120.0,
        state_callback: ApprovalStateCallback | None = None,
        max_pending: int = 32,
        max_pending_per_owner: int = 5,
        max_failed_attempts: int = 5,
        failure_window: float = 60.0,
        lockout_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        persistence_path: str | Path | None = None,
    ) -> None:
        if not 0 < timeout <= MAX_APPROVAL_TIMEOUT:
            raise ValueError("approval timeout must be positive and at most one hour")
        if max_pending < 1 or max_pending_per_owner < 1:
            raise ValueError("pending approval limits must be positive")
        if max_failed_attempts < 1 or failure_window <= 0 or lockout_seconds <= 0:
            raise ValueError("approval lockout settings must be positive")
        self.timeout = timeout
        self.state_callback = state_callback
        self.max_pending = max_pending
        self.max_pending_per_owner = max_pending_per_owner
        self.max_failed_attempts = max_failed_attempts
        self.failure_window = failure_window
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self.persistence_path = str(persistence_path) if persistence_path is not None else None
        if self.persistence_path is not None:
            self._initialize_persistence()
        self.notifier: Notifier | None = None
        self.pending: dict[str, PendingApproval] = {}
        self._failures: dict[str, deque[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _initialize_persistence(self) -> None:
        if self.persistence_path is None:
            return
        with managed(self.persistence_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_approval_requests ("
                "token_hash TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner_id TEXT NOT NULL, "
                "chat_id TEXT NOT NULL, tool_name TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL NOT NULL, "
                "decided_at TEXT, decision INTEGER)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS gateway_approval_task_status "
                "ON gateway_approval_requests(task_id, status)"
            )
            db.execute(
                "UPDATE gateway_approval_requests SET status='interrupted', decided_at=? "
                "WHERE status='pending'",
                (datetime.now(UTC).isoformat(),),
            )

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _persist_request(self, pending: PendingApproval) -> None:
        if self.persistence_path is None:
            return
        with managed(self.persistence_path) as db:
            db.execute(
                "INSERT INTO gateway_approval_requests "
                "(token_hash, task_id, owner_id, chat_id, tool_name, fingerprint, "
                "status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    self._token_hash(pending.token),
                    pending.task_id,
                    pending.owner_id,
                    str(pending.chat_id),
                    pending.tool_name,
                    pending.fingerprint,
                    datetime.now(UTC).isoformat(),
                    time.time() + self.timeout,
                ),
            )

    def _persist_decision(self, pending: PendingApproval, allowed: bool, status: str) -> None:
        if self.persistence_path is None:
            return
        with managed(self.persistence_path) as db:
            db.execute(
                "UPDATE gateway_approval_requests SET status=?, decided_at=?, decision=? "
                "WHERE token_hash=? AND status='pending'",
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    int(allowed),
                    self._token_hash(pending.token),
                ),
            )

    def bind_notifier(self, notifier: Notifier) -> None:
        self.notifier = notifier

    def is_locked_out(self, owner_id: str) -> bool:
        until = self._lockouts.get(owner_id)
        if until is None:
            return False
        if self.clock() >= until:
            del self._lockouts[owner_id]
            return False
        return True

    def _record_failure(self, owner_id: str) -> None:
        now = self.clock()
        attempts = self._failures.setdefault(owner_id, deque())
        attempts.append(now)
        while attempts and now - attempts[0] > self.failure_window:
            attempts.popleft()
        if len(attempts) >= self.max_failed_attempts:
            self._lockouts[owner_id] = now + self.lockout_seconds
            del self._failures[owner_id]
            log.warning("Approval attempts locked out owner_id=%s", owner_id)

    def _render_arguments(self, call: ToolCall) -> tuple[str, str]:
        serialized = json.dumps(
            redact_arguments(call.arguments),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        shown = serialized[:MAX_DISPLAYED_ARGUMENT_CHARS]
        hidden = len(serialized) - len(shown)
        notice = f"\n[{hidden} more characters not shown]" if hidden else ""
        return shown, notice

    def _render_prompt(
        self,
        context: ApprovalContext,
        pending: PendingApproval,
        tool_name: str,
        fingerprint: str,
    ) -> str:
        """Render the human-facing prompt for a pending approval.

        Subclasses may override this to surface approvals through other
        channels; the safety machinery (limits, persistence, timeouts) is
        shared and must not be bypassed.
        """
        return (
            "Owner approval required\n"
            f"Task: {context.task_id[:8]}\n"
            f"Tool: {tool_name}\n"
            f"Arguments: {pending.arguments_json}{pending.display_notice}\n"
            f"Action: {fingerprint[:12]}\n"
            f"Approve: /approve {pending.token}\n"
            f"Deny: /deny {pending.token}\n"
            f"Expires in {int(self.timeout)} seconds."
        )

    async def approve(self, call: ToolCall, tool: Tool) -> bool:
        context = current_approval_context.get()
        notifier = self.notifier
        state_callback = self.state_callback
        if context is None or notifier is None:
            return False
        token = secrets.token_urlsafe(9)
        shown, notice = self._render_arguments(call)
        fingerprint = action_fingerprint(call)
        pending = PendingApproval(
            token=token,
            task_id=context.task_id,
            owner_id=context.owner_id,
            chat_id=context.chat_id,
            tool_name=tool.name,
            arguments_json=shown,
            future=asyncio.get_running_loop().create_future(),
            fingerprint=fingerprint,
            expires_at=self.clock() + self.timeout,
            display_notice=notice,
        )
        async with self._lock:
            owned = sum(1 for item in self.pending.values() if item.owner_id == context.owner_id)
            if len(self.pending) >= self.max_pending or owned >= self.max_pending_per_owner:
                log.warning(
                    "Approval refused: too many pending approvals task_id=%s", context.task_id
                )
                pending.future.cancel()
                return False
            self.pending[token] = pending
        try:
            self._persist_request(pending)
        except Exception as exc:
            async with self._lock:
                self.pending.pop(token, None)
            log.error(
                "Approval persistence failed task_id=%s error_type=%s",
                context.task_id,
                type(exc).__name__,
            )
            return False
        prompt = self._render_prompt(context, pending, tool.name, fingerprint)
        decision = False
        try:
            if state_callback is not None:
                await state_callback(context.task_id, True)
            await notifier(context.chat_id, prompt)
            decision = await asyncio.wait_for(pending.future, timeout=self.timeout)
            return decision
        except TimeoutError:
            with contextlib.suppress(Exception):
                self._persist_decision(pending, False, "expired")
            return False
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self._persist_decision(pending, False, "cancelled")
            if not pending.future.done():
                pending.future.cancel()
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._persist_decision(pending, False, "notification_failed")
            log.warning(
                "Approval prompt failed task_id=%s error_type=%s",
                context.task_id,
                type(exc).__name__,
            )
            return False
        finally:
            async with self._lock:
                self.pending.pop(token, None)
            if state_callback is not None:
                with contextlib.suppress(Exception):
                    await state_callback(context.task_id, False)
            log.info(
                "Approval finished task_id=%s tool=%s action=%s allowed=%s",
                context.task_id,
                tool.name,
                fingerprint[:12],
                decision,
            )

    async def resolve(
        self,
        token: str,
        *,
        owner_id: str,
        chat_id: str,
        allowed: bool,
    ) -> bool:
        async with self._lock:
            if self.is_locked_out(owner_id):
                return False
            pending = self.pending.get(token) if TOKEN_PATTERN.fullmatch(token) else None
            valid = (
                pending is not None
                and pending.owner_id == owner_id
                and pending.chat_id == chat_id
                and not pending.future.done()
                and self.clock() < pending.expires_at
            )
            if pending is None or not valid:
                self._record_failure(owner_id)
                return False
            try:
                self._persist_decision(pending, allowed, "approved" if allowed else "denied")
            except Exception as exc:
                log.error(
                    "Approval decision persistence failed task_id=%s error_type=%s",
                    pending.task_id,
                    type(exc).__name__,
                )
                return False
            pending.future.set_result(allowed)
            return True

    async def cancel_task(self, task_id: str) -> None:
        async with self._lock:
            matches = [item for item in self.pending.values() if item.task_id == task_id]
            for item in matches:
                if not item.future.done():
                    item.future.set_result(False)
                with contextlib.suppress(Exception):
                    self._persist_decision(item, False, "cancelled")

    async def close(self) -> None:
        async with self._lock:
            pending = list(self.pending.values())
            self.pending.clear()
            self._failures.clear()
            self._lockouts.clear()
            for item in pending:
                if not item.future.done():
                    item.future.set_result(False)
                with contextlib.suppress(Exception):
                    self._persist_decision(item, False, "cancelled")
