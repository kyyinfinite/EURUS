from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence

from eurus.errors import AllProvidersFailedError, EurusError, ProviderError
from eurus.models import Message, ModelRef
from eurus.providers.base import ChatRequest, ChatResponse, Provider, StreamEvent, ToolSpec
from eurus.providers.streams import closing_stream
from eurus.providers.transport import HttpTransport

DEFAULT_COOLDOWN_SECONDS = 30.0
MAX_COOLDOWN_SECONDS = 300.0


class Router:
    """Provider chain with per-provider cooldown.

    When a provider fails, it is skipped for a cooldown window (extended by
    the server's Retry-After) while healthy alternatives serve the request. If
    every provider is cooling down, the chain is still served - ordered by the
    earliest cooldown expiry - so availability never drops below the plain
    chain behaviour.
    """

    def __init__(
        self,
        providers: Mapping[str, Provider],
        chain: Sequence[ModelRef],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        transport: HttpTransport | None = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = MAX_COOLDOWN_SECONDS,
    ) -> None:
        if not chain:
            raise ValueError("router needs at least one model")
        if cooldown_seconds < 0 or max_cooldown_seconds < 0:
            raise ValueError("cooldowns cannot be negative")
        self.providers = dict(providers)
        self.chain = tuple(chain)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.last_used: ModelRef | None = None
        self._transport = transport
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self._cooldown_until: dict[str, float] = {}

    def penalize_provider(self, name: str, *, retry_after: float | None) -> None:
        """Put a provider into cooldown after a failure."""
        duration = self.cooldown_seconds
        if retry_after is not None:
            duration = max(duration, min(retry_after, self.max_cooldown_seconds))
        self._cooldown_until[name] = time.monotonic() + min(duration, self.max_cooldown_seconds)

    def expire_cooldown(self, name: str) -> None:
        """Drop the cooldown for a provider (testing/operational hook)."""
        self._cooldown_until.pop(name, None)

    def provider_cooldowns(self) -> dict[str, float]:
        """Remaining cooldown seconds per provider, for observability."""
        now = time.monotonic()
        remaining = {
            name: until - now for name, until in self._cooldown_until.items() if until > now
        }
        stale = set(self._cooldown_until) - set(remaining)
        for name in stale:
            del self._cooldown_until[name]
        return remaining

    def _candidates(self) -> list[ModelRef]:
        cooling = self.provider_cooldowns()
        if not cooling:
            return list(self.chain)
        active = [ref for ref in self.chain if ref.provider not in cooling]
        if active:
            return active
        return sorted(self.chain, key=lambda ref: cooling.get(ref.provider, 0.0))

    def _request(
        self,
        ref: ModelRef,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        temperature: float | None,
        max_tokens: int | None,
    ) -> ChatRequest:
        return ChatRequest(
            model=ref.model,
            messages=tuple(messages),
            tools=tuple(tools),
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
        )

    @staticmethod
    def _failure(failures: Sequence[tuple[str, ProviderError]]) -> EurusError:
        if len(failures) == 1:
            return failures[0][1]
        return AllProvidersFailedError(failures)

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        failures: list[tuple[str, ProviderError]] = []
        for ref in self._candidates():
            request = self._request(ref, messages, tools, temperature, max_tokens)
            try:
                response = await self.providers[ref.provider].chat(request)
            except ProviderError as exc:
                failures.append((str(ref), exc))
                self.penalize_provider(ref.provider, retry_after=exc.retry_after)
                continue
            self._cooldown_until.pop(ref.provider, None)
            self.last_used = ref
            return response
        raise self._failure(failures)

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        failures: list[tuple[str, ProviderError]] = []
        for ref in self._candidates():
            request = self._request(ref, messages, tools, temperature, max_tokens)
            started = False
            try:
                async with closing_stream(self.providers[ref.provider].stream(request)) as events:
                    async for event in events:
                        if not started:
                            started = True
                            self.last_used = ref
                        yield event
                self._cooldown_until.pop(ref.provider, None)
                self.last_used = ref
                return
            except ProviderError as exc:
                if started:
                    raise
                failures.append((str(ref), exc))
                self.penalize_provider(ref.provider, retry_after=exc.retry_after)
        raise self._failure(failures)

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
        if self._transport is not None:
            await self._transport.aclose()
