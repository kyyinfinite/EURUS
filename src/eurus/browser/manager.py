from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.browser.backend import (
    BrowserBackend,
    BrowserPage,
    DownloadHandle,
    ElementInfo,
    resolve_backend,
    sanitized_filename,
)
from eurus.browser.cdp_backend import CdpBackend
from eurus.browser.playwright_backend import PlaywrightBackend, playwright_available
from eurus.browser.risk import describe_click, describe_type
from eurus.browser.security import BrowserPolicyError, DnsCache, DomainPolicy, UrlGuard
from eurus.browser.snapshot import (
    RefResolver,
    format_snapshot,
)
from eurus.config import BrowserConfig as _BC
from eurus.errors import ToolError
from eurus.termux import is_termux

MAX_TEXT_CHARS = 12_000
DEFAULT_IDLE_WAIT = 1.0
SCROLL_STEP = 600
DEFAULT_MAX_SESSIONS = 3


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    enabled: bool = False
    backend: str = "auto"
    cdp_endpoint: str = ""
    chromium_executable: str = ""
    profile_dir: str = ""
    headless: bool = True
    navigation_timeout: float = 30.0
    interaction_timeout: float = 10.0
    startup_timeout: float = 25.0
    max_snapshot_tokens: int = 3000
    max_text_chars: int = MAX_TEXT_CHARS
    max_page_bytes: int = 2_000_000
    max_screenshot_width: int = 1280
    max_screenshot_height: int = 800
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    download_dir: str = ""
    max_download_mb: int = 50
    max_screenshot_mb: int = 10
    dns_cache_ttl: float = 60.0
    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_idle_ttl: float = 900.0
    element_cap: int = 400

    @classmethod
    def from_config(cls, config: _BC) -> BrowserSettings:
        return cls(
            enabled=config.enabled,
            backend=config.backend,
            cdp_endpoint=config.cdp_endpoint,
            chromium_executable=config.chromium_executable,
            profile_dir=config.profile_dir,
            headless=config.headless,
            navigation_timeout=config.navigation_timeout_seconds,
            interaction_timeout=config.interaction_timeout_seconds,
            startup_timeout=config.startup_timeout_seconds,
            max_snapshot_tokens=config.max_snapshot_tokens,
            max_text_chars=min(MAX_TEXT_CHARS, config.max_page_bytes // 4),
            max_page_bytes=config.max_page_bytes,
            max_screenshot_width=config.max_screenshot_width,
            max_screenshot_height=config.max_screenshot_height,
            allowed_domains=config.allowed_domains,
            blocked_domains=config.blocked_domains,
            download_dir=config.download_dir,
            max_download_mb=config.max_download_mb,
            dns_cache_ttl=config.dns_cache_ttl_seconds,
            max_sessions=config.max_sessions,
            session_idle_ttl=config.session_idle_ttl_seconds,
        )

    @property
    def snapshot_budget_chars(self) -> int:
        return max(400, self.max_snapshot_tokens * 4)


def build_backend(settings: BrowserSettings) -> BrowserBackend:
    chosen = (
        resolve_backend(settings, termux=is_termux())
        if settings.backend == "auto"
        else settings.backend
    )
    if chosen == "playwright":
        if not playwright_available():
            raise ToolError(
                "browser.backend is 'playwright' but Playwright is not installed; run "
                "`pip install -e '.[browser]' && playwright install chromium`, or set "
                'browser.backend = "cdp"'
            )
        backend: BrowserBackend = PlaywrightBackend(settings)
    elif chosen in {"cdp", "remote", "attach"}:
        backend = CdpBackend(settings, variant=chosen)
    else:
        raise ToolError(f"unknown browser backend '{chosen}'")
    return backend


@dataclass(slots=True)
class PendingDownload:
    token: str
    suggested_name: str
    url: str
    state: str = "started"
    source: Any = None


class BrowserRunSession:
    def __init__(
        self,
        manager: BrowserManager,
        run_key: str,
        workspace: Path,
        page: BrowserPage,
    ) -> None:
        self.manager = manager
        self.run_key = run_key
        self.workspace = workspace
        self.pages: list[BrowserPage] = [page]
        self.active = page
        self.resolver = RefResolver()
        self.last_elements: list[ElementInfo] = []
        self.last_title = ""
        self.last_url = ""
        self.lock = asyncio.Lock()
        self.created_at = time.monotonic()
        self.last_used = self.created_at
        self.secrets: list[str] = []
        self.pending_downloads: dict[str, PendingDownload] = {}
        self.notes: list[str] = []
        if hasattr(page, "set_timeout"):
            page.set_timeout(manager.settings.interaction_timeout)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def note(self, message: str) -> None:
        if message and message not in self.notes:
            self.notes.append(message)

    def drain_notes(self) -> list[str]:
        notes = list(self.notes)
        self.notes.clear()
        return notes

    async def adopt(self, page: BrowserPage, opener: Any = None) -> None:
        if page in self.pages:
            return
        self.pages.append(page)
        self.active = page
        self.note("a new tab opened and became active")
        await self._settle(page)

    async def _settle(self, page: BrowserPage) -> None:
        await asyncio.sleep(0.15)
        with contextlib.suppress(Exception):
            await page.wait_load(min(self.manager.settings.navigation_timeout, 5.0))
        await page.wait_network_idle(min(self.manager.settings.navigation_timeout, 5.0))

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "••••••")
        return text

    def remember_secret(self, value: str) -> str:
        self.secrets.append(value)
        return "••••••"

    def resolve_secret(self, text: str) -> str:
        import os

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            value = os.environ.get(f"EURUS_SECRET_{name}", "")
            if not value:
                raise ToolError(
                    f"secret '{name}' is not set; provide it via the EURUS_SECRET_{name} "
                    "environment variable"
                )
            self.remember_secret(value)
            return value

        return re.sub(r"\{\{secret:([A-Za-z0-9_-]+)\}\}", _replace, text)

    async def current_elements(
        self, page: BrowserPage | None = None, remember: bool = True
    ) -> list[ElementInfo]:
        target = page or self.active
        elements = await target.collect_elements(self.manager.settings.element_cap)
        self.last_elements = elements
        if remember:
            self.resolver.remember(elements)
        self.last_title = self.redact(await target.title())[:300]
        self.last_url = await target.url()
        return elements

    async def snapshot(self, header: str = "") -> str:
        elements = await self.current_elements()
        return format_snapshot(
            elements,
            title=self.last_title,
            url=self.last_url,
            budget_chars=self.manager.settings.snapshot_budget_chars,
            header=header,
        )

    async def resolve(self, ref: str) -> ElementInfo:
        elements = await self.current_elements(remember=False)
        try:
            element, recovered = self.resolver.resolve(ref, elements)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if recovered:
            self.note(
                f"reference {ref} matched a moved element; its new reference is {element.ref}"
            )
        return element

    async def navigate(self, url: str) -> dict[str, Any]:
        guard = self.manager.url_guard
        try:
            check = await guard.check(url)
        except BrowserPolicyError as exc:
            raise ToolError(str(exc)) from exc
        outcome = await self.active.goto(check.url, self.manager.settings.navigation_timeout)
        await self._settle(self.active)
        if outcome.status >= 400:
            self.note(f"the site returned HTTP {outcome.status}")
        return {
            "url": outcome.url,
            "status": outcome.status,
            "title": self.redact(await self.active.title())[:300],
        }

    async def click(self, ref: str) -> dict[str, Any]:
        element = await self.resolve(ref)
        risk = self.manager.classify(element, "click")
        description = describe_click(element.ref, element.role, element.name)
        before_url = await self.active.url()
        before_pages = len(self.pages)
        await self.active.click(element.index, self.manager.settings.interaction_timeout)
        await self._settle(self.active)
        result: dict[str, Any] = {"clicked": description, "risk": risk.level}
        if risk.reason:
            result["risk_reason"] = risk.reason
        current_url = await self.active.url()
        if current_url != before_url:
            result["navigated_to"] = current_url
        if len(self.pages) != before_pages:
            result["note"] = "the click opened a new tab; it is now active"
        await self.current_elements()
        return result

    async def type_text(self, ref: str, text: str) -> dict[str, Any]:
        element = await self.resolve(ref)
        secret = bool(getattr(element, "input_type", "") == "password") or "{{secret:" in text
        resolved = self.resolve_secret(text) if "{{secret:" in text else text
        if secret and resolved not in self.secrets:
            self.remember_secret(resolved)
        self.manager.classify(element, "type")
        description = describe_type(element.ref, element.role, element.name, text, secret)
        await self.active.fill(element.index, resolved, self.manager.settings.interaction_timeout)
        await self.current_elements()
        display = "••••••" if secret else text[:80]
        return {"typed_into": description, "value": display}

    async def press_key(self, key: str) -> dict[str, Any]:
        from eurus.browser.backend import parse_key_combo

        parse_key_combo(key)
        await self.active.press_key(key)
        await asyncio.sleep(0.2)
        await self._settle(self.active)
        await self.current_elements()
        return {"pressed": key}

    async def select_option(self, ref: str, values: list[str]) -> dict[str, Any]:
        element = await self.resolve(ref)
        if element.role != "combobox":
            raise ToolError(
                f"element {element.ref} is a {element.role or element.tag}, not a dropdown; "
                "use a combobox reference from the snapshot"
            )
        if element.options and not element.options:
            pass
        selected = await self.active.select_options(
            element.index, values, self.manager.settings.interaction_timeout
        )
        await asyncio.sleep(0.2)
        await self.current_elements()
        return {"selected": selected, "on": describe_click(element.ref, element.role, element.name)}

    async def hover(self, ref: str) -> dict[str, Any]:
        element = await self.resolve(ref)
        await self.active.hover(element.index, self.manager.settings.interaction_timeout)
        return {"hovered": describe_click(element.ref, element.role, element.name)}

    async def scroll(self, direction: str, amount: int) -> dict[str, Any]:
        deltas: dict[str, tuple[int, int]] = {
            "up": (0, -amount),
            "down": (0, amount),
            "left": (-amount, 0),
            "right": (amount, 0),
            "top": (0, -1_000_000),
            "bottom": (0, 1_000_000),
        }
        if direction not in deltas:
            raise ToolError("direction must be one of up, down, left, right, top, bottom")
        dx, dy = deltas[direction]
        await self.active.scroll(dx, dy)
        position = await self.active.evaluate("window.scrollY || 0")
        return {"scrolled": direction, "amount": amount, "scroll_y": int(position or 0)}

    async def wait_for(self, condition: str, value: str, seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.5, min(seconds, 60.0))
        if condition == "time":
            await asyncio.sleep(max(0.5, min(seconds, 30.0)))
            return {"waited": seconds}
        while time.monotonic() < deadline:
            if condition == "text":
                body = await self.active.text_content(MAX_TEXT_CHARS)
                if value.casefold() in body.casefold():
                    return {"condition": "text", "found": True}
            elif condition == "text_gone":
                body = await self.active.text_content(MAX_TEXT_CHARS)
                if value.casefold() not in body.casefold():
                    return {"condition": "text_gone", "found": True}
            elif condition == "url_contains":
                if value in await self.active.url():
                    return {"condition": "url_contains", "found": True}
            elif condition == "idle":
                if await self.active.wait_network_idle(deadline - time.monotonic()):
                    return {"condition": "idle", "found": True}
            else:
                raise ToolError(
                    "condition must be one of text, text_gone, url_contains, idle, time"
                )
            await asyncio.sleep(0.25)
        raise ToolError(f"condition '{condition}' was not met within {seconds:g}s")

    async def back(self) -> dict[str, Any]:
        await self.active.evaluate("window.history.back()")
        await asyncio.sleep(0.3)
        await self._settle(self.active)
        return {"url": await self.active.url()}

    async def tabs(self, action: str, index: int) -> dict[str, Any]:
        if action == "list":
            listing = []
            active_index = 0
            for position, page in enumerate(self.pages):
                is_active = page is self.active
                if is_active:
                    active_index = position
                listing.append(
                    {
                        "index": position,
                        "url": await page.url(),
                        "title": self.redact(await page.title())[:120],
                        "active": is_active,
                    }
                )
            return {"tabs": listing, "active": active_index}
        if action == "select":
            if not 0 <= index < len(self.pages):
                raise ToolError(f"tab index must be between 0 and {len(self.pages) - 1}")
            self.active = self.pages[index]
            return {"active_tab": index, "url": await self.active.url()}
        if action == "new":
            page = await self.manager.backend.open_page()
            self.pages.append(page)
            self.active = page
            return {"opened_tab": len(self.pages) - 1, "url": await page.url()}
        if action == "close":
            if len(self.pages) <= 1:
                raise ToolError("the last tab cannot be closed; use browser_close instead")
            if not 0 <= index < len(self.pages):
                raise ToolError(f"tab index must be between 0 and {len(self.pages) - 1}")
            closing = self.pages.pop(index)
            await closing.close()
            if self.active is closing:
                self.active = self.pages[min(index, len(self.pages) - 1)]
            return {"closed_tab": index, "active_url": await self.active.url()}
        raise ToolError("action must be one of list, select, new, close")

    async def set_dialog_policy(self, action: str) -> dict[str, Any]:
        if action not in {"accept", "dismiss"}:
            raise ToolError("action must be 'accept' or 'dismiss'")
        page = self.active
        if hasattr(page, "dialog_policy"):
            page.dialog_policy = "accept_once" if action == "accept" else "dismiss"
        return {
            "dialog_policy": action,
            "note": "the next dialog on the active tab will be handled accordingly",
        }

    async def dialog_state(self) -> dict[str, Any]:
        page = self.active
        last = getattr(page, "last_dialog", None)
        return {"last_dialog": dict(last) if last else None}

    async def screenshot(self, path: Path, width: int, height: int) -> dict[str, Any]:
        data = await self.active.screenshot_bytes(width, height)
        if not data:
            raise ToolError("the browser returned an empty screenshot")
        limit = self.manager.settings.max_screenshot_mb * 1024 * 1024
        if len(data) > limit:
            raise ToolError(
                f"screenshot is {len(data) // 1024} KiB, above the configured limit; "
                "reduce width and height"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"screenshot": str(path), "bytes": len(data)}

    async def upload(self, ref: str, files: list[Path]) -> dict[str, Any]:
        element = await self.resolve(ref)
        await self.active.upload(element.index, files, self.manager.settings.interaction_timeout)
        return {
            "uploaded_to": describe_click(element.ref, element.role, element.name),
            "files": [str(item) for item in files],
        }

    async def read_element(self, ref: str, max_chars: int) -> str:
        element = await self.resolve(ref)
        return self.redact(await self.active.read_text(element.index, max_chars))

    async def page_text(self, max_chars: int) -> str:
        return self.redact(await self.active.text_content(max_chars))

    async def close(self) -> None:
        for page in list(self.pages):
            with contextlib.suppress(Exception):
                await page.close()
        self.pages.clear()


