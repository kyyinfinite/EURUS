from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import httpx
import pytest

from eurus.research.errors import (
    ProviderUnavailableError,
    SearchTimeoutError,
    SearchUnavailableError,
)
from eurus.research.models import SearchHit
from eurus.research.providers.base import SearchProvider, clamp_limit
from eurus.research.providers.ddgs_provider import (
    MISSING_EXTRA_MESSAGE,
    DdgsSearchProvider,
)
from eurus.research.providers.json_api import (
    API_KEY_ENV,
    TAVILY_SPEC,
    JsonApiSearchProvider,
)
from eurus.research.providers.registry import ProviderRegistry
from eurus.research.settings import ResearchSettings


class FakeProvider(SearchProvider):
    def __init__(
        self,
        name: str,
        hits: list[SearchHit] | None = None,
        error: BaseException | None = None,
        available: bool = True,
    ) -> None:
        self.name = name
        self.calls = 0
        self._hits = hits or []
        self._error = error
        self._available = available

    def available(self) -> bool:
        return self._available

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._hits[: clamp_limit(limit)]


def make_registry(*providers: SearchProvider) -> ProviderRegistry:
    return ProviderRegistry(list(providers), settings=ResearchSettings())


def test_registry_returns_first_provider_hits() -> None:
    provider = FakeProvider("ddgs", [SearchHit("t", "https://a.com/1", "s", "ddgs", 1)])
    registry = make_registry(provider)
    hits = asyncio.run(registry.search("query", 5))
    assert [hit.url for hit in hits] == ["https://a.com/1"]


def test_registry_falls_back_to_next_provider() -> None:
    failing = FakeProvider("ddgs", error=RuntimeError("ddgs exploded"))
    backup = FakeProvider("brave", [SearchHit("t", "https://b.com/1", "s", "brave", 1)])
    registry = make_registry(failing, backup)
    hits = asyncio.run(registry.search("query", 5))
    assert [hit.provider for hit in hits] == ["brave"]
    health = registry.health("ddgs")
    assert health is not None and health.failures == 1
    assert registry.stats.errors == 1
    assert registry.stats.by_provider["brave"] == 1


def test_registry_records_timeout() -> None:
    provider = FakeProvider("ddgs", error=SearchTimeoutError("slow", provider="ddgs"))
    registry = make_registry(provider)
    with pytest.raises(SearchUnavailableError):
        asyncio.run(registry.search("query", 5))
    health = registry.health("ddgs")
    assert health is not None and health.timeouts == 1


def test_registry_raises_when_all_providers_fail() -> None:
    first = FakeProvider("ddgs", error=RuntimeError("boom"))
    second = FakeProvider("brave", error=ProviderUnavailableError("no key", provider="brave"))
    registry = make_registry(first, second)
    with pytest.raises(SearchUnavailableError) as excinfo:
        asyncio.run(registry.search("query", 5))
    assert excinfo.value.failures[0][0] == "ddgs"
    assert excinfo.value.failures[1][0] == "brave"


def test_registry_skips_unavailable_providers() -> None:
    missing = FakeProvider("ddgs", available=False)
    ready = FakeProvider("brave", [SearchHit("t", "https://b.com/1", "s", "brave", 1)])
    registry = make_registry(missing, ready)
    hits = asyncio.run(registry.search("query", 5))
    assert hits[0].provider == "brave"
    with pytest.raises(SearchUnavailableError) as excinfo:
        asyncio.run(make_registry(missing).search("query", 5))
    assert isinstance(excinfo.value.failures[0][1], ProviderUnavailableError)


