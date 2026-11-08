from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from eurus.errors import ToolError
from eurus.execution.envelope import NetworkProfile
from eurus.network.policy import NetworkPolicyError, PolicySettings, build_policy_engine
from eurus.research.errors import BrowserFallbackError
from eurus.research.fetch import ContentFetcher
from eurus.research.settings import ResearchSettings

FAKE_ADDRESSES = {
    "example.com": ["93.184.216.34"],
    "redirector.example.com": ["93.184.216.35"],
    "evil.test": ["127.0.0.1"],
    "meta.test": ["169.254.169.254"],
}


def fake_resolver(host: str, port: int) -> list[str]:
    if host in FAKE_ADDRESSES:
        return FAKE_ADDRESSES[host]
    raise NetworkPolicyError(f"cannot resolve host: {host}")


def make_policy():
    return build_policy_engine(
        PolicySettings(profile=NetworkProfile.WEB_READ, requests_per_minute=600),
        resolver=fake_resolver,
    )


def install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def mocked_client(**kwargs: Any) -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(
            timeout=kwargs.get("timeout", 15.0),
            follow_redirects=False,
            headers=kwargs.get("headers"),
            transport=transport,
        )

    monkeypatch.setattr("eurus.research.fetch.public_http_client", mocked_client)
    return seen


def plain_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "example.com" and request.url.path == "/report":
        return httpx.Response(200, text=LONG_PAGE, headers={"content-type": "text/html"})
    if request.url.host == "example.com" and request.url.path == "/thin":
        thin = "<html><body>ok</body></html>"
        return httpx.Response(200, text=thin, headers={"content-type": "text/html"})
    if request.url.host == "example.com" and request.url.path == "/redirect":
        return httpx.Response(301, headers={"location": "https://example.com/report"})
    if request.url.host == "example.com" and request.url.path == "/loop":
        return httpx.Response(302, headers={"location": "https://example.com/loop"})
    if request.url.host == "example.com" and request.url.path == "/nolocation":
        return httpx.Response(301)
    if request.url.host == "example.com" and request.url.path == "/big":
        return httpx.Response(200, content=b"x" * 3_000_000, headers={"content-type": "text/html"})
    if request.url.host == "example.com" and request.url.path == "/zip":
        return httpx.Response(200, content=b"zip", headers={"content-type": "application/zip"})
    if request.url.host == "example.com" and request.url.path == "/json":
        return httpx.Response(200, json={"key": "value"})
    if request.url.host == "example.com" and request.url.path == "/gone":
        return httpx.Response(404)
    return httpx.Response(500)


PARAGRAPH = "<p>Solid state batteries reached 400 Wh/kg.</p>"
LONG_PAGE = "<html><head><title>Report</title></head><body>" + (PARAGRAPH * 40) + "</body></html>"


def make_fetcher(policy: Any = None, browser: Any = None) -> ContentFetcher:
    return ContentFetcher(
        settings=ResearchSettings(max_fetch_bytes=2_000_000, browser_min_text_chars=200),
        policy=policy,
        browser_manager=browser,
    )


