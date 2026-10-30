from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eurus.browser.backend import ElementInfo, NavigationOutcome
from eurus.browser.integration import ensure_browser_tools
from eurus.browser.manager import BrowserManager, BrowserSettings
from eurus.browser.session import (
    UnavailableBrowserSession,
    browser_available,
    build_browser_session,
    guarded_url,
)
from eurus.browser.tools import browser_tools
from eurus.errors import ToolError
from eurus.tools.base import Tool, ToolContext, ToolRegistry

BROWSER_TOOL_NAMES = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_select_option",
    "browser_hover",
    "browser_scroll",
    "browser_wait_for",
    "browser_back",
    "browser_tabs",
    "browser_handle_dialog",
    "browser_screenshot",
    "browser_upload",
    "browser_download",
    "browser_extract",
    "browser_read",
    "browser_close",
)


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, max_output=20000, timeout=30.0, run_id="run-test")


def _element(
    ref: str, name: str, role: str = "button", tag: str = "button", **extra
) -> ElementInfo:
    return ElementInfo(
        ref=ref,
        index=int(ref[1:]) - 1,
        role=role,
        name=name,
        tag=tag,
        signature=f"{role}|{tag}|{name}",
        **extra,
    )


class FakePage:
    def __init__(self) -> None:
        self.url_value = "about:blank"
        self.title_value = ""
        self.elements: list[ElementInfo] = []
        self.clicked: list[int] = []
        self.filled: list[tuple[int, str]] = []
        self.keys: list[str] = []
        self.selected: list[tuple[int, list[str]]] = []
        self.hovered: list[int] = []
        self.scrolled: list[tuple[int, int]] = []
        self.uploaded: list[tuple[int, list[Path]]] = []
        self.read_text_value = "element text"
        self.evaluate_value: dict[str, object] = {}
        self.screenshot_data = b"png-bytes"
        self.closed = False
        self.dialog_policy = "dismiss"
        self.last_dialog = None
        self.goto_outcome = NavigationOutcome(url="", status=200)

    async def url(self) -> str:
        return self.url_value

    async def title(self) -> str:
        return self.title_value

    async def goto(self, url: str, timeout: float) -> NavigationOutcome:
        self.url_value = url
        self.goto_outcome = NavigationOutcome(url=url, status=200, redirected=True)
        return self.goto_outcome

    async def text_content(self, max_chars: int) -> str:
        return "page body text " * 10

    async def collect_elements(self, max_elements: int) -> list[ElementInfo]:
        return self.elements[:max_elements]

    async def click(self, index: int, timeout: float) -> None:
        self.clicked.append(index)

    async def fill(self, index: int, text: str, timeout: float) -> None:
        self.filled.append((index, text))

    async def press_key(self, key: str) -> None:
        self.keys.append(key)

    async def select_options(self, index: int, values: list[str], timeout: float) -> list[str]:
        self.selected.append((index, values))
        return values

    async def hover(self, index: int, timeout: float) -> None:
        self.hovered.append(index)

    async def scroll(self, x: int, y: int) -> None:
        self.scrolled.append((x, y))

    async def screenshot_bytes(self, width: int, height: int) -> bytes:
        return self.screenshot_data

    async def upload(self, index: int, files: list[Path], timeout: float) -> None:
        self.uploaded.append((index, files))

    async def evaluate(self, expression: str) -> object:
        return self.evaluate_value.get(expression, 0)

    async def wait_load(self, timeout: float) -> None:
        return None

    async def wait_network_idle(self, timeout: float, idle_seconds: float = 0.5) -> bool:
        return True

    async def content_bytes(self, max_bytes: int) -> bytes:
        return b"<html></html>"[:max_bytes]

    async def read_text(self, index: int, max_chars: int) -> str:
        return self.read_text_value[:max_chars]

    async def extract_table(self, index: int) -> list[list[str]]:
        return [["head"], ["cell"]]

    async def read_by_selector(self, selector: str) -> str:
        return f"content of {selector}"

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.page = FakePage()
        self.started = 0
        self.stopped = 0
        self.opened = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    @property
    def connected(self) -> bool:
        return self.started > self.stopped

    def owns_process(self) -> bool:
        return True

    async def open_page(self) -> FakePage:
        self.opened += 1
        return self.page

    async def status(self) -> dict:
        return {"backend": self.name, "connected": self.connected}


def _manager(tmp_path: Path, backend: FakeBackend | None = None) -> BrowserManager:
    settings = BrowserSettings(enabled=True, backend="cdp", allowed_domains=("example.com",))
    manager = BrowserManager(settings, workspace=tmp_path, backend=backend or FakeBackend())
    return manager


