"""C3 regression tests: the router must cool down failing providers.

Before the fix, Router tried the chain head on every request even when that
provider had just failed, so a rate-limited provider added retry latency to
every single call while healthy alternatives were available.
"""

from __future__ import annotations

from typing import Any

import pytest

from eurus.errors import EurusError, ProviderError
from eurus.models import Message, ModelRef
from eurus.providers.base import ChatResponse
from eurus.providers.router import Router


class FakeProvider:
    def __init__(self, *, fail: bool = False, retry_after: float | None = None) -> None:
        self.fail = fail
        self.retry_after = retry_after
        self.chat_calls = 0

    async def chat(self, request: Any) -> ChatResponse:
        self.chat_calls += 1
        if self.fail:
            raise ProviderError("boom", provider="p", retryable=False, retry_after=self.retry_after)
        return ChatResponse(message=Message.assistant("ok"), model="m", provider="p")

    async def stream(self, request: Any):
        from eurus.providers.base import StreamEvent

        yield StreamEvent.text_delta("ok")

    async def aclose(self) -> None:
        return None


def _two_provider_router(primary: FakeProvider, secondary: FakeProvider) -> Router:
    return Router(
        {"a": primary, "b": secondary},
        [ModelRef.parse("a/primary"), ModelRef.parse("b/secondary")],
    )


@pytest.mark.asyncio
async def test_failing_provider_is_skipped_while_cooling_down():
    primary = FakeProvider(fail=True)
    secondary = FakeProvider()
    router = _two_provider_router(primary, secondary)

    first = await router.chat([Message.user("hi")])
    assert first.message.content == "ok"
    assert primary.chat_calls == 1
    assert secondary.chat_calls == 1

    second = await router.chat([Message.user("again")])
    assert second.message.content == "ok"
    assert primary.chat_calls == 1, "cooling provider must not be retried immediately"
    assert secondary.chat_calls == 2


@pytest.mark.asyncio
async def test_cooldown_expires_and_provider_is_tried_again():
    primary = FakeProvider(fail=True)
    secondary = FakeProvider()
    router = _two_provider_router(primary, secondary)
    await router.chat([Message.user("hi")])
    assert primary.chat_calls == 1

    router.expire_cooldown("a")
    primary.fail = False

    await router.chat([Message.user("hi")])
    assert primary.chat_calls == 2, "a recovered provider must be preferred again"


@pytest.mark.asyncio
async def test_success_clears_cooldown_of_serving_provider():
    import time

    primary = FakeProvider()
    secondary = FakeProvider()
    router = _two_provider_router(primary, secondary)
    # Simulate both providers cooling down, with b expiring sooner.
    router._cooldown_until["a"] = time.monotonic() + 300.0
    router._cooldown_until["b"] = time.monotonic() + 1.0

    response = await router.chat([Message.user("hi")])
    assert response.message.content == "ok"
    remaining = router.provider_cooldowns()
    assert "b" not in remaining, "the succeeding provider must have its cooldown cleared"
    assert "a" in remaining, "an untouched cooling provider must stay cooling"


@pytest.mark.asyncio
async def test_all_providers_cooling_reports_remaining_time():
    primary = FakeProvider(fail=True, retry_after=60.0)
    secondary = FakeProvider(fail=True, retry_after=1.0)
    router = _two_provider_router(primary, secondary)
    with pytest.raises(EurusError):
        await router.chat([Message.user("hi")])

    cooldowns = router.provider_cooldowns()
    assert set(cooldowns) == {"a", "b"}
    assert cooldowns["b"] <= cooldowns["a"], "retry-after must extend the cooldown"


@pytest.mark.asyncio
async def test_stream_failure_penalizes_provider():
    class FailingStreamPrimary(FakeProvider):
        async def stream(self, request: Any):
            raise ProviderError("stream boom", provider="a", retryable=False)
            yield  # pragma: no cover

    primary = FailingStreamPrimary(fail=True)
    secondary = FakeProvider()
    router = _two_provider_router(primary, secondary)

    chunks: list[str] = []
    async for event in router.stream([Message.user("hi")]):
        chunks.append(event.text)
    assert chunks == ["ok"], "the healthy provider must serve the stream"
    cooldowns = router.provider_cooldowns()
    assert "a" in cooldowns, "the failed provider must be cooling down"
    assert "b" not in cooldowns, "the successful provider must not be cooling down"
