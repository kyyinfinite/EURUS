from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.config import BrowserConfig

SELECTOR_PREFIX = "css="


@dataclass(frozen=True, slots=True)
class NavigationOutcome:
    url: str
    status: int = 0
    redirected: bool = False


@dataclass(frozen=True, slots=True)
class ElementInfo:
    ref: str
    index: int
    role: str
    name: str
    tag: str
    value: str = ""
    href: str = ""
    input_type: str = ""
    checked: bool | None = None
    required: bool = False
    disabled: bool = False
    placeholder: str = ""
    level: int = 0
    options: tuple[str, ...] = ()
    signature: str = ""


@dataclass(frozen=True, slots=True)
class TabInfo:
    index: int
    url: str
    title: str
    active: bool


@dataclass(frozen=True, slots=True)
class DownloadHandle:
    suggested_name: str = ""
    url: str = ""
    token: str = ""


@dataclass(slots=True)
class DialogRecord:
    message: str = ""
    dialog_type: str = ""
    url: str = ""
    handled: bool = False


class BrowserBackend(ABC):
    name: str = "base"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def owns_process(self) -> bool: ...

    @abstractmethod
    async def open_page(self) -> Any: ...

    @abstractmethod
    async def status(self) -> dict[str, Any]: ...


class BrowserPage(ABC):
    @abstractmethod
    async def url(self) -> str: ...

    @abstractmethod
    async def title(self) -> str: ...

    @abstractmethod
    async def goto(self, url: str, timeout: float) -> NavigationOutcome: ...

    @abstractmethod
    async def text_content(self, max_chars: int) -> str: ...

    @abstractmethod
    async def collect_elements(self, max_elements: int) -> list[ElementInfo]: ...

    @abstractmethod
    async def element_handle(self, index: int) -> Any: ...

    @abstractmethod
    async def click(self, index: int, timeout: float) -> None: ...

    @abstractmethod
    async def fill(self, index: int, text: str, timeout: float) -> None: ...

    @abstractmethod
    async def press_key(self, key: str) -> None: ...

    @abstractmethod
    async def select_options(self, index: int, values: list[str], timeout: float) -> list[str]: ...

    @abstractmethod
    async def hover(self, index: int, timeout: float) -> None: ...

    @abstractmethod
    async def scroll(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def screenshot_bytes(self, width: int, height: int) -> bytes: ...

    @abstractmethod
    async def upload(self, index: int, files: list[Path], timeout: float) -> None: ...

    @abstractmethod
    async def evaluate(self, expression: str) -> Any: ...

    @abstractmethod
    async def wait_load(self, timeout: float) -> None: ...

    @abstractmethod
    async def wait_network_idle(self, timeout: float, idle_seconds: float = 0.5) -> bool: ...

    @abstractmethod
    async def content_bytes(self, max_bytes: int) -> bytes: ...

    @abstractmethod
    async def read_text(self, index: int, max_chars: int) -> str: ...

    @abstractmethod
    async def extract_table(self, index: int) -> list[list[str]]: ...

    @abstractmethod
    async def click_element_by_selector(self, selector: str) -> None: ...

    @abstractmethod
    async def fill_by_selector(self, selector: str, text: str) -> None: ...

    @abstractmethod
    async def read_by_selector(self, selector: str) -> str: ...

    @abstractmethod
    async def close(self) -> None: ...


PageOpenedCallback = Callable[[Any, Any], None]


@dataclass(slots=True)
class BackendHooks:
    page_opened: PageOpenedCallback | None = None
    download_started: Callable[[Any, DownloadHandle], None] | None = None
    download_progress: Callable[[str, str, str], None] | None = None


DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800

KEY_CODES: dict[str, tuple[int, str, str]] = {
    "enter": (13, "Enter", "\r"),
    "tab": (9, "Tab", ""),
    "escape": (27, "Escape", ""),
    "backspace": (8, "Backspace", ""),
    "delete": (46, "Delete", ""),
    "space": (32, "Space", " "),
    "arrowup": (38, "ArrowUp", ""),
    "arrowdown": (40, "ArrowDown", ""),
    "arrowleft": (37, "ArrowLeft", ""),
    "arrowright": (39, "ArrowRight", ""),
    "home": (36, "Home", ""),
    "end": (35, "End", ""),
    "pageup": (33, "PageUp", ""),
    "pagedown": (34, "PageDown", ""),
}

MODIFIER_BITS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "shift": 8}

