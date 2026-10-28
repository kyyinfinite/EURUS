from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eurus.browser.session import (
    BrowserPageResult,
    UnavailableBrowserSession,
    build_browser_session,
    guarded_url,
)
from eurus.browser.tools import browser_tools
from eurus.errors import ToolError
from eurus.tools.base import ToolContext


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, max_output=20000, timeout=30.0)


def _page(url: str = "https://example.com/page") -> BrowserPageResult:
    return BrowserPageResult(
        url=url,
        title="Example",
        text="page body text",
        links=("https://example.com/next",),
        metadata={"url": url, "warning": "untrusted"},
    )


class FakeSession:
    def __init__(self) -> None:
        self.navigated: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.closed = False

    async def navigate(self, url: str) -> BrowserPageResult:
        self.navigated.append(url)
        return _page(url)

    async def click(self, selector: str) -> BrowserPageResult:
        self.clicked.append(selector)
        return _page()

    async def type_text(self, selector: str, text: str) -> BrowserPageResult:
        self.typed.append((selector, text))
        return _page()

    async def extract(self, selector: str) -> str:
        return f"content of {selector}"

    async def screenshot(self, path: str, width: int, height: int) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fakepng")
        return str(target)

    async def close(self) -> None:
        self.closed = True


def _tools(session: FakeSession) -> dict:
    return {tool.name: tool for tool in browser_tools(session=session)}


def test_core_imports_survive_without_playwright(monkeypatch) -> None:
    import builtins

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    from eurus.browser.session import browser_available as check

    assert check() is False
    session = build_browser_session()
    assert isinstance(session, UnavailableBrowserSession)
    with pytest.raises(ToolError, match="unavailable"):
        asyncio.run(session.navigate("https://example.com"))


def test_guarded_url_blocks_dangerous_schemes() -> None:
    for url in (
        "file:///etc/passwd",
        "chrome://settings",
        "about:blank",
        "javascript:alert(1)",
        "data:text/html,hello",
        "http://localhost/",
        "http://169.254.169.254/",
    ):
        with pytest.raises(ToolError):
            guarded_url(url)
    assert guarded_url("https://example.com/x") == "https://example.com/x"


def test_browser_navigate_returns_untrusted_content_warning(tmp_path: Path) -> None:
    session = FakeSession()
    tools = _tools(session)
    result = asyncio.run(
        tools["browser_navigate"].handler({"url": "https://example.com"}, _context(tmp_path))
    )
    payload = json.loads(result)
    assert payload["title"] == "Example"
    assert "untrusted" in payload["metadata"]["warning"]
    assert session.navigated == ["https://example.com"]


def test_browser_navigate_rejects_private_targets(tmp_path: Path) -> None:
    session = FakeSession()
    tools = _tools(session)
    with pytest.raises(ToolError):
        asyncio.run(
            tools["browser_navigate"].handler({"url": "http://10.0.0.1/panel"}, _context(tmp_path))
        )
    assert session.navigated == []


def test_browser_click_and_type_are_bounded(tmp_path: Path) -> None:
    session = FakeSession()
    tools = _tools(session)
    asyncio.run(tools["browser_click"].handler({"selector": "#next"}, _context(tmp_path)))
    with pytest.raises(ToolError):
        asyncio.run(tools["browser_click"].handler({"selector": "x" * 400}, _context(tmp_path)))
    asyncio.run(
        tools["browser_type"].handler({"selector": "#q", "text": "query"}, _context(tmp_path))
    )
    assert session.clicked == ["#next"]
    assert session.typed == [("#q", "query")]


def test_browser_screenshot_confined_to_workspace(tmp_path: Path) -> None:
    session = FakeSession()
    tools = _tools(session)
    result = asyncio.run(
        tools["browser_screenshot"].handler({"path": "shots/page.png"}, _context(tmp_path))
    )
    payload = json.loads(result)
    assert payload["screenshot"].startswith(str(tmp_path))
    assert (tmp_path / "shots" / "page.png").exists()
    with pytest.raises(ToolError):
        asyncio.run(
            tools["browser_screenshot"].handler(
                {"path": str(tmp_path.parent / "escape.png")}, _context(tmp_path)
            )
        )
    with pytest.raises(ToolError):
        asyncio.run(
            tools["browser_screenshot"].handler({"path": "shots/page.exe"}, _context(tmp_path))
        )


def test_browser_screenshot_requires_approval(tmp_path: Path) -> None:
    tools = _tools(FakeSession())
    assert tools["browser_screenshot"].mutates is True
    assert tools["browser_screenshot"].requires_approval is True
    assert tools["browser_navigate"].mutates is False


def test_unavailable_session_gives_explicit_message() -> None:
    session = UnavailableBrowserSession()
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(session.click("#x"))
    assert "unavailable" in str(excinfo.value)
    assert "web_fetch" in str(excinfo.value)


def test_browser_tools_registered_only_when_enabled(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from eurus.tools.builtin import default_registry

    off = default_registry(
        include_capability_tools=True,
        browser_settings=SimpleNamespace(enabled=False),
    )
    assert "browser_navigate" not in off
    on = default_registry(
        include_capability_tools=True,
        browser_settings=SimpleNamespace(
            enabled=True,
            navigation_timeout_seconds=10.0,
            max_page_bytes=1_000_000,
            max_screenshot_width=800,
            max_screenshot_height=600,
        ),
    )
    for name in (
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_read",
        "browser_screenshot",
    ):
        assert name in on


def test_browser_click_and_type_require_approval_and_mutate() -> None:
    tools = browser_tools()
    by_name = {tool.name: tool for tool in tools}
    assert by_name["browser_click"].mutates is True
    assert by_name["browser_click"].requires_approval is True
    assert by_name["browser_type"].mutates is True
    assert by_name["browser_type"].requires_approval is True


def test_route_guard_resolves_url_off_the_event_loop() -> None:
    import asyncio

    from eurus.browser.session import PlaywrightBrowserSession

    class FakeRoute:
        class Request:
            def __init__(self, url: str) -> None:
                self.url = url

        def __init__(self, url: str) -> None:
            self.request = FakeRoute.Request(url)
            self.aborted = False
            self.continued = False

        async def abort(self) -> None:
            self.aborted = True

        async def continue_(self) -> None:
            self.continued = True

    session = PlaywrightBrowserSession()
    good = FakeRoute("https://example.com/page")
    bad = FakeRoute("http://127.0.0.1/admin")
    asyncio.run(session._guard_request(good))
    asyncio.run(session._guard_request(bad))
    assert good.continued is True and good.aborted is False
    assert bad.aborted is True and bad.continued is False
