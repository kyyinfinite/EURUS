from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from eurus.browser.backend import (
    BrowserBackend,
    BrowserPage,
    ElementInfo,
    NavigationOutcome,
    find_chromium,
    parse_key_combo,
    viewport_limits,
)
from eurus.browser.snapshot import PICK_JS, WALKER_JS, build_elements
from eurus.errors import ToolError

CDP_DEFAULT_TIMEOUT = 20.0
WS_MAX_SIZE = 64 * 1024 * 1024


class CdpError(ToolError):
    pass


class CdpConnection:
    def __init__(self, ws_url: str, *, timeout: float = CDP_DEFAULT_TIMEOUT) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=800)
        self._last_seq = 0
        self._condition: asyncio.Condition = asyncio.Condition()

    @property
    def alive(self) -> bool:
        return self._ws is not None and not self._closed.is_set()

    async def connect(self) -> None:
        import websockets

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.ws_url, max_size=WS_MAX_SIZE), timeout=self.timeout
            )
        except TimeoutError as exc:
            raise CdpError("timed out connecting to the browser DevTools endpoint") from exc
        except Exception as exc:
            raise CdpError(f"cannot connect to the browser DevTools endpoint: {exc}") from exc
        self._closed.clear()
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed.set()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CdpError("browser connection closed"))
        self._pending.clear()
        async with self._condition:
            self._condition.notify_all()

    async def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                message = await self._ws.recv()
                data = json.loads(message)
                if isinstance(data.get("id"), int):
                    future = self._pending.pop(data["id"], None)
                    if future is not None and not future.done():
                        if "error" in data:
                            future.set_exception(
                                CdpError(str(data["error"].get("message", data["error"])))
                            )
                        else:
                            future.set_result(data.get("result", {}))
                elif "method" in data:
                    async with self._condition:
                        self._last_seq += 1
                        self._events.append((self._last_seq, data))
                        self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("browser connection lost"))
            self._pending.clear()
            async with self._condition:
                self._condition.notify_all()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.alive:
            raise CdpError("browser connection is not open")
        self._next_id += 1
        call_id = self._next_id
        payload: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        limit = timeout or self.timeout
        try:
            await self._ws.send(json.dumps(payload))
            result = await asyncio.wait_for(future, timeout=limit)
            return result
        except TimeoutError as exc:
            self._pending.pop(call_id, None)
            raise CdpError(f"DevTools call {method} timed out after {limit:g}s") from exc
        except OSError as exc:
            self._pending.pop(call_id, None)
            raise CdpError(f"browser connection failed during {method}: {exc}") from exc

    async def next_event(
        self, timeout: float, since: int = 0, session_id: str = "", prefix: str = ""
    ) -> tuple[dict[str, Any] | None, int]:
        deadline = time.monotonic() + timeout
        cursor = since
        while True:
            async with self._condition:
                for seq, event in tuple(self._events):
                    if seq <= cursor:
                        continue
                    cursor = seq
                    if session_id and event.get("sessionId") != session_id:
                        continue
                    if prefix and not str(event.get("method", "")).startswith(prefix):
                        continue
                    return event, seq
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, cursor
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return None, cursor

    async def wait_event_cursor(
        self, method: str, timeout: float, session_id: str = "", since: int = 0
    ) -> tuple[dict[str, Any] | None, int]:
        deadline = time.monotonic() + timeout
        cursor = since
        while True:
            async with self._condition:
                for seq, event in tuple(self._events):
                    if seq <= cursor:
                        continue
                    cursor = seq
                    if event.get("method") != method:
                        continue
                    if session_id and event.get("sessionId") != session_id:
                        continue
                    return event, seq
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, cursor
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return None, cursor

    def position(self) -> int:
        return self._last_seq


