from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from eurus.models import ToolCall
from eurus.security.redaction import redact_text, summarize_arguments
from eurus.tools.base import Tool


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    tool_name: str
    action_fingerprint: str
    summary: str
    risk_level: str = "high"


class ApprovalProvider(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> bool: ...


def action_fingerprint(call: ToolCall) -> str:
    payload = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approval_request(call: ToolCall, tool: Tool) -> ApprovalRequest:
    summary = redact_text(summarize_arguments(call.arguments))
    return ApprovalRequest(
        request_id=call.id,
        tool_name=tool.name,
        action_fingerprint=action_fingerprint(call),
        summary=summary,
        risk_level="high" if tool.requires_approval else "medium",
    )


NEVER_SESSION_SCOPED = frozenset(
    {
        "shell",
        "delegate_task",
        "delegate_code_task",
        "browser_screenshot",
        "remember_memory",
        "replace_memory",
        "forget_memory",
    }
)
SESSION_SCOPE_ALLOWED = frozenset(
    {
        "apply_patch",
        "document_create",
        "document_replace",
        "office_replace",
        "apply_patchset",
    }
)
DEFAULT_SESSION_SCOPE_TTL_SECONDS = 3600.0


@dataclass(slots=True)
class SessionScopeGrant:
    fingerprint: str
    tool_name: str
    granted_at: float
    expires_at: float


class SessionScopedApprover:
    def __init__(
        self,
        base: Callable[[ToolCall, Tool], Awaitable[bool] | bool],
        *,
        ttl_seconds: float = DEFAULT_SESSION_SCOPE_TTL_SECONDS,
        allowed_tools: frozenset[str] = SESSION_SCOPE_ALLOWED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base = base
        self.ttl_seconds = ttl_seconds
        self.allowed_tools = allowed_tools
        self.clock = clock
        self.grants: dict[str, SessionScopeGrant] = {}

    async def __call__(self, call: ToolCall, tool: Tool) -> bool:
        fingerprint = action_fingerprint(call)
        grant = self.grants.get(fingerprint)
        if (
            grant is not None
            and tool.name in self.allowed_tools
            and tool.name not in NEVER_SESSION_SCOPED
            and grant.tool_name == tool.name
            and self.clock() < grant.expires_at
        ):
            return True
        if grant is not None:
            del self.grants[fingerprint]
        allowed = await self._ask_base(call, tool)
        if allowed and tool.name in self.allowed_tools and tool.name not in NEVER_SESSION_SCOPED:
            now = self.clock()
            self.grants[fingerprint] = SessionScopeGrant(
                fingerprint=fingerprint,
                tool_name=tool.name,
                granted_at=now,
                expires_at=now + self.ttl_seconds,
            )
        return allowed

    async def _ask_base(self, call: ToolCall, tool: Tool) -> bool:
        decision = self.base(call, tool)
        if isinstance(decision, Awaitable):
            return bool(await decision)
        return bool(decision)

    def revoke(self, fingerprint: str) -> bool:
        return self.grants.pop(fingerprint, None) is not None

    def revoke_all(self) -> int:
        count = len(self.grants)
        self.grants.clear()
        return count

    def active_grants(self) -> list[dict[str, object]]:
        now = self.clock()
        return [
            {
                "fingerprint": grant.fingerprint,
                "tool": grant.tool_name,
                "expires_in": max(0.0, grant.expires_at - now),
            }
            for grant in self.grants.values()
            if now < grant.expires_at
        ]