async def test_fetch_policy_path_extracts_html(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    result = await fetcher.fetch("https://example.com/report")
    assert result.page.content_type == "html"
    assert "Solid state batteries" in result.page.text
    assert result.via_browser is False


async def test_fetch_legacy_path_extracts_html(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    monkeypatch.setattr(
        "eurus.research.fetch.avalidate_public_url",
        lambda url: asyncio.sleep(0, result="https://example.com/report"),
    )
    fetcher = make_fetcher()
    result = await fetcher.fetch("https://example.com/report")
    assert "Solid state batteries" in result.page.text


async def test_fetch_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    result = await fetcher.fetch("https://example.com/redirect")
    assert result.page.text


async def test_fetch_rejects_redirect_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    with pytest.raises(ToolError, match="too many redirects"):
        await fetcher.fetch("https://example.com/loop")


async def test_fetch_rejects_redirect_without_location(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher()
    monkeypatch.setattr(
        "eurus.research.fetch.avalidate_public_url",
        lambda url: asyncio.sleep(0, result="https://example.com/nolocation"),
    )
    with pytest.raises(ToolError, match="redirect without a location"):
        await fetcher.fetch("https://example.com/nolocation")
    assert seen is not None


async def test_fetch_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    with pytest.raises(ToolError, match="MB download limit"):
        await fetcher.fetch("https://example.com/big")


async def test_fetch_rejects_unsupported_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    with pytest.raises(ToolError, match="unsupported webpage content type"):
        await fetcher.fetch("https://example.com/zip")


async def test_fetch_supports_json_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    result = await fetcher.fetch("https://example.com/json")
    assert result.page.content_type == "json"


async def test_fetch_reports_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    fetcher = make_fetcher(policy=make_policy())
    with pytest.raises(ToolError, match="website request failed"):
        await fetcher.fetch("https://example.com/gone")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "https://evil.test/steal",
        "https://meta.test/credentials",
        "http://localhost/x",
        "file:///etc/passwd",
    ],
)
async def test_fetch_rejects_ssrf_targets(url: str) -> None:
    fetcher = make_fetcher(policy=make_policy())
    with pytest.raises(ToolError):
        await fetcher.fetch(url)


class FakeUrlCheck:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeGuard:
    def __init__(self, reject: BaseException | None = None) -> None:
        self.reject = reject
        self.checked: list[str] = []

    async def check(self, url: str) -> FakeUrlCheck:
        self.checked.append(url)
        if self.reject is not None:
            raise self.reject
        return FakeUrlCheck(url)


class FakeLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeSession:
    def __init__(self, text: str, title: str = "Rendered") -> None:
        self.text = text
        self.title = title
        self.lock = FakeLock()
        self.navigated: list[str] = []

    async def navigate(self, url: str) -> dict[str, Any]:
        self.navigated.append(url)
        return {"url": url, "status": "loaded", "title": self.title}

    async def page_text(self, max_chars: int) -> str:
        return self.text[:max_chars]


class FakeManager:
    def __init__(self, session: FakeSession, guard: FakeGuard | None = None) -> None:
        self.session = session
        self.url_guard = guard or FakeGuard()

    async def session_for(self, run_key: str, workspace: Any = None) -> FakeSession:
        assert run_key == "research-engine"
        return self.session


async def test_fetch_uses_browser_for_thin_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    long_text = "Rich browser-rendered content. " * 20
    manager = FakeManager(FakeSession(long_text, title="Rendered"))
    fetcher = make_fetcher(policy=make_policy(), browser=manager)
    result = await fetcher.fetch("https://example.com/thin")
    assert result.via_browser is True
    assert result.page.title == "Rendered"
    assert "Rich browser-rendered content" in result.page.text


async def test_fetch_keeps_http_result_when_browser_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(monkeypatch, plain_handler)

    class BrokenManager(FakeManager):
        async def session_for(self, run_key: str, workspace: Any = None) -> FakeSession:
            raise BrowserFallbackError("no browser backend installed")

    fetcher = make_fetcher(policy=make_policy(), browser=BrokenManager(FakeSession("")))
    result = await fetcher.fetch("https://example.com/thin")
    assert result.via_browser is False
    assert result.browser_error
    assert result.page.text.strip() == "ok"


async def test_fetch_browser_rejected_by_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    from eurus.browser.security import BrowserPolicyError

    guard = FakeGuard(reject=BrowserPolicyError("blocked domain"))
    session = FakeSession("x" * 500)
    fetcher = make_fetcher(policy=make_policy(), browser=FakeManager(session, guard=guard))
    result = await fetcher.fetch("https://example.com/thin")
    assert result.via_browser is False
    assert "blocked domain" in result.browser_error


async def test_fetch_browser_not_used_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    manager = FakeManager(FakeSession("Rich browser-rendered content. " * 20))
    fetcher = ContentFetcher(
        settings=ResearchSettings(browser_fallback=False, browser_min_text_chars=200),
        policy=make_policy(),
        browser_manager=manager,
    )
    result = await fetcher.fetch("https://example.com/thin")
    assert result.via_browser is False
    assert manager.session.navigated == []


async def test_fetch_browser_not_used_for_rich_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mock_transport(monkeypatch, plain_handler)
    manager = FakeManager(FakeSession("x"))
    fetcher = make_fetcher(policy=make_policy(), browser=manager)
    result = await fetcher.fetch("https://example.com/report")
    assert result.via_browser is False
    assert manager.session.navigated == []