class BrowserManager:
    def __init__(
        self,
        settings: BrowserSettings,
        *,
        workspace: Path | None = None,
        backend: BrowserBackend | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or build_backend(settings)
        self.domain_policy = DomainPolicy(
            allowed_domains=settings.allowed_domains,
            blocked_domains=settings.blocked_domains,
        )
        self.dns_cache = DnsCache(ttl_seconds=settings.dns_cache_ttl)
        self.url_guard = UrlGuard(domain_policy=self.domain_policy, dns_cache=self.dns_cache)
        self.workspace = workspace or Path.cwd()
        self._lock = asyncio.Lock()
        self._sessions: dict[str, BrowserRunSession] = {}
        self._closed = False
        self.dns_cache_entries = 0
        self._hooks = _ManagerHooks(self)
        if hasattr(self.backend, "set_hooks"):
            self.backend.set_hooks(self._hooks)
        if hasattr(self.backend, "set_request_guard"):
            self.backend.set_request_guard(self._guard_request)
        if hasattr(self.backend, "set_download_dir"):
            self.backend.set_download_dir(self.download_root)

    @property
    def download_root(self) -> Path:
        if self.settings.download_dir:
            configured = Path(self.settings.download_dir).expanduser()
            if not configured.is_absolute():
                configured = (self.workspace / configured).resolve()
            return configured
        return (self.workspace / "downloads").resolve()

    def classify(self, element: ElementInfo, action: str) -> Any:
        from eurus.browser.risk import classify_click, classify_type

        if action == "click":
            return classify_click(
                element.role, element.name, element.tag, element.input_type, element.href
            )
        return classify_type(element.role, element.name, element.input_type)

    async def _guard_request(self, url: str) -> None:
        await self.url_guard.check(url)

    async def session_for(self, run_key: str, workspace: Path | None = None) -> BrowserRunSession:
        if self._closed:
            raise ToolError("the browser manager is closed")
        async with self._lock:
            session = self._sessions.get(run_key)
            if session is not None:
                session.touch()
                return session
            await self._evict_locked()
            page = await self.backend.open_page()
            session = BrowserRunSession(
                self, run_key, (workspace or self.workspace).resolve(), page
            )
            self._sessions[run_key] = session
            return session

    async def _evict_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, item in self._sessions.items()
            if now - item.last_used > self.settings.session_idle_ttl
        ]
        for key in stale:
            session = self._sessions.pop(key)
            await self._safe_close_session(session)
        while len(self._sessions) >= self.settings.max_sessions:
            oldest_key = min(self._sessions, key=lambda key: self._sessions[key].last_used)
            session = self._sessions.pop(oldest_key)
            await self._safe_close_session(session)

    async def _safe_close_session(self, session: BrowserRunSession) -> None:
        with contextlib.suppress(Exception):
            await session.close()

    async def close_run(self, run_key: str) -> None:
        async with self._lock:
            session = self._sessions.pop(run_key, None)
        if session is not None:
            await self._safe_close_session(session)

    async def stop(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._safe_close_session(session)
        with contextlib.suppress(Exception):
            await self.backend.stop()

    async def close_all(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop()

    def route_page_opened(self, page: Any, opener: Any) -> None:
        opener_key = getattr(opener, "page", opener)
        target = None
        for session in self._sessions.values():
            if opener_key is not None and any(
                getattr(item, "page", item) is opener_key for item in session.pages
            ):
                target = session
                break
        if target is None and self._sessions:
            target = max(self._sessions.values(), key=lambda item: item.last_used)
        if target is None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(page.close())
            return
        asyncio.get_running_loop().create_task(target.adopt(page, opener))

    def route_page_closed(self, page: Any) -> None:
        for session in self._sessions.values():
            if page in session.pages:
                session.pages.remove(page)
                if session.active is page:
                    if session.pages:
                        session.active = session.pages[-1]
                    else:
                        asyncio.get_running_loop().create_task(self.close_run(session.run_key))
                return

    def route_download_started(self, page: Any, download: Any, handle: DownloadHandle) -> None:
        record = PendingDownload(
            token=handle.token,
            suggested_name=handle.suggested_name,
            url=handle.url,
            source=download,
        )
        for session in self._sessions.values():
            if any(getattr(item, "page", None) is page for item in session.pages):
                session.pending_downloads[handle.token] = record
                return
        for session in self._sessions.values():
            session.pending_downloads[handle.token] = record
            return

    async def save_download(
        self, session: BrowserRunSession, handle: PendingDownload, destination: Path
    ) -> dict[str, Any]:
        max_bytes = self.settings.max_download_mb * 1024 * 1024
        source = handle.source
        if source is None:
            raise ToolError("the download is no longer tracked")
        save = getattr(source, "save_as", None)
        cancel = getattr(source, "cancel", None)
        if save is None:
            raise ToolError("this backend cannot save the download")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.wait_for(save(str(destination)), timeout=self.settings.navigation_timeout)
        except TimeoutError as exc:
            with contextlib.suppress(Exception):
                if cancel:
                    await cancel()
            raise ToolError("the download did not finish in time") from exc
        size = destination.stat().st_size if destination.exists() else 0
        if size > max_bytes:
            with contextlib.suppress(Exception):
                destination.unlink()
            raise ToolError(
                f"download is {size // (1024 * 1024)} MiB, above the configured limit of "
                f"{self.settings.max_download_mb} MiB; the file was discarded"
            )
        return {"file": str(destination), "bytes": size}

    async def download(self, session: BrowserRunSession, url: str, filename: str) -> dict[str, Any]:
        from eurus.browser.playwright_backend import PlaywrightBackend

        try:
            check = await self.url_guard.check(url)
        except BrowserPolicyError as exc:
            raise ToolError(str(exc)) from exc
        destination_root = self.download_root
        destination_root.mkdir(parents=True, exist_ok=True)
        resolved_root = destination_root.resolve()
        name = sanitized_filename(Path(filename or "").name or "") or ""
        if isinstance(self.backend, PlaywrightBackend):
            page = await self.backend.open_page()
            session.pages.append(page)
            try:
                async with page.page.expect_download(
                    timeout=int(self.settings.navigation_timeout * 1000)
                ) as download_info:
                    with contextlib.suppress(Exception):
                        await page.page.goto(
                            check.url, timeout=int(self.settings.navigation_timeout * 1000)
                        )
                download = await download_info.value
                suggested = str(getattr(download, "suggested_filename", "") or "download.bin")
                target_name = sanitized_filename(name or suggested)
                destination = (resolved_root / target_name).resolve()
                if not destination.is_relative_to(resolved_root):
                    raise ToolError("download destination escapes the download directory")
                result = await self.save_download(
                    session,
                    PendingDownload(
                        token=str(id(download)),
                        suggested_name=suggested,
                        url=check.url,
                        source=download,
                    ),
                    destination,
                )
            finally:
                with contextlib.suppress(Exception):
                    await page.close()
                with contextlib.suppress(ValueError):
                    session.pages.remove(page)
            return {**result, "url": check.url}
        quarantine = getattr(self.backend, "_download_dir", None) or resolved_root
        quarantine = Path(quarantine)
        quarantine.mkdir(parents=True, exist_ok=True)
        before = {item.name for item in quarantine.iterdir()}
        page = await self.backend.open_page()
        session.pages.append(page)
        try:
            with contextlib.suppress(Exception):
                await page.goto(check.url, self.settings.navigation_timeout)
            deadline = time.monotonic() + self.settings.navigation_timeout
            new_file: Path | None = None
            while time.monotonic() < deadline:
                candidates = [
                    item
                    for item in quarantine.iterdir()
                    if item.name not in before and not item.name.endswith(".crdownload")
                ]
                if candidates:
                    candidate = max(candidates, key=lambda item: item.stat().st_mtime)
                    size = candidate.stat().st_size
                    await asyncio.sleep(0.5)
                    if candidate.stat().st_size == size:
                        new_file = candidate
                        break
                await asyncio.sleep(0.4)
        finally:
            with contextlib.suppress(Exception):
                await page.close()
            with contextlib.suppress(ValueError):
                session.pages.remove(page)
        if new_file is None:
            raise ToolError(
                "no download appeared within the timeout; the URL may not be a downloadable file"
            )
        max_bytes = self.settings.max_download_mb * 1024 * 1024
        if new_file.stat().st_size > max_bytes:
            with contextlib.suppress(Exception):
                new_file.unlink()
            raise ToolError(
                f"download exceeds the configured limit of {self.settings.max_download_mb} MiB; "
                "the file was discarded"
            )
        target_name = sanitized_filename(name or new_file.name)
        destination = (resolved_root / target_name).resolve()
        if not destination.is_relative_to(resolved_root):
            raise ToolError("download destination escapes the download directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            new_file.replace(destination)
        return {"file": str(destination), "bytes": destination.stat().st_size, "url": check.url}

    def status(self) -> dict[str, Any]:
        return {
            "backend": getattr(self.backend, "variant", getattr(self.backend, "name", "unknown")),
            "connected": self.backend.connected,
            "owns_process": self.backend.owns_process(),
            "sessions": {
                key: {
                    "tabs": len(item.pages),
                    "active_url": item.last_url,
                    "idle_seconds": round(time.monotonic() - item.last_used, 1),
                }
                for key, item in self._sessions.items()
            },
            "profile_dir": self.settings.profile_dir,
            "download_dir": str(self.download_root),
        }


class _ManagerHooks:
    def __init__(self, manager: BrowserManager) -> None:
        self.manager = manager

    def page_opened(self, page: Any, opener: Any) -> None:
        self.manager.route_page_opened(page, opener)

    def download_started(self, page: Any, download: Any, handle: DownloadHandle) -> None:
        self.manager.route_download_started(page, download, handle)

    def download_progress(self, token: str, state: str, received: str) -> None:
        return None

    def page_closed(self, page: Any) -> None:
        self.manager.route_page_closed(page)

    def page_info_changed(self, page: Any) -> None:
        return None
