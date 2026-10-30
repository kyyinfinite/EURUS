from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from eurus.browser.manager import BrowserManager, BrowserSettings

PLAYWRIGHT_CHROMIUM = Path.home() / ".cache" / "ms-playwright"
SYSTEM_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or ""

pytestmark = pytest.mark.browser_integration

pytest.importorskip("playwright")


PAGE = """<!doctype html><html><head><title>Integration</title></head><body>
<h1>Integration Page</h1>
<a href="/second" id="next">Go second</a>
<button id="alert-btn" onclick="alert('integration dialog')">Show alert</button>
<button id="count-btn"
 onclick="window.count=(window.count||0)+1;window.countBtn=this">Count me</button>
<script>
setInterval(() => {
  if (window.countBtn) window.countBtn.textContent = 'Counted ' + window.count;
}, 50);
</script>
<form id="f"
 onsubmit="event.preventDefault();document.getElementById('out').textContent='got:'+this.q.value">
<label for="q">Query</label>
<input type="text" name="q" id="q" placeholder="Type here"/>
<select id="opts" aria-label="Choose one">
<option value="a">Alpha</option><option value="b">Beta</option>
</select>
<input type="password" id="pw" aria-label="Password"/>
<button type="submit" id="go">Send form</button>
</form>
<div id="out"></div>
<table id="t" aria-label="Metrics">
<tr><th>Key</th><th>Val</th></tr>
<tr><td>cpu</td><td>12</td></tr>
<tr><td>mem</td><td>48</td></tr>
</table>
<input type="file" id="file1" aria-label="Attach file"/>
<a href="/popup" target="_blank" id="pop">Open popup</a>
</body></html>"""