def test_registry_cools_down_after_repeated_failures() -> None:
    flaky = FakeProvider("ddgs", error=RuntimeError("down"))
    ticks = iter(float(i) for i in range(400))

    def clock() -> float:
        return next(ticks, 999.0)

    registry = ProviderRegistry([flaky], settings=ResearchSettings(), clock=clock)
    for _ in range(5):
        with pytest.raises(SearchUnavailableError):
            asyncio.run(registry.search("q", 5))
    assert flaky.calls == 10
    with pytest.raises(SearchUnavailableError) as excinfo:
        asyncio.run(registry.search("q", 5))
    assert "cooling down" in str(excinfo.value.failures[0][1])
    assert flaky.calls == 10


def test_registry_rejects_duplicate_and_missing_providers() -> None:
    with pytest.raises(ValueError, match="duplicate provider name"):
        make_registry(FakeProvider("ddgs"), FakeProvider("ddgs"))
    with pytest.raises(ValueError, match="at least one provider"):
        ProviderRegistry([], settings=ResearchSettings())


def test_clamp_limit_bounds_results() -> None:
    assert clamp_limit(0) == 1
    assert clamp_limit(99) == 25


def test_ddgs_provider_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDdgs:
        def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
            assert query == "battery"
            assert max_results == 4
            return [
                {"title": "Doc", "href": "https://a.com/doc", "body": "useful"},
                {"url": "https://b.com/x"},
                {"title": "Empty"},
            ]

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = FakeDdgs
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)
    provider = DdgsSearchProvider()
    hits = provider.search("battery", 4)
    assert [hit.url for hit in hits] == ["https://a.com/doc", "https://b.com/x"]
    assert hits[0].snippet == "useful"
    assert hits[1].title == "https://b.com/x"


def test_ddgs_provider_reports_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ddgs", None)
    provider = DdgsSearchProvider()
    assert provider.available() is False
    with pytest.raises(ProviderUnavailableError, match="web search requires"):
        provider.search("battery", 5)
    assert provider.unavailable_reason() == MISSING_EXTRA_MESSAGE


def test_ddgs_provider_propagates_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenDdgs:
        def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
            raise RuntimeError("rate limited by upstream")

    fake_module = types.ModuleType("ddgs")
    fake_module.DDGS = BrokenDdgs
    monkeypatch.setitem(sys.modules, "ddgs", fake_module)
    with pytest.raises(RuntimeError, match="rate limited by upstream"):
        DdgsSearchProvider().search("battery", 5)


def test_json_api_provider_requires_key() -> None:
    provider = JsonApiSearchProvider("tavily", TAVILY_SPEC, environ={})
    assert provider.available() is False
    with pytest.raises(ProviderUnavailableError, match=API_KEY_ENV["tavily"]):
        provider.search("query", 5)


def test_json_api_provider_maps_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = JsonApiSearchProvider("tavily", TAVILY_SPEC, environ={"TAVILY_API_KEY": "k-1"})
    sent: list[httpx.Request] = []

    def fake_send(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "R1", "url": "https://r.com/1", "content": "text one"},
                    {"title": "No url", "content": "skip"},
                ]
            },
        )

    monkeypatch.setattr(provider, "_send", fake_send)
    hits = provider.search("battery", 5)
    assert [hit.url for hit in hits] == ["https://r.com/1"]
    assert hits[0].published_at == ""
    assert sent and sent[0].headers["Authorization"] == "Bearer k-1"


@pytest.mark.parametrize("status", [429, 503, 500, 302])
def test_json_api_provider_rejects_bad_statuses(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = JsonApiSearchProvider("tavily", TAVILY_SPEC, environ={"TAVILY_API_KEY": "k-1"})

    def fake_send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    monkeypatch.setattr(provider, "_send", fake_send)
    with pytest.raises(ProviderUnavailableError):
        provider.search("battery", 5)


def test_json_api_provider_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = JsonApiSearchProvider("tavily", TAVILY_SPEC, environ={"TAVILY_API_KEY": "k-1"})

    def fake_send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json")

    monkeypatch.setattr(provider, "_send", fake_send)
    with pytest.raises(ProviderUnavailableError, match="invalid JSON"):
        provider.search("battery", 5)
