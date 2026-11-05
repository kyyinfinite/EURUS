from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from eurus.research.errors import SearchTimeoutError
from eurus.research.models import SearchHit

MAX_PROVIDER_RESULTS = 25

HEALTH_FAILURE_LIMIT = 5
HEALTH_COOLDOWN_SECONDS = 30.0


@dataclass
class ProviderHealth:
    provider: str
    consecutive_failures: int = 0
    last_error: str = ""
    last_failure_at: float = 0.0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0

    def note_success(self) -> None:
        self.successes += 1
        self.consecutive_failures = 0
        self.last_error = ""

    def note_failure(self, error: BaseException, now: float) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        self.last_error = f"{type(error).__name__}: {error}"
        self.last_failure_at = now
        if isinstance(error, SearchTimeoutError | TimeoutError):
            self.timeouts += 1

    def cooled_down(self, now: float) -> bool:
        if self.consecutive_failures < HEALTH_FAILURE_LIMIT:
            return True
        return now >= self.last_failure_at + HEALTH_COOLDOWN_SECONDS


class SearchProvider(ABC):
    name: str = ""

    @abstractmethod
    def search(self, query: str, limit: int) -> list[SearchHit]:
        raise NotImplementedError

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return "provider is not configured or its dependency is missing"


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    provider: str
    error: BaseException
    phase: str = "search"


@dataclass
class ProviderStats:
    requests: int = 0
    errors: int = 0
    retries: int = 0
    fallbacks: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)

    def note_request(self, provider: str) -> None:
        self.requests += 1
        self.by_provider[provider] = self.by_provider.get(provider, 0) + 1


def clamp_limit(limit: int) -> int:
    return max(1, min(MAX_PROVIDER_RESULTS, limit))
