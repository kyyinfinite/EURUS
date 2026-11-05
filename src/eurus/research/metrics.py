from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from eurus.research.errors import BudgetExceededError, DeadlineExceededError


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_rounds: int = 3
    max_queries: int = 12
    max_sources: int = 16
    max_fetches: int = 10
    max_response_bytes: int = 20_000_000
    deadline_seconds: float = 240.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= 10:
            raise ValueError("max_rounds must be between 1 and 10")
        if not 1 <= self.max_queries <= 60:
            raise ValueError("max_queries must be between 1 and 60")
        if not 1 <= self.max_sources <= 60:
            raise ValueError("max_sources must be between 1 and 60")
        if not 1 <= self.max_fetches <= 60:
            raise ValueError("max_fetches must be between 1 and 60")
        if not 1 <= self.max_response_bytes <= 200_000_000:
            raise ValueError("max_response_bytes is out of range")
        if not 10.0 <= self.deadline_seconds <= 3600.0:
            raise ValueError("deadline_seconds must be between 10 and 3600")


class BudgetTracker:
    def __init__(
        self,
        limits: BudgetLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        start_round: int = 0,
        used_queries: int = 0,
        used_sources: int = 0,
        used_fetches: int = 0,
        used_bytes: int = 0,
    ) -> None:
        self.limits = limits
        self._clock = clock
        self.started = self._clock()
        self.rounds = start_round
        self.queries = used_queries
        self.sources = used_sources
        self.fetches = used_fetches
        self.response_bytes = used_bytes

    def elapsed(self) -> float:
        return self._clock() - self.started

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.deadline_seconds - self.elapsed())

    def ensure_time(self) -> None:
        if self.elapsed() >= self.limits.deadline_seconds:
            raise DeadlineExceededError(
                f"research exceeded its {self.limits.deadline_seconds:g}s deadline"
            )

    def begin_round(self) -> int:
        self.rounds += 1
        if self.rounds > self.limits.max_rounds:
            raise BudgetExceededError(
                f"research exceeded the round limit of {self.limits.max_rounds}"
            )
        return self.rounds

    def count_query(self, amount: int = 1) -> None:
        if self.queries + amount > self.limits.max_queries:
            raise BudgetExceededError(
                f"research exceeded the query limit of {self.limits.max_queries}"
            )
        self.queries += amount

    def count_source(self, amount: int = 1) -> None:
        if self.sources + amount > self.limits.max_sources:
            raise BudgetExceededError(
                f"research exceeded the source limit of {self.limits.max_sources}"
            )
        self.sources += amount

    def count_fetch(self, amount: int = 1) -> None:
        if self.fetches + amount > self.limits.max_fetches:
            raise BudgetExceededError(
                f"research exceeded the fetch limit of {self.limits.max_fetches}"
            )
        self.fetches += amount

    def count_bytes(self, amount: int) -> None:
        if self.response_bytes + amount > self.limits.max_response_bytes:
            raise BudgetExceededError(
                f"research exceeded the response byte limit of {self.limits.max_response_bytes}"
            )
        self.response_bytes += amount

    def exhausted_reason(self) -> str:
        if self.rounds > self.limits.max_rounds:
            return "max rounds reached"
        if self.queries >= self.limits.max_queries:
            return "query budget exhausted"
        if self.fetches >= self.limits.max_fetches:
            return "fetch budget exhausted"
        if self.sources >= self.limits.max_sources:
            return "source budget exhausted"
        if self.elapsed() >= self.limits.deadline_seconds:
            return "deadline reached"
        return ""

    def snapshot(self) -> dict[str, float | int]:
        return {
            "rounds": self.rounds,
            "max_rounds": self.limits.max_rounds,
            "queries": self.queries,
            "max_queries": self.limits.max_queries,
            "sources": self.sources,
            "max_sources": self.limits.max_sources,
            "fetches": self.fetches,
            "max_fetches": self.limits.max_fetches,
            "response_bytes": self.response_bytes,
            "max_response_bytes": self.limits.max_response_bytes,
            "elapsed_seconds": round(self.elapsed(), 2),
            "deadline_seconds": self.limits.deadline_seconds,
        }


@dataclass
class ResearchMetrics:
    job_id: str = ""
    mode: str = ""
    provider_chain: tuple[str, ...] = ()
    searches: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    browser_fetches: int = 0
    browser_failures: int = 0
    fetch_failed: int = 0
    deduped: int = 0
    events: list[dict[str, object]] = field(default_factory=list)
    error_category: str = ""
    started_at: float = field(default_factory=time.monotonic)

    def note_event(self, event_type: str, **payload: object) -> None:
        self.events.append({"event": event_type, **payload})

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def snapshot(
        self, *, qa_status: str = "", budget: dict[str, float | int] | None = None
    ) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "mode": self.mode,
            "provider_chain": list(self.provider_chain),
            "searches": self.searches,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "browser_fetches": self.browser_fetches,
            "browser_failures": self.browser_failures,
            "fetch_failed": self.fetch_failed,
            "deduped": self.deduped,
            "elapsed_seconds": round(self.elapsed(), 2),
            "error_category": self.error_category,
            "qa_status": qa_status,
            "budget": budget or {},
        }