class CdpSession:
    def __init__(self, connection: CdpConnection, session_id: str) -> None:
        self.connection = connection
        self.session_id = session_id

    async def send(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        return await self.connection.send(
            method, params, session_id=self.session_id, timeout=timeout
        )

    async def wait_event_cursor(
        self, method: str, timeout: float, since: int = 0
    ) -> tuple[dict[str, Any] | None, int]:
        return await self.connection.wait_event_cursor(method, timeout, self.session_id, since)


class CdpBackend(BrowserBackend):
    name = "cdp"

    def __init__(self, settings: Any, *, variant: str = "cdp") -> None:
        super().__init__(settings)
        self.variant = variant
        self.connection: CdpConnection | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._port = 0
        self._pages: dict[str, CdpPage] = {}
        self._hooks: Any = None
        self._download_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._guard: Any = None
        self._monitor_task: asyncio.Task[None] | None = None

    def set_hooks(self, hooks: Any) -> None:
        self._hooks = hooks

    def set_request_guard(self, guard: Any) -> None:
        self._guard = guard

    @property
    def connected(self) -> bool:
        return self.connection is not None and self.connection.alive

    def owns_process(self) -> bool:
        return self._process is not None

    async def start(self) -> None:
        async with self._lock:
            if self.connected:
                return
            await self._close_connection()
            await self._terminate_process()
            endpoint = getattr(self.settings, "cdp_endpoint", "") or ""
            if endpoint or self.variant in {"remote", "attach"}:
                await self._connect_remote(endpoint)
            else:
                await self._launch_local()
            await self._after_connect()

    async def _connect_remote(self, endpoint: str) -> None:
        import httpx

        base = endpoint if "://" in endpoint else f"http://{endpoint}"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"}:
            raise CdpError("browser.cdp_endpoint must use http(s)")
        if not parsed.hostname:
            raise CdpError("browser.cdp_endpoint must include a host")
        version_url = base.rstrip("/") + "/json/version"
        startup = float(getattr(self.settings, "startup_timeout", 25.0))
        try:
            async with httpx.AsyncClient(timeout=startup) as client:
                response = await client.get(version_url)
                response.raise_for_status()
                info = response.json()
        except Exception as exc:
            raise CdpError(
                f"cannot reach the CDP endpoint {base}; is the browser running with "
                f"--remote-debugging-port? ({type(exc).__name__})"
            ) from exc
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("the CDP endpoint did not return a webSocketDebuggerUrl")
        self.connection = CdpConnection(ws_url, timeout=startup)
        await self.connection.connect()

    async def _launch_local(self) -> None:
        executable = find_chromium(self.settings.chromium_executable)
        self._port = _free_port()
        profile = self._profile_dir()
        args = [
            executable,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={profile}",
            "--headless=new" if self.settings.headless else "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--mute-audio",
            "--noerrdialogs",
            "--disable-features=Translate,MediaRouter",
            "--remote-allow-origins=*",
            "about:blank",
        ]
        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._process = None
            raise CdpError(f"cannot start Chromium at {executable}: {exc}") from exc
        startup = float(getattr(self.settings, "startup_timeout", 25.0))
        deadline = time.monotonic() + startup
        import httpx

        try:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    code = self._process.returncode
                    self._process = None
                    raise CdpError(
                        f"Chromium exited immediately (code {code}); the browser profile may "
                        "be locked by another Chromium instance or the executable may be broken"
                    )
                try:
                    async with httpx.AsyncClient(timeout=1.5) as client:
                        response = await client.get(f"http://127.0.0.1:{self._port}/json/version")
                        if response.status_code == 200:
                            info = response.json()
                            ws_url = info.get("webSocketDebuggerUrl") or (
                                f"ws://127.0.0.1:{self._port}/devtools/browser"
                            )
                            self.connection = CdpConnection(ws_url, timeout=CDP_DEFAULT_TIMEOUT)
                            await self.connection.connect()
                            return
                except CdpError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(0.2)
        except BaseException:
            await self._terminate_process()
            raise
        await self._terminate_process()
        raise CdpError(f"Chromium did not expose its DevTools endpoint within {startup:g}s")

    def _profile_dir(self) -> Path:
        configured = getattr(self.settings, "profile_dir", "") or ""
        if configured:
            profile = Path(configured).expanduser()
        else:
            from eurus import paths

            profile = paths.home_dir() / "browser" / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        return profile

    async def _after_connect(self) -> None:
        assert self.connection is not None
        with contextlib.suppress(Exception):
            await self.connection.send(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                },
            )
        download_dir = self._download_dir or Path.cwd()
        download_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            await self.connection.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": str(download_dir),
                    "eventsEnabled": True,
                },
            )
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self) -> None:
        assert self.connection is not None
        cursor = self.connection.position()
        while self.connection.alive:
            try:
                event, cursor = await self.connection.next_event(
                    timeout=0.2, since=cursor, prefix="Target."
                )
                if event is None:
                    continue
                await self._handle_target_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.2)

    async def _handle_target_event(self, event: dict[str, Any]) -> None:
        method = event.get("method", "")
        params = event.get("params", {})
        info = params.get("targetInfo", {})
        if info.get("type") != "page":
            return
        target_id = str(info.get("targetId", ""))
        if method == "Target.targetDestroyed":
            self.note_closed(target_id)
            page = self._pages.get(target_id)
            if self._hooks is not None and page is not None and self._hooks.page_closed is not None:
                self._hooks.page_closed(page)
            return
        opener_id = str(info.get("openerId", ""))
        opener = self._pages.get(opener_id)
        known = self._pages.get(target_id)
        if known is not None:
            if (
                method == "Target.targetInfoChanged"
                and self._hooks is not None
                and self._hooks.page_info_changed is not None
            ):
                self._hooks.page_info_changed(known)
            return
        if not opener_id and method == "Target.attachedToTarget":
            return
        self.adopt_target(target_id, opener)

    def set_download_dir(self, directory: Path) -> None:
        self._download_dir = directory

    async def stop(self) -> None:
        async with self._lock:
            if self._monitor_task is not None:
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._monitor_task
                self._monitor_task = None
            for page in list(self._pages.values()):
                with contextlib.suppress(Exception):
                    await page.shutdown()
            self._pages.clear()
            await self._close_connection()
            await self._terminate_process()

    async def _close_connection(self) -> None:
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.close()
            self.connection = None

    async def _terminate_process(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        for _ in range(30):
            if process.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=2)

    async def open_page(self) -> CdpPage:
        await self.start()
        assert self.connection is not None
        result = await self.connection.send("Target.createTarget", {"url": "about:blank"})
        target_id = str(result.get("targetId", ""))
        page = CdpPage(self, target_id)
        self._pages[target_id] = page
        await page.initialize()
        return page

    def adopt_target(self, target_id: str, opener: CdpPage | None) -> CdpPage | None:
        if target_id in self._pages:
            return self._pages[target_id]
        page = CdpPage(self, target_id)
        self._pages[target_id] = page
        if self._hooks is not None and self._hooks.page_opened is not None:
            self._hooks.page_opened(page, opener)
            return page
        asyncio.get_running_loop().create_task(_quiet_initialize(page))
        return page

    def note_closed(self, target_id: str) -> None:
        self._pages.pop(target_id, None)

    async def status(self) -> dict[str, Any]:
        label = self.variant if self.variant in {"remote", "attach"} else self.name
        return {
            "backend": label,
            "connected": self.connected,
            "owns_process": self.owns_process(),
            "port": self._port,
            "pages": len(self._pages),
            "headless": self.settings.headless,
        }