SUPPORTED_KEYS = (
    "Enter, Tab, Escape, Backspace, Delete, Space, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, "
    "Home, End, PageUp, PageDown, a single character, or combinations like Control+a"
)


def parse_key_combo(combo: str) -> dict[str, Any]:
    from eurus.errors import ToolError

    parts = [part.strip() for part in combo.split("+") if part.strip()]
    if not parts:
        raise ToolError("key must not be empty")
    modifiers = 0
    for part in parts[:-1]:
        bit = MODIFIER_BITS.get(part.lower())
        if bit is None:
            raise ToolError(f"unsupported key modifier '{part}'")
        modifiers |= bit
    final = parts[-1]
    named = KEY_CODES.get(final.lower())
    if named is not None:
        virtual, code, text = named
        return {"key": code, "code": code, "virtual": virtual, "text": text, "modifiers": modifiers}
    if len(final) == 1:
        upper = final.upper()
        code = f"Key{upper}" if upper.isalpha() else f"Digit{final}" if final.isdigit() else ""
        return {
            "key": final,
            "code": code,
            "virtual": ord(upper),
            "text": final,
            "modifiers": modifiers,
        }
    raise ToolError(f"unsupported key '{final}'; use {SUPPORTED_KEYS}")


def resolve_backend(settings: Any, *, termux: bool) -> str:
    requested = str(getattr(settings, "backend", "auto")).lower()
    if requested != "auto":
        return requested
    if termux:
        return "cdp"
    try:
        import playwright  # noqa: F401

        return "playwright"
    except ImportError:
        return "cdp"


def backend_error(name: str, missing: str) -> RuntimeError:
    return RuntimeError(
        f"browser backend '{name}' requires {missing}; install it or set browser.backend "
        "to another value in the configuration"
    )


def chromium_candidates() -> list[str]:
    import os
    import shutil

    prefix = os.environ.get("PREFIX", "")
    candidates = []
    if settings_executable := os.environ.get("EURUS_CHROMIUM", "").strip():
        candidates.append(settings_executable)
    for name in ("chromium-browser", "chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if prefix:
        candidates.append(str(Path(prefix) / "bin" / "chromium-browser"))
        candidates.append(str(Path(prefix) / "bin" / "chromium"))
    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.is_dir():
        try:
            for child in sorted(cache.iterdir(), reverse=True):
                if child.name.startswith("chromium-"):
                    for layout in ("chrome-linux64", "chrome-linux"):
                        binary = child / layout / "chrome"
                        if binary.is_file():
                            candidates.append(str(binary))
        except OSError:
            pass
    return candidates


def find_chromium(configured: str = "") -> str:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise RuntimeError(f"browser.chromium_executable does not exist: {configured}")
    for item in chromium_candidates():
        if Path(item).is_file():
            return item
    raise RuntimeError(
        "no Chromium executable found; install chromium (Termux: `pkg install x11-repo "
        "&& pkg install chromium`) or set browser.chromium_executable"
    )


def viewport_limits(settings: BrowserConfig) -> tuple[int, int]:
    return (
        max(320, min(settings.max_screenshot_width, 3840)),
        max(240, min(settings.max_screenshot_height, 2160)),
    )


def sanitized_filename(name: str, default: str = "download.bin") -> str:
    cleaned = "".join(char if char.isalnum() or char in {".", "_", "-"} else "_" for char in name)
    cleaned = cleaned.strip("._") or default
    if len(cleaned) > 120:
        stem, _, suffix = cleaned.rpartition(".")
        cleaned = (stem[:110] or "download") + ("." + suffix if suffix else "")
    return cleaned


@dataclass(slots=True)
class PendingDownload:
    token: str
    suggested_name: str
    url: str
    state: str = "started"
    received: int = 0
    total: int = 0
    path: str = ""
    error: str = ""


@dataclass(slots=True)
class SessionSnapshotData:
    elements: list[ElementInfo] = field(default_factory=list)
    title: str = ""
    url: str = ""
