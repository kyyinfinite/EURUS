from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from eurus.browser.backend import ElementInfo
from eurus.browser.manager import (
    BrowserManager,
    BrowserSettings,
    build_backend,
)
from eurus.browser.snapshot import build_elements
from eurus.errors import ToolError


class FakePage:
    def __init__(self, url: str = "https://example.com/") -> None:
        self.url_value = url
        self.title_value = "Example"
        self.closed = False
        self.elements: list[ElementInfo] = []
        self.kind = "page"

    async def url(self) -> str:
        return self.url_value

    async def title(self) -> str:
        return self.title_value

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.opened = 0
        self.stopped = 0
        self.pages: list[FakePage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped += 1

    @property
    def connected(self) -> bool:
        return True

    def owns_process(self) -> bool:
        return True

    async def open_page(self) -> FakePage:
        self.opened += 1
        page = FakePage()
        self.pages.append(page)
        return page

    async def status(self) -> dict:
        return {"backend": self.name}


def _settings(**overrides) -> BrowserSettings:
    base = dict(
        enabled=True,
        backend="cdp",
        max_sessions=3,
        session_idle_ttl=900.0,
    )
    base.update(overrides)
    return BrowserSettings(**base)


def test_session_for_reuses_same_run() -> None:
    async def run() -> None:
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=FakeBackend())
        first = await manager.session_for("run-1")
        second = await manager.session_for("run-1")
        assert first is second
        assert manager.backend.opened == 1
        other = await manager.session_for("run-2")
        assert other is not first
        assert manager.backend.opened == 2
        await manager.close_all()

    asyncio.run(run())


def test_session_lock_serializes_operations() -> None:
    async def run() -> None:
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=FakeBackend())
        session = await manager.session_for("run-1")
        order: list[str] = []

        async def worker(tag: str) -> None:
            async with session.lock:
                order.append(f"{tag}-start")
                await asyncio.sleep(0.05)
                order.append(f"{tag}-end")

        await asyncio.gather(worker("a"), worker("b"))
        assert order == ["a-start", "a-end", "b-start", "b-end"]
        await manager.close_all()

    asyncio.run(run())


def test_idle_sessions_are_evicted() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(
            _settings(session_idle_ttl=30.0), workspace=Path("/tmp"), backend=backend
        )
        first = await manager.session_for("run-1")
        page = first.pages[0]
        first.last_used = time.monotonic() - 60.0
        await manager.session_for("run-2")
        assert page.closed is True
        assert "run-1" not in manager._sessions
        await manager.close_all()

    asyncio.run(run())


def test_max_sessions_evicts_least_recently_used() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(max_sessions=2), workspace=Path("/tmp"), backend=backend)
        one = await manager.session_for("run-1")
        page_one = one.pages[0]
        await manager.session_for("run-2")
        await asyncio.sleep(0.01)
        await manager.session_for("run-3")
        assert "run-1" not in manager._sessions
        assert "run-2" in manager._sessions
        assert "run-3" in manager._sessions
        assert page_one.closed is True
        await manager.close_all()

    asyncio.run(run())


def test_close_run_and_close_all() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        session = await manager.session_for("run-1")
        page = session.pages[0]
        await manager.close_run("run-1")
        assert page.closed is True
        assert "run-1" not in manager._sessions
        await manager.session_for("run-2")
        await manager.close_all()
        assert backend.stopped == 1
        with pytest.raises(ToolError, match="closed"):
            await manager.session_for("run-3")

    asyncio.run(run())


def test_stop_keeps_manager_reusable() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        await manager.session_for("run-1")
        await manager.stop()
        assert backend.stopped == 1
        again = await manager.session_for("run-2")
        assert again is not None
        await manager.close_all()

    asyncio.run(run())


def test_popup_routed_to_owner_session() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        session = await manager.session_for("run-1")
        popup = FakePage("https://example.com/popup")
        backend.pages.append(popup)
        manager.route_page_opened(popup, session.pages[0])
        await asyncio.sleep(0.05)
        assert popup in session.pages
        assert session.active is popup
        await manager.close_all()

    asyncio.run(run())