def _registry(
    tmp_path: Path, backend: FakeBackend | None = None
) -> tuple[ToolRegistry, FakeBackend]:
    manager = _manager(tmp_path, backend)
    registry = ToolRegistry()
    for tool in browser_tools(manager):
        registry.register(tool)
    getattr(manager.backend, "page", None)
    return registry, manager.backend  # type: ignore[return-value]


def _snapshot(registry: ToolRegistry, tmp_path: Path) -> None:
    asyncio.run(_tool(registry, "browser_snapshot").handler({}, _context(tmp_path)))


def _tool(registry: ToolRegistry, name: str) -> Tool:
    tool = registry.get(name)
    assert tool is not None
    return tool


def test_all_eighteen_tools_present_with_schemas(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    for name in BROWSER_TOOL_NAMES:
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.description
        schema = tool.parameters
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        for required in schema.get("required", []):
            assert required in schema["properties"]


def test_interaction_tools_share_page_and_require_approval(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    for name in BROWSER_TOOL_NAMES:
        tool = registry.get(name)
        assert tool is not None
        assert tool.parallel_safe is False, name
    for name in (
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_select_option",
        "browser_handle_dialog",
        "browser_screenshot",
        "browser_upload",
    ):
        tool = registry.get(name)
        assert tool is not None
        assert tool.mutates is True
        assert tool.requires_approval is True
    for name in ("browser_navigate", "browser_snapshot", "browser_scroll", "browser_extract"):
        tool = registry.get(name)
        assert tool is not None
        assert tool.requires_approval is False


def test_navigate_returns_snapshot_with_refs(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.title_value = "Example"
    backend.page.elements = [
        _element("e1", "Dashboard", role="heading", tag="h1", level=1),
        _element("e2", "Home", role="link", tag="a", href="https://example.com/home"),
    ]
    registry, _ = _registry(tmp_path, backend)
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_navigate").handler(
                {"url": "https://example.com/"}, _context(tmp_path)
            )
        )
    )
    assert result["status"] == 200
    assert result["title"] == "Example"
    assert '[e1] heading1 "Dashboard"' in result["snapshot"]
    assert "[e2]" in result["snapshot"]
    assert "untrusted" in result["warning"]


def test_navigate_rejects_offpolicy_domain(tmp_path: Path) -> None:
    registry, backend = _registry(tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(
            _tool(registry, "browser_navigate").handler(
                {"url": "http://10.0.0.1/panel"}, _context(tmp_path)
            )
        )
    with pytest.raises(ToolError):
        asyncio.run(
            _tool(registry, "browser_navigate").handler(
                {"url": "file:///etc/passwd"}, _context(tmp_path)
            )
        )
    assert backend.opened == 0


def test_click_uses_element_reference(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "Create project"), _element("e2", "Submit")]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_click").handler(
                {"element": "e1", "description": "the create button"}, _context(tmp_path)
            )
        )
    )
    assert result["clicked"] == "click button 'Create project' [e1]"
    assert backend.page.clicked == [0]


def test_click_rejects_invalid_and_stale_references(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "Only button")]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    with pytest.raises(ToolError, match="invalid element reference"):
        asyncio.run(
            _tool(registry, "browser_click").handler({"element": "button1"}, _context(tmp_path))
        )
    with pytest.raises(ToolError, match="e9 is unknown"):
        asyncio.run(_tool(registry, "browser_click").handler({"element": "e9"}, _context(tmp_path)))
    backend.page.elements = [_element("e1", "Different button")]
    with pytest.raises(ToolError, match="stale"):
        asyncio.run(_tool(registry, "browser_click").handler({"element": "e1"}, _context(tmp_path)))


def test_type_resolves_secret_placeholders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EURUS_SECRET_API_KEY", "sk-super-secret")
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "API key", role="textbox", tag="input")]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_type").handler(
                {"element": "e1", "text": "{{secret:API_KEY}}"}, _context(tmp_path)
            )
        )
    )
    assert result["value"] == "••••••"
    assert backend.page.filled == [(0, "sk-super-secret")]
    snapshot = asyncio.run(_tool(registry, "browser_snapshot").handler({}, _context(tmp_path)))
    assert "sk-super-secret" not in snapshot


def test_type_rejects_missing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EURUS_SECRET_NOPE", raising=False)
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "Field", role="textbox", tag="input")]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    with pytest.raises(ToolError, match="EURUS_SECRET_NOPE"):
        asyncio.run(
            _tool(registry, "browser_type").handler(
                {"element": "e1", "text": "{{secret:NOPE}}"}, _context(tmp_path)
            )
        )


