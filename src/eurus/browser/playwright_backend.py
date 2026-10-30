from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from eurus.browser.backend import (
    BrowserBackend,
    BrowserPage,
    DownloadHandle,
    ElementInfo,
    NavigationOutcome,
    viewport_limits,
)
from eurus.browser.snapshot import PICK_JS, WALKER_JS
from eurus.errors import ToolError

UNAVAILABLE_MESSAGE = (
    "browser automation is unavailable in this environment; install the optional "
    "`browser` extra (pip install -e '.[browser]') and run `playwright install "
    "chromium`; web_fetch remains available as a non-browser fallback"
)


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


class PlaywrightBackend(BrowserBackend):
    name = "playwright"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._profile_dir: Path | None = None
        self._hooks: Any = None
        self._guard: Any = None
        self._lock = asyncio.Lock()
        self._wrappers: dict[Any, Any] = {}
        self._known_pages: set[Any] = set()
        self._expecting_page = False

    def set_hooks(self, hooks: Any) -> None:
        self._hooks = hooks

    def set_request_guard(self, guard: Any) -> None:
        self._guard = guard

    @property
    def connected(self) -> bool:
        return self._browser is not None or self._context is not None

    def owns_process(self) -> bool:
        return self._browser is not None

    async def _route_guard(self, route: Any) -> None:
        url = str(route.request.url)
        if self._guard is None:
            await route.continue_()
            return
        try:
            await self._guard(url)
        except Exception:
            await route.abort()
            return
        await route.continue_()

    async def start(self) -> None:
        async with self._lock:
            if self.connected:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise ToolError(UNAVAILABLE_MESSAGE) from exc
            self._playwright = await async_playwright().start()
            try:
                await self._launch()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None
                raise ToolError(
                    "browser could not be launched; run `playwright install chromium` "
                    f"({type(exc).__name__}: {exc})"
                ) from exc

    async def _launch(self) -> None:
        width, height = viewport_limits(self.settings)
        launch_kwargs: dict[str, Any] = {
            "viewport": {"width": width, "height": height},
            "accept_downloads": True,
        }
        if self.settings.chromium_executable:
            launch_kwargs["executable_path"] = self.settings.chromium_executable
        profile = self._resolve_profile_dir()
        if profile is not None:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile),
                headless=self.settings.headless,
                **launch_kwargs,
            )
            for page in self._context.pages:
                self._known_pages.add(page)
            await self._context.route("**/*", self._route_guard)
            return
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless,
            executable_path=launch_kwargs.get("executable_path") or None,
        )

    def _resolve_profile_dir(self) -> Path | None:
        if self._profile_dir is not None:
            return self._profile_dir if str(self._profile_dir) else None
        configured = getattr(self.settings, "profile_dir", "") or ""
        return Path(configured).expanduser() if configured else None

    def use_profile(self, profile_dir: Path) -> None:
        self._profile_dir = profile_dir

    async def stop(self) -> None:
        async with self._lock:
            context, self._context = self._context, None
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            errors: list[str] = []
            for closer in (context, browser):
                if closer is None:
                    continue
                try:
                    await closer.close()
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}")
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}")
            if errors:
                raise ToolError("browser close reported issues: " + ", ".join(sorted(set(errors))))

    async def open_page(self) -> PlaywrightPage:
        await self.start()
        if self._context is not None:
            page = await self._context.new_page()
            return PlaywrightPage(page, hooks=self._hooks)
        if self._browser is None:
            raise ToolError(UNAVAILABLE_MESSAGE)
        width, height = viewport_limits(self.settings)
        context = await self._browser.new_context(
            viewport={"width": width, "height": height},
            accept_downloads=True,
        )
        context.on("page", self._on_context_page)
        await context.route("**/*", self._route_guard)
        self._expecting_page = True
        page = await context.new_page()
        self._known_pages.add(page)
        self._expecting_page = False
        wrapper = PlaywrightPage(page, hooks=self._hooks, context=context)
        return wrapper

    def _on_context_page(self, page: Any) -> None:
        if self._expecting_page or page in self._known_pages:
            self._known_pages.add(page)
            self._expecting_page = False
            return
        if self._hooks is None or self._hooks.page_opened is None:
            return
        wrapper = PlaywrightPage(page, hooks=self._hooks)
        self._wrappers[page] = wrapper
        asyncio.ensure_future(self._announce(wrapper))

    async def _announce(self, wrapper: Any) -> None:
        opener: Any = None
        try:
            raw_opener = await wrapper.page.opener()
            if raw_opener is not None:
                opener = self._wrappers.get(raw_opener)
        except Exception:
            opener = None
        if self._hooks is not None and self._hooks.page_opened is not None:
            self._hooks.page_opened(wrapper, opener)

    async def status(self) -> dict[str, Any]:
        pages = 0
        if self._context is not None:
            pages = len(self._context.pages)
        elif self._browser is not None:
            pages = sum(len(context.pages) for context in self._browser.contexts)
        return {
            "backend": self.name,
            "connected": self.connected,
            "owns_process": self.owns_process(),
            "pages": pages,
            "headless": self.settings.headless,
        }


