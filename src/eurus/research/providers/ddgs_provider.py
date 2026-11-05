from __future__ import annotations

from typing import Any

from eurus.research.errors import ProviderUnavailableError
from eurus.research.models import SearchHit
from eurus.research.providers.base import SearchProvider, clamp_limit

MISSING_EXTRA_MESSAGE = (
    "web search requires the optional web extra; install with `pip install 'eurus-agent[web]'`"
)


class DdgsSearchProvider(SearchProvider):
    name = "ddgs"

    def _client_class(self) -> Any:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise ProviderUnavailableError(MISSING_EXTRA_MESSAGE, provider=self.name) from exc
        return DDGS

    def available(self) -> bool:
        try:
            self._client_class()
        except ProviderUnavailableError:
            return False
        return True

    def unavailable_reason(self) -> str:
        return MISSING_EXTRA_MESSAGE

    def search(self, query: str, limit: int) -> list[SearchHit]:
        bounded = clamp_limit(limit)
        ddgs_cls = self._client_class()
        results = ddgs_cls().text(query, max_results=bounded)
        hits: list[SearchHit] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            href = str(result.get("href") or result.get("url") or "")
            if not href:
                continue
            hits.append(
                SearchHit(
                    title=str(result.get("title") or href),
                    url=href,
                    snippet=str(result.get("body") or result.get("snippet") or ""),
                    provider=self.name,
                    rank=len(hits) + 1,
                )
            )
        return hits