async def _quiet_initialize(page: CdpPage) -> None:
    with contextlib.suppress(Exception):
        await page.initialize()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CdpPage(BrowserPage):
    def __init__(self, backend: CdpBackend, target_id: str) -> None:
        self.backend = backend
        self.target_id = target_id
        self.session: CdpSession | None = None
        self.timeout_ms = 30_000
        self.last_dialog: dict[str, str] | None = None
        self.dialog_policy: str = "dismiss"
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    def set_timeout(self, timeout_seconds: float) -> None:
        self.timeout_ms = max(1000, int(timeout_seconds * 1000))

    async def initialize(self) -> None:
        if self.session is None:
            assert self.backend.connection is not None
            result = await self.backend.connection.send(
                "Target.attachToTarget", {"targetId": self.target_id, "flatten": True}
            )
            self.session = CdpSession(self.backend.connection, str(result.get("sessionId", "")))
        init_calls: list[tuple[str, dict[str, Any]]] = [
            ("Page.enable", {}),
            ("Runtime.enable", {}),
            ("Network.enable", {}),
            ("DOM.enable", {}),
        ]
        for method, params in init_calls:
            with contextlib.suppress(Exception):
                await self.session.send(method, params)
        if self.backend._guard is not None:
            with contextlib.suppress(Exception):
                await self.session.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        width, height = viewport_limits(self.backend.settings)
        with contextlib.suppress(Exception):
            await self.session.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())

    async def shutdown(self) -> None:
        self._closed = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None

    async def _pump(self) -> None:
        fetch_cursor = 0
        dialog_cursor = 0
        if self.session is not None:
            start = self.session.connection.position()
            fetch_cursor = start
            dialog_cursor = start
        while not self._closed and self.session is not None:
            try:
                if self.backend._guard is not None:
                    event, fetch_cursor = await self.session.wait_event_cursor(
                        "Fetch.requestPaused", timeout=0.1, since=fetch_cursor
                    )
                    if event is not None:
                        await self._answer_fetch(event)
                        continue
                event, dialog_cursor = await self.session.wait_event_cursor(
                    "Page.javascriptDialogOpening", timeout=0.1, since=dialog_cursor
                )
                if event is not None:
                    self._record_dialog(event)
                    await self._answer_dialog()
                    continue
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.2)

    async def _answer_fetch(self, event: dict[str, Any]) -> None:
        assert self.session is not None
        params = event.get("params", {})
        request_id = params.get("requestId")
        url = str(params.get("request", {}).get("url", ""))
        allowed = True
        if self.backend._guard is not None:
            try:
                await self.backend._guard(url)
            except Exception:
                allowed = False
        if not allowed and request_id is not None:
            with contextlib.suppress(Exception):
                await self.session.send(
                    "Fetch.failRequest",
                    {"requestId": request_id, "reason": "BlockedByClient"},
                )
            return
        if request_id is not None:
            with contextlib.suppress(Exception):
                await self.session.send("Fetch.continueRequest", {"requestId": request_id})

    def _record_dialog(self, event: dict[str, Any]) -> None:
        params = event.get("params", {})
        self.last_dialog = {
            "message": str(params.get("message", "")),
            "type": str(params.get("type", "")),
            "url": str(params.get("url", "")),
            "handled": "dismissed",
        }

    async def _answer_dialog(self) -> None:
        assert self.session is not None
        accept = self.dialog_policy == "accept_once"
        if accept:
            self.dialog_policy = "dismiss"
            if self.last_dialog is not None:
                self.last_dialog["handled"] = "accepted"
        with contextlib.suppress(Exception):
            await self.session.send("Page.handleJavaScriptDialog", {"accept": accept})

    async def url(self) -> str:
        assert self.backend.connection is not None
        result = await self.backend.connection.send(
            "Target.getTargetInfo", {"targetId": self.target_id}
        )
        return str(result.get("targetInfo", {}).get("url", ""))

    async def title(self) -> str:
        assert self.backend.connection is not None
        result = await self.backend.connection.send(
            "Target.getTargetInfo", {"targetId": self.target_id}
        )
        return str(result.get("targetInfo", {}).get("title", ""))

    async def goto(self, url: str, timeout: float) -> NavigationOutcome:
        await self.initialize()
        assert self.session is not None
        before = await self.url()
        cursor = self.session.connection.position()
        try:
            await self.session.send("Page.navigate", {"url": url})
        except CdpError as exc:
            raise ToolError(f"navigation to {url} failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event, _cursor = await self.session.wait_event_cursor(
                "Page.loadEventFired", timeout=0.2, since=cursor
            )
            if event is not None:
                break
        current = await self.url()
        status = await self._main_response_status(cursor)
        return NavigationOutcome(url=current, status=status, redirected=current != before)

    async def _main_response_status(self, cursor: int) -> int:
        assert self.session is not None
        condition = self.session.connection._condition
        async with condition:
            events = [
                event for seq, event in tuple(self.session.connection._events) if seq > cursor
            ]
        frame_id = ""
        for event in events:
            if event.get("method") == "FrameNavigated":
                frame = event.get("params", {}).get("frame", {})
                if not frame.get("parentId"):
                    frame_id = str(frame.get("id", ""))
        for event in reversed(events):
            if event.get("method") != "Network.responseReceived":
                continue
            params = event.get("params", {})
            if frame_id and params.get("frameId") != frame_id:
                continue
            return int(params.get("response", {}).get("status", 0))
        return 0

    async def text_content(self, max_chars: int) -> str:
        value = await self.evaluate("document.body ? document.body.innerText : ''")
        return str(value)[:max_chars]

    async def collect_elements(self, max_elements: int) -> list[ElementInfo]:
        raw = await self.evaluate(f"({WALKER_JS})()")
        if not isinstance(raw, list):
            return []
        return build_elements(raw[:max_elements])

    async def _pick_object(self, index: int) -> str:
        await self.initialize()
        assert self.session is not None
        result = await self.session.send(
            "Runtime.evaluate",
            {"expression": f"({PICK_JS})({int(index)})", "returnByValue": False},
        )
        object_id = result.get("result", {}).get("objectId")
        if not object_id:
            raise ToolError("the element is no longer available; take a new browser_snapshot")
        return str(object_id)

    async def element_handle(self, index: int) -> str:
        return await self._pick_object(index)

    async def _release(self, object_id: str) -> None:
        assert self.session is not None
        with contextlib.suppress(Exception):
            await self.session.send("Runtime.releaseObject", {"objectId": object_id}, timeout=3.0)

    async def _rect(self, object_id: str) -> tuple[float, float]:
        assert self.session is not None
        result = await self.session.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": """function () {
                    const el = this;
                    if (el.scrollIntoViewIfNeeded) el.scrollIntoViewIfNeeded(true);
                    else el.scrollIntoView({block: 'center'});
                    const rect = el.getBoundingClientRect();
                    return {x: rect.x, y: rect.y, w: rect.width, h: rect.height};
                }""",
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value", {})
        if not value or value.get("w", 0) <= 0:
            raise ToolError("the element has no visible area to interact with")
        return float(value["x"]) + float(value["w"]) / 2, float(value["y"]) + float(value["h"]) / 2

    async def click(self, index: int, timeout: float) -> None:
        await self.initialize()
        assert self.session is not None
        object_id = await self._pick_object(index)
        try:
            x, y = await self._rect(object_id)
            with contextlib.suppress(CdpError):
                await self.session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                    timeout=5.0,
                )
                await self.session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                    timeout=5.0,
                )
        finally:
            await self._release(object_id)

    async def fill(self, index: int, text: str, timeout: float) -> None:
        await self.initialize()
        assert self.session is not None
        object_id = await self._pick_object(index)
        try:
            await self.session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """function () {
                        const el = this;
                        el.focus();
                        if (el.select) el.select();
                        if (el.setSelectionRange && el.value)
                            el.setSelectionRange(0, el.value.length);
                        if (el.isContentEditable) {
                            const range = document.createRange();
                            range.selectNodeContents(el);
                            const selection = window.getSelection();
                            if (selection) {
                                selection.removeAllRanges();
                                selection.addRange(range);
                            }
                        }
                        return true;
                    }""",
                    "returnByValue": True,
                },
            )
            await self.session.send("Input.insertText", {"text": text}, timeout=5.0)
        finally:
            await self._release(object_id)

    async def press_key(self, key: str) -> None:
        await self.initialize()
        assert self.session is not None
        parsed = parse_key_combo(key)
        base: dict[str, Any] = {
            "modifiers": parsed["modifiers"],
            "windowsVirtualKeyCode": parsed["virtual"],
            "nativeVirtualKeyCode": parsed["virtual"],
            "key": parsed["key"],
        }
        if parsed["code"]:
            base["code"] = parsed["code"]
        down = dict(base)
        if parsed["text"]:
            down["type"] = "keyDown"
            down["text"] = parsed["text"]
            down["unmodifiedText"] = parsed["text"]
        else:
            down["type"] = "rawKeyDown"
        with contextlib.suppress(CdpError):
            await self.session.send("Input.dispatchKeyEvent", down, timeout=5.0)
        with contextlib.suppress(CdpError):
            await self.session.send(
                "Input.dispatchKeyEvent", {**base, "type": "keyUp"}, timeout=5.0
            )

    async def select_options(self, index: int, values: list[str], timeout: float) -> list[str]:
        await self.initialize()
        assert self.session is not None
        object_id = await self._pick_object(index)
        try:
            result = await self.session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """function (wanted) {
                        const el = this;
                        if (el.tagName !== 'SELECT')
                            throw new Error('element is not a select dropdown');
                        const selected = [];
                        for (const option of Array.from(el.options)) {
                            const match = wanted.includes(option.value) ||
                                wanted.includes(option.textContent.trim());
                            option.selected = match;
                            if (match) selected.push(option.textContent.trim());
                        }
                        if (!selected.length && wanted.length) {
                            throw new Error('no option matched: ' + wanted.join(', '));
                        }
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return selected;
                    }""",
                    "arguments": [{"value": values}],
                    "returnByValue": True,
                },
            )
            return list(result.get("result", {}).get("value", []))
        except CdpError as exc:
            raise ToolError(f"could not select options: {exc}") from exc
        finally:
            await self._release(object_id)

    async def hover(self, index: int, timeout: float) -> None:
        await self.initialize()
        assert self.session is not None
        object_id = await self._pick_object(index)
        try:
            x, y = await self._rect(object_id)
            await self.session.send(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}
            )
        finally:
            await self._release(object_id)

    async def scroll(self, x: int, y: int) -> None:
        await self.evaluate(f"window.scrollBy({int(x)}, {int(y)})")
        await asyncio.sleep(0.15)

    async def screenshot_bytes(self, width: int, height: int) -> bytes:
        await self.initialize()
        assert self.session is not None
        with contextlib.suppress(Exception):
            await self.session.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
        result = await self.session.send(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        data = str(result.get("data", ""))
        return base64.b64decode(data)

    async def upload(self, index: int, files: list[Path], timeout: float) -> None:
        await self.initialize()
        assert self.session is not None
        object_id = await self._pick_object(index)
        try:
            tag = await self.evaluate_object(
                object_id, "function () { return this.tagName.toLowerCase(); }"
            )
            input_type = await self.evaluate_object(
                object_id, "function () { return (this.type || '').toLowerCase(); }"
            )
            if tag != "input" or input_type != "file":
                raise ToolError(
                    "uploads target an <input type=file> element; take a snapshot and use the "
                    "reference of the file input"
                )
            node = await self.session.send("DOM.requestNode", {"objectId": object_id})
            await self.session.send(
                "DOM.setFileInputFiles",
                {"files": [str(item) for item in files], "nodeId": int(node.get("nodeId", 0))},
            )
        finally:
            await self._release(object_id)

    async def evaluate_object(self, object_id: str, function: str) -> Any:
        assert self.session is not None
        result = await self.session.send(
            "Runtime.callFunctionOn",
            {"objectId": object_id, "functionDeclaration": function, "returnByValue": True},
        )
        return result.get("result", {}).get("value")

    async def evaluate(self, expression: str) -> Any:
        await self.initialize()
        assert self.session is not None
        result = await self.session.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        return result.get("result", {}).get("value")

    async def evaluate_quick(self, expression: str, timeout: float = 3.0) -> Any:
        if self.session is None or not self.session.connection.alive:
            return None
        try:
            result = await self.session.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": False},
                timeout=timeout,
            )
            return result.get("result", {}).get("value")
        except Exception:
            return None

    async def wait_load(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.evaluate_quick("document.readyState", timeout=2.0)
            if state in {"complete", "interactive"}:
                return
            await asyncio.sleep(0.1)

    async def wait_network_idle(self, timeout: float, idle_seconds: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        ready_at: float | None = None
        while time.monotonic() < deadline:
            state = await self.evaluate_quick("document.readyState", timeout=2.0)
            if state == "complete":
                if ready_at is None:
                    ready_at = time.monotonic()
                elif time.monotonic() - ready_at >= idle_seconds:
                    return True
            else:
                ready_at = None
            await asyncio.sleep(0.1)
        return False

    async def content_bytes(self, max_bytes: int) -> bytes:
        raw = await self.evaluate("document.documentElement.outerHTML")
        return str(raw or "").encode("utf-8", errors="ignore")[:max_bytes]

    async def read_text(self, index: int, max_chars: int) -> str:
        object_id = await self._pick_object(index)
        try:
            value = await self.evaluate_object(
                object_id, "function () { return this.innerText || this.value || ''; }"
            )
            text = str(value or "")[:max_chars]
            if not text.strip():
                raise ToolError("element produced no readable text")
            return text
        finally:
            await self._release(object_id)

    async def extract_table(self, index: int) -> list[list[str]]:
        object_id = await self._pick_object(index)
        try:
            value = await self.evaluate_object(
                object_id,
                """function () {
                    const el = this;
                    const table = el.tagName === 'TABLE' ? el : el.querySelector('table');
                    if (!table) return [];
                    const rows = [];
                    for (const tr of table.querySelectorAll('tr')) {
                        const cells = Array.from(tr.querySelectorAll('th,td'))
                            .map(c => (c.innerText || '').trim());
                        if (cells.length) rows.push(cells);
                    }
                    return rows.slice(0, 200);
                }""",
            )
            return [list(map(str, row)) for row in (value or [])][:200]
        finally:
            await self._release(object_id)

    async def close(self) -> None:
        connection = self.backend.connection
        await self.shutdown()
        if connection is None:
            self.backend.note_closed(self.target_id)
            return
        with contextlib.suppress(Exception):
            await connection.send("Target.closeTarget", {"targetId": self.target_id})
        self.backend.note_closed(self.target_id)

    async def _query_object(self, selector: str) -> str:
        await self.initialize()
        assert self.session is not None
        result = await self.session.send(
            "Runtime.evaluate",
            {
                "expression": f"document.querySelector({json.dumps(selector)})",
                "returnByValue": False,
            },
        )
        object_id = result.get("result", {}).get("objectId")
        if not object_id:
            raise ToolError(f"no element matched the selector '{selector}'")
        return str(object_id)

    async def click_element_by_selector(self, selector: str) -> None:
        object_id = await self._query_object(selector)
        try:
            x, y = await self._rect(object_id)
            assert self.session is not None
            await self.session.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
            await self.session.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
        finally:
            await self._release(object_id)

    async def fill_by_selector(self, selector: str, text: str) -> None:
        object_id = await self._query_object(selector)
        try:
            assert self.session is not None
            await self.session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """function () {
                        const el = this;
                        el.focus();
                        if (el.select) el.select();
                        if (el.setSelectionRange && el.value)
                            el.setSelectionRange(0, el.value.length);
                        return true;
                    }""",
                    "returnByValue": True,
                },
            )
            await self.session.send("Input.insertText", {"text": text}, timeout=5.0)
        finally:
            await self._release(object_id)

    async def read_by_selector(self, selector: str) -> str:
        object_id = await self._query_object(selector)
        try:
            value = await self.evaluate_object(
                object_id, "function () { return this.innerText || this.value || ''; }"
            )
            return str(value or "")
        finally:
            await self._release(object_id)