def test_press_key_select_and_hover(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [
        _element("e1", "Choose", role="combobox", tag="select", options=("Alpha", "Beta")),
        _element("e2", "Menu", role="button", tag="button"),
    ]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    asyncio.run(_tool(registry, "browser_press_key").handler({"key": "Enter"}, _context(tmp_path)))
    assert backend.page.keys == ["Enter"]
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_select_option").handler(
                {"element": "e1", "values": ["Beta"]}, _context(tmp_path)
            )
        )
    )
    assert result["selected"] == ["Beta"]
    asyncio.run(_tool(registry, "browser_hover").handler({"element": "e2"}, _context(tmp_path)))
    assert backend.page.hovered == [1]
    with pytest.raises(ToolError, match="not a dropdown"):
        asyncio.run(
            _tool(registry, "browser_select_option").handler(
                {"element": "e2", "values": ["x"]}, _context(tmp_path)
            )
        )


def test_scroll_wait_and_back(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "x")]
    registry, _ = _registry(tmp_path, backend)
    asyncio.run(
        _tool(registry, "browser_scroll").handler({"direction": "down"}, _context(tmp_path))
    )
    assert backend.page.scrolled == [(0, 600)]
    with pytest.raises(ToolError, match="direction"):
        asyncio.run(
            _tool(registry, "browser_scroll").handler({"direction": "sideways"}, _context(tmp_path))
        )
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_wait_for").handler(
                {"condition": "time", "seconds": 0.5}, _context(tmp_path)
            )
        )
    )
    assert result["waited"] == 0.5
    asyncio.run(_tool(registry, "browser_back").handler({}, _context(tmp_path)))
    assert backend.page.url_value == "about:blank"


def test_tabs_list_and_close(tmp_path: Path) -> None:
    backend = FakeBackend()
    registry, _ = _registry(tmp_path, backend)
    result = json.loads(
        asyncio.run(_tool(registry, "browser_tabs").handler({}, _context(tmp_path)))
    )
    assert result["tabs"][0]["active"] is True
    with pytest.raises(ToolError, match="last tab"):
        asyncio.run(
            _tool(registry, "browser_tabs").handler(
                {"action": "close", "index": 0}, _context(tmp_path)
            )
        )


def test_screenshot_confined_to_workspace(tmp_path: Path) -> None:
    backend = FakeBackend()
    registry, _ = _registry(tmp_path, backend)
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_screenshot").handler(
                {"path": "shots/page.png"}, _context(tmp_path)
            )
        )
    )
    assert result["screenshot"].startswith(str(tmp_path))
    assert (tmp_path / "shots" / "page.png").read_bytes() == b"png-bytes"
    with pytest.raises(ToolError):
        asyncio.run(
            _tool(registry, "browser_screenshot").handler(
                {"path": str(tmp_path.parent / "escape.png")}, _context(tmp_path)
            )
        )
    with pytest.raises(ToolError, match=".png or .jpg"):
        asyncio.run(
            _tool(registry, "browser_screenshot").handler(
                {"path": "shots/page.exe"}, _context(tmp_path)
            )
        )


def test_upload_requires_existing_workspace_files(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [_element("e1", "Upload", input_type="file", tag="input")]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    source = tmp_path / "data.txt"
    source.write_text("payload")
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_upload").handler(
                {"element": "e1", "files": ["data.txt"]}, _context(tmp_path)
            )
        )
    )
    assert result["files"] == [str(source)]
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(ToolError, match="inside the workspace"):
        asyncio.run(
            _tool(registry, "browser_upload").handler(
                {"element": "e1", "files": [str(outside)]}, _context(tmp_path)
            )
        )
    with pytest.raises(ToolError, match="does not exist"):
        asyncio.run(
            _tool(registry, "browser_upload").handler(
                {"element": "e1", "files": ["missing.txt"]}, _context(tmp_path)
            )
        )


