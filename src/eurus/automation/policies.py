from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from eurus.runtime.budgets import BudgetSpec
from eurus.storage.jobs import Job

log = logging.getLogger(__name__)

TOOL_ALLOW_ALL = "*"

DEFAULT_MAX_RUNTIME_SECONDS = 1800
DEFAULT_MAX_STEPS = 20
POLICY_SCOPES = frozenset(
    {"automation", "scheduled", "goal", "workflow", "event", "specialist", "manual"}
)

SpecialistProfile = Mapping[str, Any]
SpecialistProfileResolver = Callable[[str], SpecialistProfile | None]


class PolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    allowed_tools: frozenset[str] = frozenset()
    blocked_tools: frozenset[str] = frozenset()
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    max_steps: int = DEFAULT_MAX_STEPS
    deadline_at: datetime | None = None
    policy_scope: str = "automation"
    notifications: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def max_iterations(self) -> int:
        return self.max_steps

    def validate(self) -> None:
        if not 1 <= self.max_steps <= 1000:
            raise ValueError("max_steps must be between 1 and 1000")
        if not 1 <= self.max_runtime_seconds <= 86400:
            raise ValueError("max_runtime_seconds must be between 1 and 86400")
        if self.policy_scope not in POLICY_SCOPES:
            raise ValueError(f"unknown policy scope: {self.policy_scope}")
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at must include a timezone")
        overlap = self.allowed_tools & self.blocked_tools
        if overlap:
            raise ValueError(f"tools are both allowed and blocked: {sorted(overlap)}")

    def allows_tool(self, name: str) -> bool:
        if name in self.blocked_tools:
            return False
        if TOOL_ALLOW_ALL in self.allowed_tools:
            return True
        return name in self.allowed_tools

    def budget_spec(self) -> BudgetSpec:
        return BudgetSpec(
            max_iterations=self.max_steps,
            max_runtime_seconds=self.max_runtime_seconds,
            deadline_at=self.deadline_at,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        scope: str = "automation",
        deadline_at: datetime | None = None,
    ) -> ExecutionPolicy:
        profile_raw = payload.get("profile")
        profile: Mapping[str, Any] = profile_raw if isinstance(profile_raw, Mapping) else {}
        allowlist_configured = "allowed_tools" in payload or "allowed_tools" in profile
        allowed_raw = payload.get("allowed_tools", profile.get("allowed_tools", ()))
        blocked_raw = payload.get("blocked_tools", ())
        allowed = frozenset(str(item) for item in allowed_raw if str(item).strip())
        blocked = frozenset(str(item) for item in blocked_raw if str(item).strip())
        if not allowlist_configured and scope != "specialist":
            allowed = frozenset({TOOL_ALLOW_ALL})
        runtime = float(
            payload.get(
                "max_runtime_seconds",
                profile.get("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS),
            )
        )
        steps = int(payload.get("max_steps", profile.get("max_steps", DEFAULT_MAX_STEPS)))
        deadline = deadline_at
        if deadline is None:
            deadline_text = payload.get("deadline_at")
            if isinstance(deadline_text, str) and deadline_text.strip():
                parsed = datetime.fromisoformat(deadline_text)
                deadline = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        policy = cls(
            allowed_tools=allowed,
            blocked_tools=blocked,
            max_runtime_seconds=runtime,
            max_steps=steps,
            deadline_at=deadline,
            policy_scope=scope,
            notifications=bool(payload.get("notifications", profile.get("notifications", True))),
        )
        policy.validate()
        return policy


class PolicyEnforcer:
    def __init__(
        self,
        *,
        policy: ExecutionPolicy | None = None,
        specialist_profile_resolver: SpecialistProfileResolver | None = None,
    ) -> None:
        self.policy = policy
        self.specialist_profile_resolver = specialist_profile_resolver

    def policy_for(self, job: Job) -> ExecutionPolicy:
        if self.policy is not None:
            return self.policy
        scope = _scope_for_source(job.source)
        policy = ExecutionPolicy.from_payload(job.payload, scope=scope)
        if scope == "specialist":
            policy = self._bind_specialist_profile(job, policy)
        return policy

    def _bind_specialist_profile(self, job: Job, policy: ExecutionPolicy) -> ExecutionPolicy:
        if self.specialist_profile_resolver is None:
            return policy
        payload = job.payload or {}
        role = str(payload.get("role", "") or "").strip()
        if not role:
            return policy
        profile = self.specialist_profile_resolver(role)
        if profile is None:
            return policy
        profile_tools = frozenset(
            str(item) for item in profile.get("allowed_tools", ()) if str(item).strip()
        )
        profile_version = profile.get("tool_policy_version")
        payload_version = payload.get("tool_policy_version")
        if (
            profile_version is not None
            and payload_version is not None
            and str(payload_version) != str(profile_version)
        ):
            raise PolicyViolation(
                f"job {job.job_id} carries tool policy version {payload_version} but "
                f"persisted profile for {role!r} is at version {profile_version}"
            )
        requested = policy.allowed_tools
        if not requested:
            log.info(
                "job %s: specialist %r allowlist resolved from persisted profile (%d tools)",
                job.job_id,
                role,
                len(profile_tools),
            )
            return replace(policy, allowed_tools=profile_tools)
        escalation = requested - profile_tools
        if escalation:
            raise PolicyViolation(
                f"job {job.job_id} requests tools outside the persisted {role!r} profile: "
                f"{sorted(escalation)}"
            )
        return policy

    def verify_startable(self, job: Job, *, now: datetime | None = None) -> ExecutionPolicy:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        policy = self.policy_for(job)
        if policy.deadline_at is not None and policy.deadline_at <= current:
            raise PolicyViolation(
                f"job {job.job_id} deadline passed at {policy.deadline_at.isoformat()}"
            )
        return policy


def _scope_for_source(source: str) -> str:
    if source in POLICY_SCOPES:
        return source
    return "automation"


def deadline_from_delay(delay_seconds: float, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    return current + timedelta(seconds=delay_seconds)