def test_unowned_popup_goes_to_most_recent_session() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        session = await manager.session_for("run-1")
        stray = FakePage("https://example.com/stray")
        manager.route_page_opened(stray, None)
        await asyncio.sleep(0.05)
        assert stray in session.pages
        await manager.close_all()

    asyncio.run(run())


def test_popup_without_sessions_is_closed() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        stray = FakePage("https://example.com/stray")
        manager.route_page_opened(stray, None)
        await asyncio.sleep(0.05)
        assert stray.closed is True

    asyncio.run(run())


def test_closed_page_removes_from_session() -> None:
    async def run() -> None:
        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        session = await manager.session_for("run-1")
        page = session.pages[0]
        manager.route_page_closed(page)
        await asyncio.sleep(0)
        assert page not in session.pages
        assert "run-1" not in manager._sessions

    asyncio.run(run())


def test_secrets_redacted_from_text() -> None:
    async def run() -> None:
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=FakeBackend())
        session = await manager.session_for("run-1")
        session.remember_secret("hunter2")
        assert session.redact("password is hunter2 ok") == "password is •••••• ok"

    asyncio.run(run())


def test_resolve_secret_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("EURUS_SECRET_TOKEN", "abc123")
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=FakeBackend())
        session = await manager.session_for("run-1")
        assert session.resolve_secret("value={{secret:TOKEN}}") == "value=abc123"
        with pytest.raises(ToolError, match="EURUS_SECRET_MISSING"):
            session.resolve_secret("{{secret:MISSING}}")

    asyncio.run(run())


def test_element_cap_used_for_collection() -> None:
    async def run() -> None:
        seen_sizes: list[int] = []

        class CountingPage(FakePage):
            async def collect(self, cap: int) -> list:
                seen_sizes.append(cap)
                return []

        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=FakeBackend())
        session = await manager.session_for("run-1")
        assert session.manager.settings.element_cap == 400
        assert seen_sizes == []

    asyncio.run(run())


def test_snapshot_remember_and_stale_detection() -> None:
    async def run() -> None:
        class SnapshotPage(FakePage):
            def __init__(self) -> None:
                super().__init__()
                self.raw: list[dict] = [
                    {"role": "button", "name": "One", "tag": "button"},
                    {"role": "link", "name": "Two", "tag": "a"},
                ]

            async def collect_elements(self, cap: int) -> list[ElementInfo]:
                return build_elements(self.raw[:cap])

        backend = FakeBackend()
        manager = BrowserManager(_settings(), workspace=Path("/tmp"), backend=backend)
        session = await manager.session_for("run-1")
        page = session.pages[0]
        page.__class__ = SnapshotPage
        page.raw = [
            {"role": "button", "name": "One", "tag": "button"},
            {"role": "link", "name": "Two", "tag": "a"},
        ]
        await session.snapshot()
        element = await session.resolve("e2")
        assert element.name == "Two"
        page.raw = [{"role": "button", "name": "One", "tag": "button"}]
        with pytest.raises(ToolError, match="stale"):
            await session.resolve("e2")

    asyncio.run(run())


def test_build_backend_selects_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(backend="playwright")
    backend = build_backend(settings)
    assert type(backend).__name__ == "PlaywrightBackend"
    cdp = build_backend(_settings(backend="cdp"))
    assert type(cdp).__name__ == "CdpBackend"
    remote = build_backend(_settings(backend="remote", cdp_endpoint="http://127.0.0.1:9222"))
    assert type(remote).__name__ == "CdpBackend"
    monkeypatch.setattr("eurus.browser.manager.playwright_available", lambda: False)
    with pytest.raises(ToolError, match="playwright"):
        build_backend(_settings(backend="playwright"))
    with pytest.raises(ToolError, match="unknown browser backend"):
        build_backend(_settings(backend="carrier-pigeon"))


def test_settings_from_config_mapping() -> None:
    from eurus.config import BrowserConfig

    config = BrowserConfig(enabled=True, backend="cdp", max_snapshot_tokens=1500)
    settings = BrowserSettings.from_config(config)
    assert settings.enabled is True
    assert settings.backend == "cdp"
    assert settings.max_snapshot_tokens == 1500
    assert settings.snapshot_budget_chars == 6000
