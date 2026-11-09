from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from eurus.errors import ToolError
from eurus.network.policy import NetworkPolicyEngine, PolicyDecision
from eurus.research.errors import BrowserFallbackError, SecurityRejectionError
from eurus.research.extract import PageContent, extract_payload
from eurus.research.settings import ResearchSettings
from eurus.security.net_guard import (
    anormalize_redirect,
    avalidate_public_url,
    public_http_client,
)

USER_AGENT = "Eurus/1.0"
ACCEPT_HEADER = (
    "text/html,application/xhtml+xml,application/pdf,application/rss+xml,"
    "application/atom+xml,application/json,text/plain;q=0.9,*/*;q=0.5"
)
MAX_REDIRECT_HOPS = 6
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BROWSER_SESSION_KEY = "research-engine"
MAX_BROWSER_TEXT_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    content_type: str
    page: PageContent
    response_bytes: int
    via_browser: bool = False
    browser_error: str = ""

    @property
    def text(self) -> str:
        return self.page.text


class ContentFetcher:
    def __init__(
        self,
        *,
        settings: ResearchSettings,
        policy: NetworkPolicyEngine | None = None,
        browser_manager: Any | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.browser_manager = browser_manager

    def _host_of(self, url: str) -> str:
        return urlparse(url).hostname or ""

    def _byte_cap(self) -> int:
        cap = self.settings.max_fetch_bytes
        if self.policy is not None:
            cap = min(cap, self.policy.policy.max_response_bytes)
        return max(100_000, cap)

    def _timeout(self) -> float:
        return self.settings.fetch_timeout_seconds

    async def validate(self, url: str) -> str:
        if self.policy is not None:
            decision: PolicyDecision = await self.policy.acheck_url(url)
            return decision.url
        return await avalidate_public_url(url)

    async def _download(self, url: str) -> tuple[bytes, str, str]:
        if self.policy is None:
            return await self._download_legacy(url)
        return await self._download_policy(url)

    async def _download_legacy(self, url: str) -> tuple[bytes, str, str]:
        current = await avalidate_public_url(url)
        headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER}
        async with public_http_client(timeout=self._timeout(), headers=headers) as client:
            for _ in range(MAX_REDIRECT_HOPS):
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise ToolError("website returned a redirect without a location")
                            current = await anormalize_redirect(current, location)
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").lower()
                        payload = await self._collect(response)
                        encoding = response.encoding or "utf-8"
                        return payload, content_type, encoding
                except httpx.HTTPError as exc:
                    raise ToolError(f"website request failed: {type(exc).__name__}") from exc
        raise ToolError("too many website redirects")

    async def _download_policy(self, url: str) -> tuple[bytes, str, str]:
        assert self.policy is not None
        policy = self.policy
        decision = await policy.acheck_url(url)
        current = decision.url
        session = policy.redirect_session()
        byte_cap = self._byte_cap()
        headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER}
        async with public_http_client(
            timeout=self._timeout(),
            headers=headers,
            resolver=policy.address_resolver(),
            strict_public=not policy.policy.allow_private_addresses,
        ) as client:
            while True:
                policy.acquire(self._host_of(current))
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            current = session.follow(current, location or "")
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").lower()
                        payload = await self._collect(response, cap=byte_cap)
                        encoding = response.encoding or "utf-8"
                        return payload, content_type, encoding
                except httpx.HTTPError as exc:
                    raise ToolError(f"website request failed: {type(exc).__name__}") from exc

    async def _collect(self, response: httpx.Response, *, cap: int | None = None) -> bytes:
        limit = self._byte_cap() if cap is None else cap
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise ToolError(f"webpage exceeds the {limit // (1024 * 1024)} MB download limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _browser_text(self, url: str) -> tuple[str, str]:
        if self.browser_manager is None:
            raise BrowserFallbackError("no browser manager is available")
        from eurus.browser.security import BrowserPolicyError

        manager = self.browser_manager
        try:
            await manager.url_guard.check(url)
        except BrowserPolicyError as exc:
            raise SecurityRejectionError(str(exc)) from exc
        session = await manager.session_for(BROWSER_SESSION_KEY)
        async with session.lock:
            info = await session.navigate(url)
            text = await session.page_text(MAX_BROWSER_TEXT_CHARS)
        title = str(info.get("title") or "")
        return text, title

    def _needs_browser(self, page: PageContent) -> bool:
        if page.content_type != "html":
            return False
        if not self.settings.browser_fallback:
            return False
        if self.browser_manager is None:
            return False
        return len(page.text.strip()) < self.settings.browser_min_text_chars

    async def fetch(self, url: str, *, allow_browser: bool = True) -> FetchResult:
        validated = await self.validate(url)
        payload, content_type, encoding = await self._download(validated)
        page = extract_payload(payload, content_type, validated, encoding=encoding)
        if not self._needs_browser(page) or not allow_browser:
            return FetchResult(
                url=validated,
                content_type=content_type,
                page=page,
                response_bytes=len(payload),
            )
        browser_error = ""
        try:
            text, title = await self._browser_text(validated)
        except (BrowserFallbackError, SecurityRejectionError, ToolError) as exc:
            browser_error = f"{type(exc).__name__}: {exc}"
        else:
            if len(text.strip()) >= self.settings.browser_min_text_chars:
                return FetchResult(
                    url=validated,
                    content_type=content_type,
                    page=PageContent(
                        title=title or validated,
                        text=text,
                        date=page.date,
                        author=page.author,
                        content_type="html",
                    ),
                    response_bytes=len(payload),
                    via_browser=True,
                )
            browser_error = "browser page text was still too short"
        return FetchResult(
            url=validated,
            content_type=content_type,
            page=page,
            response_bytes=len(payload),
            browser_error=browser_error,
        )
