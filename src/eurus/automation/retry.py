from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eurus.runtime.recovery import FailureKind, classify_failure_kind, is_retryable_failure
from eurus.storage.jobs import Job

DEFAULT_RETRY_POLICY_NAME = "default"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 3600.0
    jitter_fraction: float = 0.25
    max_retry_duration_seconds: float = 86400.0

    def validate(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if self.backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds must be positive")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must not be below backoff_base_seconds")
        if not 0.0 <= self.jitter_fraction < 1.0:
            raise ValueError("jitter_fraction must be between 0 and 1")
        if self.max_retry_duration_seconds <= 0:
            raise ValueError("max_retry_duration_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay_seconds: float
    reason: str
    failure_kind: FailureKind


class RetryManager:
    def __init__(
        self,
        *,
        default_policy: RetryPolicy | None = None,
        policies: Mapping[str, RetryPolicy] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.default_policy = default_policy or RetryPolicy()
        self.default_policy.validate()
        self.policies: dict[str, RetryPolicy] = {}
        for name, policy in (policies or {}).items():
            policy.validate()
            self.policies[name] = policy
        self.rng = rng or random.Random()

    def policy_for(self, job: Job) -> RetryPolicy:
        return self.policies.get(job.source, self.policies.get("default", self.default_policy))

    def decide(
        self, job: Job, error: BaseException | str, *, now: datetime | None = None
    ) -> RetryDecision:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        error_text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        kind = classify_failure_kind(error=error_text)
        policy = self.policy_for(job)
        if not is_retryable_failure(kind):
            return RetryDecision(False, 0.0, f"failure_kind_{kind.value}_not_retryable", kind)
        attempt_cap = min(policy.max_attempts, job.max_attempts)
        if job.attempts >= attempt_cap:
            return RetryDecision(False, 0.0, "retry_exhausted", kind)
        created = _parse_time(job.created_at)
        if created is not None:
            age = current - created
            if age.total_seconds() >= policy.max_retry_duration_seconds:
                return RetryDecision(False, 0.0, "retry_duration_exceeded", kind)
        delay = float(self.backoff_delay(policy, job.attempts))
        return RetryDecision(True, delay, f"retryable_{kind.value}", kind)

    def backoff_delay(self, policy: RetryPolicy, attempts: int) -> float:
        capped_attempts = max(1, attempts)
        raw = policy.backoff_base_seconds * (2 ** (capped_attempts - 1))
        delay = min(raw, policy.backoff_max_seconds)
        if policy.jitter_fraction > 0:
            spread = delay * policy.jitter_fraction
            delay = delay - spread + self.rng.random() * spread * 2
        return float(max(0.0, min(delay, policy.backoff_max_seconds)))


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def retry_deadline(policy: RetryPolicy, *, from_time: datetime) -> datetime:
    return from_time + timedelta(seconds=policy.max_retry_duration_seconds)
