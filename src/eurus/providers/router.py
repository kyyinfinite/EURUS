from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass

from eurus.errors import (
    AllProvidersFailedError,
    AuthenticationError,
    EurusError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
)
from eurus.models import Message, ModelRef
from eurus.observability import classify_error
from eurus.providers.base import ChatRequest, ChatResponse, Provider, StreamEvent, ToolSpec
from eurus.providers.streams import closing_stream
from eurus.providers.transport import HttpTransport

DEFAULT_COOLDOWN_SECONDS = 30.0
MAX_COOLDOWN_SECONDS = 300.0
ATTEMPT_LOG_LIMIT = 32
SUMMARY_LIMIT = 200

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    provider: str
    model: str
    kind: str
    outcome: str
    error_category: str = ""
    error_summary: str = ""
    duration_ms: int = 0
    first_token_ms: int | None = None
    fallback: bool = False


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
        first_token_timeout: float | None = None,
    ) -> None:
        if not chain:
            raise ValueError("router needs at least one model")
        if cooldown_seconds < 0 or max_cooldown_seconds < 0:
            raise ValueError("cooldowns cannot be negative")
        if first_token_timeout is not None and first_token_timeout <= 0:
            raise ValueError("first_token_timeout must be positive")
        self.providers = dict(providers)
        self.chain = tuple(chain)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.last_used: ModelRef | None = None
        self._transport = transport
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.first_token_timeout = first_token_timeout
        self._cooldown_until: dict[str, float] = {}
        self._attempts: deque[AttemptRecord] = deque(maxlen=ATTEMPT_LOG_LIMIT)

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

    def attempt_log(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._attempts)

    def _candidates(self) -> list[ModelRef]:
        cooling = self.provider_cooldowns()
        if not cooling:
            return list(self.chain)
        active = [ref for ref in self.chain if ref.provider not in cooling]
        if active:
            return active
        return sorted(self.chain, key=lambda ref: cooling.get(ref.provider, 0.0))

    def _record(
        self,
        ref: ModelRef,
        kind: str,
        outcome: str,
        error: ProviderError | None = None,
        *,
        started_at: float | None = None,
        first_token_ms: int | None = None,
        fallback: bool = False,
    ) -> None:
        duration = int((time.monotonic() - started_at) * 1000) if started_at is not None else 0
        self._attempts.append(
            AttemptRecord(
                provider=ref.provider,
                model=ref.model,
                kind=kind,
                outcome=outcome,
                error_category=classify_error(error) if error is not None else "",
                error_summary=str(error)[:SUMMARY_LIMIT] if error is not None else "",
                duration_ms=duration,
                first_token_ms=first_token_ms,
                fallback=fallback,
            )
        )

    def _note_failure(
        self,
        ref: ModelRef,
        kind: str,
        exc: ProviderError,
        *,
        started_at: float | None = None,
        first_token_ms: int | None = None,
        fallback: bool = False,
    ) -> None:
        if not isinstance(exc, InvalidRequestError):
            self.penalize_provider(ref.provider, retry_after=exc.retry_after)
        log.warning(
            "provider candidate failed: provider=%s model=%s kind=%s category=%s cooldown=%s",
            ref.provider,
            ref.model,
            kind,
            classify_error(exc),
            not isinstance(exc, InvalidRequestError),
        )
        self._record(
            ref,
            kind,
            "failed",
            exc,
            started_at=started_at,
            first_token_ms=first_token_ms,
            fallback=fallback,
        )

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
        auth_failed: dict[str, AuthenticationError] = {}
        fallback = False
        for ref in self._candidates():
            if ref.provider in auth_failed:
                failures.append((str(ref), auth_failed[ref.provider]))
                self._record(ref, "chat", "skipped", auth_failed[ref.provider], fallback=fallback)
                continue
            request = self._request(ref, messages, tools, temperature, max_tokens)
            started_at = time.monotonic()
            try:
                response = await self.providers[ref.provider].chat(request)
            except ProviderError as exc:
                if isinstance(exc, AuthenticationError):
                    auth_failed.setdefault(ref.provider, exc)
                failures.append((str(ref), exc))
                self._note_failure(ref, "chat", exc, started_at=started_at, fallback=fallback)
                fallback = True
                continue
            self._cooldown_until.pop(ref.provider, None)
            self.last_used = ref
            self._record(ref, "chat", "success", started_at=started_at, fallback=fallback)
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
        auth_failed: dict[str, AuthenticationError] = {}
        fallback = False
        for ref in self._candidates():
            if ref.provider in auth_failed:
                failures.append((str(ref), auth_failed[ref.provider]))
                self._record(ref, "stream", "skipped", auth_failed[ref.provider], fallback=fallback)
                continue
            request = self._request(ref, messages, tools, temperature, max_tokens)
            started_at = time.monotonic()
            first_token_ms: int | None = None
            started = False
            try:
                async with closing_stream(self.providers[ref.provider].stream(request)) as events:
                    iterator = events.__aiter__()
                    if self.first_token_timeout is not None:
                        try:
                            event = await asyncio.wait_for(
                                iterator.__anext__(), self.first_token_timeout
                            )
                        except TimeoutError as exc:
                            raise ProviderUnavailableError(
                                f"{ref.provider}: no first token within "
                                f"{self.first_token_timeout:g}s",
                                provider=ref.provider,
                                retryable=True,
                            ) from exc
                        except StopAsyncIteration as exc:
                            raise ProviderUnavailableError(
                                f"{ref.provider}: stream ended without events",
                                provider=ref.provider,
                                retryable=True,
                            ) from exc
                        started = True
                        first_token_ms = int((time.monotonic() - started_at) * 1000)
                        self.last_used = ref
                        yield event
                    async for event in iterator:
                        if not started:
                            started = True
                            first_token_ms = int((time.monotonic() - started_at) * 1000)
                            self.last_used = ref
                        yield event
                self._cooldown_until.pop(ref.provider, None)
                self.last_used = ref
                self._record(
                    ref,
                    "stream",
                    "success",
                    started_at=started_at,
                    first_token_ms=first_token_ms,
                    fallback=fallback,
                )
                return
            except ProviderError as exc:
                if started:
                    raise
                if isinstance(exc, AuthenticationError):
                    auth_failed.setdefault(ref.provider, exc)
                failures.append((str(ref), exc))
                self._note_failure(
                    ref,
                    "stream",
                    exc,
                    started_at=started_at,
                    first_token_ms=first_token_ms,
                    fallback=fallback,
                )
                fallback = True
        raise self._failure(failures)

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
        if self._transport is not None:
            await self._transport.aclose()
