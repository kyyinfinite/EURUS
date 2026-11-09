from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

from eurus.tui.theme import Theme

TAB_KINDS = ("chat", "editor", "diff", "preview", "output")


@dataclass(frozen=True, slots=True)
class PanelGlyphs:
    collapsed: str
    expanded: str
    running: str
    ok: str
    fail: str
    pending: str
    thinking: str
    rule: str
    splitter: str
    dirty: str
    more: str


def panel_glyphs(theme: Theme) -> PanelGlyphs:
    if theme.unicode:
        return PanelGlyphs(
            collapsed="▸",
            expanded="▾",
            running="●",
            ok="✓",
            fail="✗",
            pending="○",
            thinking="◈",
            rule="─",
            splitter="│",
            dirty="●",
            more="…",
        )
    return PanelGlyphs(
        collapsed=">",
        expanded="v",
        running="*",
        ok="+",
        fail="x",
        pending="o",
        thinking="*",
        rule="-",
        splitter="|",
        dirty="*",
        more="...",
    )


def status_glyph(glyphs: PanelGlyphs, status: str) -> str:
    return {
        "running": glyphs.running,
        "ok": glyphs.ok,
        "error": glyphs.fail,
        "pending": glyphs.pending,
        "allowed": glyphs.ok,
        "denied": glyphs.fail,
        "info": glyphs.more,
        "thinking": glyphs.thinking,
        "done": glyphs.ok,
    }.get(status, glyphs.more)


@dataclass(slots=True)
class WorkspaceTab:
    kind: str
    title: str
    path: str = ""
    dirty: bool = False
    key: str = ""

    def label(self, max_width: int) -> str:
        mark = "*" if self.dirty else ""
        text = f" {self.title}{mark} "
        return text if len(text) <= max_width else text[: max(0, max_width - 1)] + "…"


@dataclass(slots=True)
class TabRegistry:
    tabs: list[WorkspaceTab] = field(default_factory=list)
    index: int = 0
    on_change: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if not self.tabs:
            self.tabs = [WorkspaceTab("chat", "Chat", key="chat")]

    @property
    def active(self) -> WorkspaceTab:
        return self.tabs[self.index]

    def _changed(self) -> None:
        self.index = min(max(self.index, 0), max(len(self.tabs) - 1, 0))
        if self.on_change is not None:
            self.on_change()

    def select(self, index: int) -> None:
        if not self.tabs:
            return
        self.index = min(max(index, 0), len(self.tabs) - 1)
        self._changed()

    def cycle(self, delta: int) -> None:
        if len(self.tabs) < 2:
            return
        self.index = (self.index + delta) % len(self.tabs)
        self._changed()

    def open(self, tab: WorkspaceTab) -> None:
        key = tab.key or tab.path or tab.title
        for index, existing in enumerate(self.tabs):
            if (existing.key or existing.path or existing.title) == key:
                self.index = index
                self._changed()
                return
        self.tabs.append(tab)
        self.index = len(self.tabs) - 1
        self._changed()

    def close(self, index: int | None = None) -> None:
        target = self.index if index is None else index
        if not self.tabs or self.tabs[target].kind == "chat":
            return
        del self.tabs[target]
        self._changed()

    def mark_dirty(self, key: str, dirty: bool) -> None:
        for tab in self.tabs:
            if (tab.key or tab.path) == key:
                tab.dirty = dirty
        self._changed()

    def find(self, key: str) -> WorkspaceTab | None:
        for tab in self.tabs:
            if (tab.key or tab.path) == key:
                return tab
        return None


def render_header(
    theme: Theme,
    workspace_name: str,
    preset: str,
    state_label: str,
    model: str,
    width: int,
) -> StyleAndTextTuples:
    out: StyleAndTextTuples = [
        ("class:header-glyph", ">_ "),
        ("class:header-title", "EURUS"),
        ("class:rule", "  │  "),
        ("class:dim", workspace_name[:40]),
        ("class:rule", "  │  "),
        ("class:footer-mode", preset.upper()),
        ("class:rule", "  │  "),
        ("class:dim", state_label),
    ]
    if model:
        out.append(("class:rule", "  │  "))
        out.append(("class:dim", model[:48]))
    flat = "".join(part[1] for part in out)
    if len(flat) > width and width > 8:
        keep = out[:2]
        keep.append(("class:dim", " … " + preset.upper()))
        return keep
    return out


def render_tab_bar(
    theme: Theme,
    tabs: list[WorkspaceTab],
    active_index: int,
    width: int,
    on_select: Callable[[int], None] | None = None,
    on_close: Callable[[int], None] | None = None,
) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    used = 0
    for index, tab in enumerate(tabs):
        active = index == active_index
        style = "class:tab-active" if active else "class:tab"
        label = tab.label(max(6, width - used - 4)) if index == len(tabs) - 1 else tab.label(24)
        if used + len(label) > width - 3:
            out.append(("class:dim", " …"))
            break
        if on_select is not None:
            handler = _tab_handler(on_select, index)
            out.append((style, label, handler))
        else:
            out.append((style, label))
        used += len(label)
    while used < width - 1:
        out.append(("class:tab-bar-fill", " "))
        used += 1
    return out


def _tab_handler(on_select: Callable[[int], None], index: int) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_UP:
            on_select(index)

    return handler


def render_footer_hints(items: list[tuple[str, str]], width: int) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    used = 0
    for key, action in items:
        need = len(key) + 1 + len(action) + (3 if out else 1)
        if used + need > width:
            break
        if out:
            out.append(("class:dim", "  ·"))
            used += 3
        out.append(("class:key", key))
        out.append(("class:dim", f" {action}"))
        used += len(key) + 1 + len(action)
    out.append(("class:dim", " " * max(width - used - 1, 0)))
    return out


def render_empty_panel(theme: Theme, title: str, message: str, width: int) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    out.append(("class:panel-title", f" {title.upper()}"))
    out.append(("", "\n"))
    out.append(("class:dim", " " + "─" * max(width - 2, 1)))
    out.append(("", "\n"))
    out.append(("class:dim", f" {message[: max(width - 4, 1)]}"))
    return out


def render_panel_title(
    theme: Theme,
    title: str,
    width: int,
    right: str = "",
    focused: bool = False,
) -> StyleAndTextTuples:
    style = "class:panel-title-focused" if focused else "class:panel-title"
    out: StyleAndTextTuples = [(style, f" {title.upper()}"[: max(width - 1, 1)])]
    if right and width >= len(title) + len(right) + 5:
        room = width - len(title) - len(right) - 2
        out.append(("class:dim", " " * room))
        out.append(("class:dim", right))
    return out
