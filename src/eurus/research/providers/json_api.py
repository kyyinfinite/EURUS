from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from eurus.research.errors import ProviderUnavailableError
from eurus.research.models import SearchHit
from eurus.research.providers.base import SearchProvider, clamp_limit

MAX_API_RESPONSE_BYTES = 1_000_000
API_TIMEOUT_SECONDS = 15.0
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ApiSpec:
    endpoint: str
    method: str
    query_param: str
    key_param: str
    key_header: str
    key_mode: str
    count_param: str
    count_scale: int = 1
    results_path: tuple[str, ...] = ()
    url_fields: tuple[str, ...] = ()
    title_fields: tuple[str, ...] = ()
    snippet_fields: tuple[str, ...] = ()
    date_fields: tuple[str, ...] = ()
    extra_params: tuple[tuple[str, str], ...] = ()


BRAVE_SPEC = ApiSpec(
    endpoint="https://api.search.brave.com/res/v1/web/search",
    method="GET",
    query_param="q",
    key_param="",
    key_header="X-Subscription-Token",
    key_mode="header",
    count_param="count",
    results_path=("web", "results"),
    url_fields=("url",),
    title_fields=("title",),
    snippet_fields=("description",),
    date_fields=("age",),
)

TAVILY_SPEC = ApiSpec(
    endpoint="https://api.tavily.com/search",
    method="POST",
    query_param="query",
    key_param="",
    key_header="Authorization",
    key_mode="header",
    count_param="max_results",
    results_path=("results",),
    url_fields=("url",),
    title_fields=("title",),
    snippet_fields=("content",),
    date_fields=("published_date",),
    extra_params=(("search_depth", "basic"),),
)

SERPAPI_SPEC = ApiSpec(
    endpoint="https://serpapi.com/search",
    method="GET",
    query_param="q",
    key_param="api_key",
    key_header="",
    key_mode="param",
    count_param="num",
    results_path=("organic_results",),
    url_fields=("link",),
    title_fields=("title",),
    snippet_fields=("snippet",),
    date_fields=("date",),
)

API_SPECS: dict[str, ApiSpec] = {
    "brave": BRAVE_SPEC,
    "tavily": TAVILY_SPEC,
    "serpapi": SERPAPI_SPEC,
}

API_KEY_ENV: dict[str, str] = {
    "brave": "BRAVE_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
}


class JsonApiSearchProvider(SearchProvider):
    def __init__(self, name: str, spec: ApiSpec, *, environ: dict[str, str] | None = None) -> None:
        self.name = name
        self.spec = spec
        self._environ = os.environ if environ is None else environ

    def _api_key(self) -> str:
        return self._environ.get(API_KEY_ENV.get(self.name, ""), "").strip()

    def key_env_name(self) -> str:
        return API_KEY_ENV.get(self.name, "")

    def available(self) -> bool:
        return bool(self._api_key())

    def _headers(self, key: str) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.spec.key_header and key:
            if self.name == "tavily":
                headers[self.spec.key_header] = f"Bearer {key}"
            else:
                headers[self.spec.key_header] = key
        return headers

    def _payload(self, query: str, limit: int, key: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            self.spec.query_param: query,
            self.spec.count_param: clamp_limit(limit) * self.spec.count_scale,
        }
        for name, value in self.spec.extra_params:
            body[name] = value
        if self.spec.key_param and key and self.spec.key_mode == "param":
            body[self.spec.key_param] = key
        return body

    def _extract_rows(self, payload: Any) -> list[dict[str, Any]]:
        node: Any = payload
        for step in self.spec.results_path:
            if not isinstance(node, dict):
                return []
            node = node.get(step)
        if isinstance(node, list):
            return [row for row in node if isinstance(row, dict)]
        return []

    def _first_field(self, row: dict[str, Any], fields: tuple[str, ...]) -> str:
        for field_name in fields:
            value = row.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _request(self, query: str, limit: int, key: str) -> httpx.Request:
        payload = self._payload(query, limit, key)
        if self.spec.method == "POST":
            params = None
            body = {name: value for name, value in payload.items() if name != self.spec.key_param}
            if self.spec.key_param and key and self.spec.key_mode == "param":
                body[self.spec.key_param] = key
            json_body: dict[str, Any] | None = body
        else:
            params = {name: str(value) for name, value in payload.items()}
            json_body = None
        return httpx.Request(
            self.spec.method,
            self.spec.endpoint,
            params=params,
            json=json_body,
            headers=self._headers(key),
        )

    def _send(self, request: httpx.Request) -> httpx.Response:
        with httpx.Client(timeout=API_TIMEOUT_SECONDS, follow_redirects=False) as client:
            return client.send(request)

    def search(self, query: str, limit: int) -> list[SearchHit]:
        key = self._api_key()
        if not key:
            raise ProviderUnavailableError(
                f"search provider '{self.name}' requires the {self.key_env_name()} "
                "environment variable",
                provider=self.name,
            )
        response = self._send(self._request(query, limit, key))
        if response.status_code in RETRYABLE_STATUS:
            raise ProviderUnavailableError(
                f"search provider '{self.name}' responded with {response.status_code}",
                provider=self.name,
            )
        if response.status_code >= 300:
            raise ProviderUnavailableError(
                f"search provider '{self.name}' responded with {response.status_code}",
                provider=self.name,
            )
        if len(response.content) > MAX_API_RESPONSE_BYTES:
            raise ProviderUnavailableError(
                f"search provider '{self.name}' returned an oversized response",
                provider=self.name,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                f"search provider '{self.name}' returned invalid JSON",
                provider=self.name,
            ) from exc
        hits: list[SearchHit] = []
        for row in self._extract_rows(payload):
            url = self._first_field(row, self.spec.url_fields)
            if not url:
                continue
            published = self._first_field(row, self.spec.date_fields)
            hits.append(
                SearchHit(
                    title=self._first_field(row, self.spec.title_fields) or url,
                    url=url,
                    snippet=self._first_field(row, self.spec.snippet_fields),
                    provider=self.name,
                    rank=len(hits) + 1,
                    published_at=published,
                )
            )
        return hits


def build_api_providers(environ: dict[str, str] | None = None) -> list[JsonApiSearchProvider]:
    return [JsonApiSearchProvider(name, spec, environ=environ) for name, spec in API_SPECS.items()]