class PlaywrightPage(BrowserPage):
    def __init__(
        self,
        page: Any,
        *,
        hooks: Any = None,
        context: Any = None,
    ) -> None:
        self.page = page
        self.context = context
        self._hooks = hooks
        self.timeout_ms = 30_000
        page.on("download", self._on_download)
        page.on("dialog", self._on_dialog)
        self.pending_dialog: Any = None
        self.last_dialog: dict[str, str] | None = None
        self.dialog_policy: str = "dismiss"

    def set_timeout(self, timeout_seconds: float) -> None:
        self.timeout_ms = max(1000, int(timeout_seconds * 1000))

    def _on_dialog(self, dialog: Any) -> None:
        self.last_dialog = {
            "message": str(getattr(dialog, "message", "")),
            "type": str(getattr(dialog, "type", "")),
            "url": str(getattr(self.page, "url", "")),
        }
        if self.dialog_policy == "accept_once":
            self.dialog_policy = "dismiss"
            asyncio.ensure_future(self._safe_accept(dialog))
            self.last_dialog["handled"] = "accepted"
        else:
            asyncio.ensure_future(self._safe_dismiss(dialog))
            self.last_dialog["handled"] = "dismissed"

    async def _safe_accept(self, dialog: Any) -> None:
        with contextlib.suppress(Exception):
            await dialog.accept()

    async def _safe_dismiss(self, dialog: Any) -> None:
        with contextlib.suppress(Exception):
            await dialog.dismiss()

    def _on_download(self, download: Any) -> None:
        if self._hooks is None or self._hooks.download_started is None:
            return
        handle = DownloadHandle(
            suggested_name=str(getattr(download, "suggested_filename", "") or "download.bin"),
            url=str(getattr(download, "url", "")),
            token=str(id(download)),
        )
        self._hooks.download_started(self, download, handle)

    async def url(self) -> str:
        return str(self.page.url)

    async def title(self) -> str:
        return str(await self.page.title())

    async def goto(self, url: str, timeout: float) -> NavigationOutcome:
        before = str(self.page.url)
        response = await self.page.goto(
            url, timeout=int(timeout * 1000), wait_until="domcontentloaded"
        )
        status = int(response.status) if response is not None else 0
        current = str(self.page.url)
        return NavigationOutcome(url=current, status=status, redirected=current != before)

    async def text_content(self, max_chars: int) -> str:
        try:
            body = self.page.locator("body")
            text = await body.inner_text(timeout=self.timeout_ms)
        except Exception:
            text = str(await self.page.evaluate("document.body ? document.body.innerText : ''"))
        return str(text)[:max_chars]

    async def collect_elements(self, max_elements: int) -> list[ElementInfo]:
        from eurus.browser.snapshot import build_elements

        raw = await self.page.evaluate(WALKER_JS)
        if not isinstance(raw, list):
            return []
        if len(raw) > max_elements:
            raw = raw[:max_elements]
        return build_elements(raw)

    async def element_handle(self, index: int) -> Any:
        handle = await self.page.evaluate_handle(PICK_JS, index)
        element = handle.as_element()
        if element is None:
            raise ToolError("the element is no longer available; take a new browser_snapshot")
        return element

    async def click(self, index: int, timeout: float) -> None:
        element = await self.element_handle(index)
        try:
            await element.click(timeout=int(timeout * 1000))
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def fill(self, index: int, text: str, timeout: float) -> None:
        element = await self.element_handle(index)
        try:
            await element.fill(text, timeout=int(timeout * 1000))
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def press_key(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def select_options(self, index: int, values: list[str], timeout: float) -> list[str]:
        element = await self.element_handle(index)
        try:
            await element.select_option(values, timeout=int(timeout * 1000))
            return list(values)
        except Exception as exc:
            raise ToolError(f"could not select options: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def hover(self, index: int, timeout: float) -> None:
        element = await self.element_handle(index)
        try:
            await element.hover(timeout=int(timeout * 1000))
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def scroll(self, x: int, y: int) -> None:
        await self.page.mouse.wheel(x, y)
        await asyncio.sleep(0.15)

    async def screenshot_bytes(self, width: int, height: int) -> bytes:
        await self.page.set_viewport_size({"width": width, "height": height})
        data = await self.page.screenshot(type="png", timeout=self.timeout_ms)
        return bytes(data)

    async def upload(self, index: int, files: list[Path], timeout: float) -> None:
        element = await self.element_handle(index)
        try:
            tag = str(await element.evaluate("el => el.tagName.toLowerCase()"))
            input_type = str(await element.evaluate("el => (el.type || '').toLowerCase()"))
            if tag != "input" or input_type != "file":
                raise ToolError(
                    "uploads target an <input type=file> element; take a snapshot and use the "
                    "reference of the file input"
                )
            await element.set_input_files(
                [str(item) for item in files], timeout=int(timeout * 1000)
            )
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def evaluate(self, expression: str) -> Any:
        return await self.page.evaluate(expression)

    async def wait_load(self, timeout: float) -> None:
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))

    async def wait_network_idle(self, timeout: float, idle_seconds: float = 0.5) -> bool:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=int(timeout * 1000))
            return True
        except Exception:
            return False

    async def content_bytes(self, max_bytes: int) -> bytes:
        raw = str(await self.page.content())
        return raw.encode("utf-8", errors="ignore")[:max_bytes]

    async def read_text(self, index: int, max_chars: int) -> str:
        element = await self.element_handle(index)
        try:
            return str(await element.inner_text())[:max_chars]
        except Exception as exc:
            raise ToolError(f"element produced no readable text: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def extract_table(self, index: int) -> list[list[str]]:
        element = await self.element_handle(index)
        try:
            rows = await element.evaluate(
                """el => {
                    const table = el.tagName === 'TABLE' ? el : el.querySelector('table');
                    if (!table) return [];
                    const rows = [];
                    for (const tr of table.querySelectorAll('tr')) {
                        const cells = Array.from(tr.querySelectorAll('th,td'))
                            .map(c => (c.innerText || '').trim());
                        if (cells.length) rows.push(cells);
                    }
                    return rows.slice(0, 200);
                }"""
            )
            return [list(map(str, row)) for row in (rows or [])][:200]
        except Exception as exc:
            raise ToolError(f"could not extract table data: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await element.dispose()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.close()

    async def click_element_by_selector(self, selector: str) -> None:
        await self.page.click(selector, timeout=self.timeout_ms)

    async def fill_by_selector(self, selector: str, text: str) -> None:
        await self.page.fill(selector, text, timeout=self.timeout_ms)

    async def read_by_selector(self, selector: str) -> str:
        try:
            return str(await self.page.inner_text(selector, timeout=self.timeout_ms))
        except Exception as exc:
            raise ToolError(f"no element matched the selector: {exc}") from exc

    async def dispose_handle(self, handle: Any) -> None:
        with contextlib.suppress(Exception):
            await handle.dispose()


def parse_download_payload(download: Any) -> dict[str, Any]:
    return {
        "suggested_filename": getattr(download, "suggested_filename", ""),
        "url": getattr(download, "url", ""),
    }


def describe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