def test_extract_fields_and_table(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.page.elements = [
        _element("e1", "Price", role="textbox", tag="input"),
        _element("e2", "Rates", role="table", tag="table"),
    ]
    registry, _ = _registry(tmp_path, backend)
    _snapshot(registry, tmp_path)
    result = json.loads(
        asyncio.run(
            _tool(registry, "browser_extract").handler(
                {
                    "fields": {
                        "price": {"element": "e1"},
                        "title": {"selector": "h1"},
                    },
                    "table": {"element": "e2"},
                },
                _context(tmp_path),
            )
        )
    )
    assert result["price"] == "element text"
    assert result["title"] == "content of h1"
    assert result["table"] == [["head"], ["cell"]]
    assert "untrusted" in result["warning"]


def test_read_keeps_legacy_selector_interface(tmp_path: Path) -> None:
    backend = FakeBackend()
    registry, _ = _registry(tmp_path, backend)
    text = asyncio.run(
        _tool(registry, "browser_read").handler({"selector": "#main"}, _context(tmp_path))
    )
    assert text == "content of #main"
    with pytest.raises(ToolError):
        asyncio.run(_tool(registry, "browser_read").handler({"selector": ""}, _context(tmp_path)))


def test_close_shuts_down_run_session(tmp_path: Path) -> None:
    registry, backend = _registry(tmp_path)
    asyncio.run(
        _tool(registry, "browser_navigate").handler(
            {"url": "https://example.com/"}, _context(tmp_path)
        )
    )
    asyncio.run(_tool(registry, "browser_close").handler({}, _context(tmp_path)))
    assert backend.page.closed is True


def test_ensure_browser_tools_idempotent(tmp_path: Path) -> None:
    registry = ToolRegistry()
    settings = BrowserSettings(enabled=True, backend="cdp")
    first = ensure_browser_tools(registry, settings, workspace=tmp_path)
    assert len(first) == len(BROWSER_TOOL_NAMES)
    second = ensure_browser_tools(registry, settings, workspace=tmp_path)
    assert second == []
    assert len(registry) == len(BROWSER_TOOL_NAMES)


def test_ensure_browser_tools_disabled_registers_nothing(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ensure_browser_tools(registry, BrowserSettings(enabled=False), workspace=tmp_path)
    assert len(registry) == 0
    ensure_browser_tools(registry, None, workspace=tmp_path)
    assert len(registry) == 0


def test_default_registry_registers_browser_tools(tmp_path: Path) -> None:
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
            backend="cdp",
            navigation_timeout_seconds=10.0,
            interaction_timeout_seconds=5.0,
            startup_timeout_seconds=10.0,
            max_page_bytes=1_000_000,
            max_screenshot_width=800,
            max_screenshot_height=600,
            max_snapshot_tokens=2000,
            allowed_domains=(),
            blocked_domains=(),
            cdp_endpoint="",
            chromium_executable="",
            profile_dir="",
            headless=True,
            download_dir="",
            max_download_mb=50,
            dns_cache_ttl_seconds=60.0,
            max_sessions=3,
            session_idle_ttl_seconds=900.0,
        ),
    )
    for name in BROWSER_TOOL_NAMES:
        assert name in on, name


def test_build_agent_attaches_browser_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eurus.agent.factory as factory

    config = SimpleNamespace(
        auxiliary=SimpleNamespace(model=""),
        network=SimpleNamespace(timeout_seconds=15, local_only=False),
        browser=SimpleNamespace(
            enabled=True,
            backend="cdp",
            cdp_endpoint="",
            chromium_executable="",
            profile_dir="",
            headless=True,
            max_page_bytes=2_000_000,
            navigation_timeout_seconds=10.0,
            interaction_timeout_seconds=5.0,
            startup_timeout_seconds=10.0,
            max_screenshot_width=1280,
            max_screenshot_height=800,
            max_snapshot_tokens=3000,
            allowed_domains=(),
            blocked_domains=(),
            download_dir="",
            max_download_mb=50,
            dns_cache_ttl_seconds=60.0,
            max_sessions=3,
            session_idle_ttl_seconds=900.0,
        ),
        permissions=SimpleNamespace(workspace=str(tmp_path)),
        tools=SimpleNamespace(max_output_chars=20000, timeout_seconds=30, max_parallel_tools=2),
        sandbox=SimpleNamespace(
            backend="host",
            runtime="docker",
            image="x",
            required=False,
            network=False,
            memory_mb=256,
            cpu_limit=1.0,
            pids_limit=32,
        ),
        learning=SimpleNamespace(mode="off", lessons_in_prompt=3, max_lessons=10, skills=False),
        agent=SimpleNamespace(
            system_prompt="p",
            iteration_budget=4,
            context_tokens=100000,
        ),
        verification=SimpleNamespace(enabled=False, model_judge=False),
        planning=SimpleNamespace(model_planning=False, max_steps=3),
        memory=SimpleNamespace(
            enabled=False, max_memories=10, retention_days=30, max_content_chars=500
        ),
    )
    registry = ToolRegistry()
    agent = factory.build_agent(
        config,  # type: ignore[arg-type]
        SimpleNamespace(),
        registry=registry,
        workspace=tmp_path,
        shell=False,
    )
    assert "browser_navigate" in registry
    assert "browser_snapshot" in registry
    assert "browser_close" in registry
    assert agent is not None


def test_unavailable_session_message() -> None:
    session = UnavailableBrowserSession()
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(session.click("#x"))
    assert "unavailable" in str(excinfo.value)
    assert "web_fetch" in str(excinfo.value)


def test_browser_available_reflects_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert browser_available() is False
    assert isinstance(build_browser_session(), UnavailableBrowserSession)


def test_guarded_url_blocks_dangerous_schemes() -> None:
    for url in (
        "file:///etc/passwd",
        "chrome://settings",
        "about:blank",
        "javascript:alert(1)",
        "data:text/html,hello",
        "http://localhost/",
        "http://169.254.169.254/",
        "ftp://example.com/file",
    ):
        with pytest.raises(ToolError):
            guarded_url(url)
    assert guarded_url("https://example.com/x") == "https://example.com/x"
