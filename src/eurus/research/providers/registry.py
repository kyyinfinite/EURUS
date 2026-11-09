from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence

from eurus.research.errors import (
    ProviderUnavailableError,
    SearchTimeoutError,
    SearchUnavailableError,
)
from eurus.research.models import SearchHit
from eurus.research.providers.base import (
    ProviderFailure,
    ProviderHealth,
    ProviderStats,
    SearchProvider,
    clamp_limit,
)
from eurus.research.settings import ResearchSettings

DEFAULT_SETTINGS = ResearchSettings()


class ProviderRegistry:
    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        settings: ResearchSettings = DEFAULT_SETTINGS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("provider registry requires at least one provider")
        seen: set[str] = set()
        for provider in providers:
            if not provider.name:
                raise ValueError(f"provider {type(provider).__name__} has no name")
            if provider.name in seen:
                raise ValueError(f"duplicate provider name: {provider.name}")
            seen.add(provider.name)
        self._providers: list[SearchProvider] = list(providers)
        self.settings = settings
        self._clock = clock
        self._health: dict[str, ProviderHealth] = {
            provider.name: ProviderHealth(provider.name) for provider in providers
        }
        self.stats = ProviderStats()

    def names(self) -> list[str]:
        return [provider.name for provider in self._providers]

    def providers(self) -> list[SearchProvider]:
        return list(self._providers)

    def health(self, name: str) -> ProviderHealth | None:
        return self._health.get(name)

    def health_snapshot(self) -> dict[str, dict[str, int | str]]:
        return {
            name: {
                "consecutive_failures": item.consecutive_failures,
                "failures": item.failures,
                "successes": item.successes,
                "timeouts": item.timeouts,
                "last_error": item.last_error,
            }
            for name, item in self._health.items()
        }

    def _usable(self, provider: SearchProvider) -> bool:
        entry = self._health[provider.name]
        if not entry.cooled_down(self._clock()):
            return False
        return provider.available()

    def _eligible(self) -> list[SearchProvider]:
        return [provider for provider in self._providers if self._usable(provider)]

    def _record_failure(
        self, provider: SearchProvider, error: BaseException, failures: list[ProviderFailure]
    ) -> None:
        self._health[provider.name].note_failure(error, self._clock())
        self.stats.errors += 1
        failures.append(ProviderFailure(provider.name, error))

    def _attempt_sync(
        self,
        provider: SearchProvider,
        query: str,
        limit: int,
        failures: list[ProviderFailure],
    ) -> list[SearchHit] | None:
        attempts = self.settings.provider_retries + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if attempt:
                self.stats.retries += 1
            try:
                hits = provider.search(query, clamp_limit(limit))
            except BaseException as exc:
                last_error = exc
                if isinstance(exc, ProviderUnavailableError | SearchTimeoutError):
                    break
                if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
                    break
                continue
            else:
                self._health[provider.name].note_success()
                return hits
        assert last_error is not None
        self._record_failure(provider, last_error, failures)
        return None

    def _skip_failure(self, provider: SearchProvider) -> ProviderFailure:
        entry = self._health[provider.name]
        if entry.consecutive_failures:
            reason = "cooling down after repeated failures"
        else:
            reason = provider.unavailable_reason()
        return ProviderFailure(
            provider.name,
            ProviderUnavailableError(reason, provider=provider.name),
        )

    def search_sync(self, query: str, limit: int) -> list[SearchHit]:
        failures: list[ProviderFailure] = []
        failed_any = False
        for provider in self._providers:
            if not self._usable(provider):
                failures.append(self._skip_failure(provider))
                failed_any = True
                continue
            self.stats.note_request(provider.name)
            hits = self._attempt_sync(provider, query, limit, failures)
            if hits is not None:
                if failed_any:
                    self.stats.fallbacks += 1
                return hits
            failed_any = True
        raise SearchUnavailableError(
            "all search providers are unavailable",
            failures=[(item.provider, item.error) for item in failures],
        )

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        failures: list[ProviderFailure] = []
        failed_any = False
        for provider in self._providers:
            if not self._usable(provider):
                failures.append(self._skip_failure(provider))
                failed_any = True
                continue
            self.stats.note_request(provider.name)
            hits = await self._attempt_async(provider, query, limit, failures)
            if hits is not None:
                if failed_any:
                    self.stats.fallbacks += 1
                return hits
            failed_any = True
        raise SearchUnavailableError(
            "all search providers are unavailable",
            failures=[(item.provider, item.error) for item in failures],
        )

    async def _attempt_async(
        self,
        provider: SearchProvider,
        query: str,
        limit: int,
        failures: list[ProviderFailure],
    ) -> list[SearchHit] | None:
        attempts = self.settings.provider_retries + 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if attempt:
                self.stats.retries += 1
            try:
                hits = await asyncio.wait_for(
                    asyncio.to_thread(provider.search, query, clamp_limit(limit)),
                    timeout=self.settings.provider_timeout_seconds,
                )
            except TimeoutError:
                last_error = SearchTimeoutError(
                    f"search provider '{provider.name}' timed out after "
                    f"{self.settings.provider_timeout_seconds:g}s",
                    provider=provider.name,
                    timeout_seconds=self.settings.provider_timeout_seconds,
                )
                self._record_failure(provider, last_error, failures)
                return None
            except BaseException as exc:
                last_error = exc
                if isinstance(exc, ProviderUnavailableError | SearchTimeoutError):
                    break
                if isinstance(exc, asyncio.CancelledError):
                    raise
                continue
            else:
                self._health[provider.name].note_success()
                return hits
        assert last_error is not None
        self._record_failure(provider, last_error, failures)
        return None


def build_default_registry(
    provider_names: Sequence[str],
    *,
    settings: ResearchSettings = DEFAULT_SETTINGS,
    environ: dict[str, str] | None = None,
) -> ProviderRegistry:
    providers: list[SearchProvider] = []
    for name in provider_names:
        if name == "ddgs":
            from eurus.research.providers.ddgs_provider import DdgsSearchProvider

            providers.append(DdgsSearchProvider())
        elif name in {"brave", "tavily", "serpapi"}:
            from eurus.research.providers.json_api import API_SPECS, JsonApiSearchProvider

            providers.append(JsonApiSearchProvider(name, API_SPECS[name], environ=environ))
        else:
            raise ValueError(f"unknown search provider: {name}")
    if not providers:
        raise ValueError("provider registry requires at least one provider")
    return ProviderRegistry(providers, settings=settings)
