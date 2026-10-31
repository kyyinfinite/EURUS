from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from eurus.gateway.approval import ApprovalBroker, ApprovalContext, PendingApproval

log = logging.getLogger(__name__)

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]

WEB_OWNER_ID = "web"


def _envelope(pending: PendingApproval, session_id: str) -> dict[str, Any]:
    return {
        "token": pending.token,
        "run_id": pending.task_id,
        "session_id": session_id,
        "tool_name": pending.tool_name,
        "arguments_json": pending.arguments_json,
        "display_notice": pending.display_notice,
        "fingerprint": pending.fingerprint,
        "expires_in_seconds": max(0, int(pending.expires_at - time.monotonic())),
    }


class WebApprovalBroker(ApprovalBroker):
    def __init__(
        self,
        *,
        broadcast: Broadcast,
        timeout: float = 600.0,
        persistence_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(timeout=timeout, persistence_path=persistence_path, **kwargs)
        self._broadcast = broadcast
        self.notifier = self._notify

    async def _notify(self, chat_id: str, prompt: str) -> None:
        with contextlib.suppress(ValueError):
            payload = json.loads(prompt)
            if isinstance(payload, dict):
                await self._broadcast(chat_id, {"type": "approval_required", **payload})
                return
        await self._broadcast(chat_id, {"type": "approval_required", "text": prompt})

    def _session_for(self, pending: PendingApproval) -> str:
        return str(pending.chat_id)

    def _render_prompt(
        self,
        context: ApprovalContext,
        pending: PendingApproval,
        tool_name: str,
        fingerprint: str,
    ) -> str:

        return json.dumps(
            _envelope(pending, self._session_for(pending)),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def pending_views(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                _envelope(pending, self._session_for(pending)) for pending in self.pending.values()
            ]

    async def resolve_for_session(self, token: str, *, session_id: str, allowed: bool) -> bool:

        async with self._lock:
            pending = self.pending.get(token)
            if pending is None or self._session_for(pending) != session_id:
                return False
        return await self.resolve(
            token,
            owner_id=pending.owner_id,
            chat_id=pending.chat_id,
            allowed=allowed,
        )