SECOND = (
    "<html><head><title>Second</title></head><body><h1>Second page</h1>"
    "<a href='/' id='home'>Back home</a></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (SECOND if self.path == "/second" else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _settings(backend: str, **overrides) -> BrowserSettings:
    base = dict(
        enabled=True,
        backend=backend,
        headless=True,
        allowed_domains=("127.0.0.1",),
        navigation_timeout=15.0,
        interaction_timeout=8.0,
        startup_timeout=30.0,
        profile_dir="",
        download_dir="",
        max_download_mb=20,
    )
    base.update(overrides)
    return BrowserSettings(**base)


def chromium_available() -> bool:
    if SYSTEM_CHROMIUM:
        return True
    if PLAYWRIGHT_CHROMIUM.is_dir():
        for child in PLAYWRIGHT_CHROMIUM.iterdir():
            if (
                child.name.startswith("chromium-")
                and (child / "chrome-linux64" / "chrome").is_file()
            ):
                return True
    return False


def chromium_binary() -> str:
    if SYSTEM_CHROMIUM:
        return SYSTEM_CHROMIUM
    for child in sorted(PLAYWRIGHT_CHROMIUM.iterdir()):
        candidate = child / "chrome-linux64" / "chrome"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("chromium binary not found")


async def _run_scenario(base: str, backend: str, profile: str = "") -> None:
    settings = _settings(backend, profile_dir=profile)
    manager = BrowserManager(settings)
    session = await manager.session_for("integration")
    try:
        async with session.lock:
            info = await session.navigate(f"{base}/")
            assert info["status"] == 200
            assert "Integration" in info["title"]
            snapshot = await session.snapshot()
            assert "[e" in snapshot
            elements = {e.name: e for e in session.last_elements}
            assert "Integration Page" in elements
            assert "Query" in elements
            assert "Count me" in elements

            counter = elements["Count me"]
            await session.click(counter.ref)
            again = await session.resolve(counter.ref)
            assert "Counted" in again.name

            query = elements["Query"]
            await session.type_text(query.ref, "hello browser")

            combo = elements["Choose one"]
            selected = await session.select_option(combo.ref, ["Beta"])
            assert selected["selected"] == ["Beta"]

            password = [e for e in session.last_elements if e.input_type == "password"]
            assert password, "password field must appear in snapshot"

            await session.press_key("Tab")
            await session.scroll("down", 300)

            table = [e for e in session.last_elements if e.tag == "table"]
            if table:
                rows = await session.active.extract_table(table[0].index)
                assert rows and rows[0] == ["Key", "Val"]

            data = await session.active.screenshot_bytes(800, 600)
            shots = session.workspace / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            (shots / "page.png").write_bytes(data)
            assert (shots / "page.png").stat().st_size > 500

            submit = [e for e in session.last_elements if e.name == "Send form"][0]
            await session.click(submit.ref)
            await asyncio.sleep(0.3)
            form_out = await session.active.evaluate("document.getElementById('out').textContent")
            assert "got:hello browser" in str(form_out)

            home = [e for e in session.last_elements if e.name == "Go second"][0]
            before_url = await session.active.url()
            await session.click(home.ref)
            after_url = await session.active.url()
            assert after_url != before_url and "/second" in after_url
            await session.back()
            assert "/second" not in await session.active.url()

            await session.set_dialog_policy("accept")
            alert_btn = [e for e in session.last_elements if e.name == "Show alert"]
            if alert_btn:
                await session.active.click(alert_btn[0].index, 5.0)
                await asyncio.sleep(1.0)
                state = await session.dialog_state()
                assert state["last_dialog"] is not None
                assert "integration dialog" in state["last_dialog"]["message"]
                assert state["last_dialog"]["handled"] == "accepted"

            await session.set_dialog_policy("dismiss")
            if alert_btn:
                await session.active.click(alert_btn[0].index, 5.0)
                await asyncio.sleep(1.0)
                state = await session.dialog_state()
                assert state["last_dialog"]["handled"] == "dismissed"

            popup = [e for e in session.last_elements if e.name == "Open popup"]
            if popup:
                before_tabs = len(session.pages)
                await session.click(popup[0].ref)
                for _ in range(30):
                    await asyncio.sleep(0.2)
                    if len(session.pages) > before_tabs:
                        break
                assert len(session.pages) > before_tabs, "popup tab was not adopted"
                assert "/popup" in await session.active.url()
    finally:
        await manager.close_all()


def test_playwright_backend_full_interaction(base_url, workspace) -> None:
    asyncio.run(_run_scenario(base_url, "playwright"))


def test_cdp_backend_full_interaction(base_url, workspace) -> None:
    if not chromium_available():
        pytest.skip("no system or playwright-managed chromium available for CDP tests")
    os.environ.setdefault("EURUS_CHROMIUM", chromium_binary())
    asyncio.run(_run_scenario(base_url, "cdp", profile="/tmp/eurus-it-cdp-profile"))


def test_stale_reference_recovery_playwright(base_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        session = await manager.session_for("stale")
        try:
            async with session.lock:
                await session.navigate(f"{base_url}/")
                await session.snapshot()
                with pytest.raises(Exception, match="unknown"):
                    await session.click("e99")
                assert len(session.last_elements) > 0
                elements = {e.name: e for e in session.last_elements}
                counter = elements["Count me"]
                await session.click(counter.ref)
                fresh = await session.snapshot()
                assert "[e1]" in fresh
        finally:
            await manager.close_all()

    asyncio.run(run())


def test_browser_cleanup_terminates_owned_process(base_url, workspace) -> None:
    if not chromium_available():
        pytest.skip("chromium not available")
    os.environ.setdefault("EURUS_CHROMIUM", chromium_binary())

    async def run() -> None:
        manager = BrowserManager(_settings("cdp", profile_dir="/tmp/eurus-cleanup-profile"))
        session = await manager.session_for("cleanup")
        async with session.lock:
            await session.navigate(f"{base_url}/")
        assert manager.backend.owns_process()
        await manager.close_all()
        assert not manager.backend.connected
        await asyncio.sleep(0.5)

    asyncio.run(run())


def test_cancelled_task_still_closes(base_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        session = await manager.session_for("cancel")
        task = asyncio.create_task(session.navigate(f"{base_url}/second"))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await manager.close_all()
        assert not manager.backend.connected
        assert manager._sessions == {}

    asyncio.run(run())


def test_multiple_runs_do_not_share_sessions(base_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        first = await manager.session_for("run-a")
        second = await manager.session_for("run-b")
        assert first is not second
        assert first.pages[0] is not second.pages[0]
        async with first.lock:
            await first.navigate(f"{base_url}/")
        async with second.lock:
            await second.navigate(f"{base_url}/second")
        assert "/second" in await second.active.url()
        assert not (await first.active.url()).endswith("/second")
        await manager.close_all()

    asyncio.run(run())


FILE_BODY = b"download-payload-0123456789" * 100


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/file.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="payload.bin"')
            self.send_header("Content-Length", str(len(FILE_BODY)))
            self.end_headers()
            self.wfile.write(FILE_BODY)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE.encode())

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def download_url():
    server = HTTPServer(("127.0.0.1", 0), DownloadHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/file.bin"
    server.shutdown()


def test_download_saves_inside_workspace(base_url, download_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        session = await manager.session_for("download")
        try:
            async with session.lock:
                await session.navigate(f"{base_url}/")
                result = await manager.download(session, download_url, "")
                saved = Path(result["file"])
                assert saved.parent.name == "downloads"
                assert saved.parent.is_relative_to(session.workspace)
                assert saved.read_bytes() == FILE_BODY
                assert result["bytes"] == len(FILE_BODY)
                assert result["url"] == download_url
        finally:
            await manager.close_all()

    asyncio.run(run())


def test_download_rejects_traversal_names(base_url, download_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        session = await manager.session_for("download-traversal")
        try:
            async with session.lock:
                await session.navigate(f"{base_url}/")
                result = await manager.download(session, download_url, "../../escape.bin")
                saved = Path(result["file"])
                assert saved.is_relative_to(manager.download_root)
        finally:
            await manager.close_all()

    asyncio.run(run())


def test_upload_via_file_input(base_url, workspace) -> None:
    async def run() -> None:
        manager = BrowserManager(_settings("playwright"))
        session = await manager.session_for("upload")
        try:
            async with session.lock:
                await session.navigate(f"{base_url}/")
                await session.snapshot()
                chosen = None
                for _attempt in range(3):
                    chosen = [e for e in session.last_elements if e.input_type == "file"]
                    if chosen:
                        break
                    await session.snapshot()
                if not chosen:
                    pytest.skip("page has no visible file input for this backend")
                payload = workspace / "upload-me.txt"
                payload.write_text("upload payload")
                result = await session.upload(chosen[0].ref, [payload])
                assert result["files"] == [str(payload)]
                value = await session.active.evaluate(
                    "document.getElementById('file1') ? "
                    "document.getElementById('file1').files.length : -1"
                )
                assert value == 1
        finally:
            await manager.close_all()

    asyncio.run(run())
