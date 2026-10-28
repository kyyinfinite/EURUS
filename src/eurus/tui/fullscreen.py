from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    CompletionsMenu,
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import AnyContainer, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import DynamicStyle
from rich.console import Console

from eurus import __version__
from eurus.config import Config
from eurus.tui import themes
from eurus.tui.brand import welcome as welcome_art
from eurus.tui.capabilities import detect
from eurus.tui.commands import COMMANDS
from eurus.tui.layout import cell_clip, clip
from eurus.tui.palette import ListOverlay, PaletteItem
from eurus.tui.prefs import ViewPrefs
from eurus.tui.prompt import (
    MAX_INPUT_LINES,
    MENU_ROWS,
    PLACEHOLDER,
    SEARCH_PLACEHOLDER,
    PromptBox,
    fragments,
)
from eurus.tui.renderer import (
    COLOR_SYSTEMS,
    SAMPLE_PROMPTS,
    ColorSystem,
    StreamView,
    TerminalRenderer,
)
from eurus.tui.theme import Theme
from eurus.tui.transcript import TranscriptSink

if TYPE_CHECKING:
    from eurus.tui.session import InteractiveSession

COMPOSER_WIDTH = 86
PALETTE_WIDTH = 54
HEADER_COLUMNS = 60
SCROLL_STEP = 3
INTERRUPT = object()
OVERLAY_MODES = frozenset({"palette", "settings", "theme", "glyphs", "options", "attach"})
OPTIONS_HINT = "enter select  ·  type filter, n/p pages, b back, q quit"
PRIMARY_COMMANDS = frozenset({"/help", "/clear", "/model", "/workspace", "/settings"})
ATTACH_LIMIT = 60


def pane_width(columns: int) -> int:
    return max(columns, 20)


class FullscreenRenderer(TerminalRenderer):
    def __init__(
        self, console: Console, theme: Theme, sink: TranscriptSink, system: ColorSystem | None
    ) -> None:
        super().__init__(console, theme)
        self.sink = sink
        self.color_system = system
        self.stream_view: StreamView | None = None
        self.rich_pages = False

    def clear(self) -> None:
        self.sink.clear()

    def startup(self, config: Config) -> None:
        return None

    @contextlib.contextmanager
    def generating(self) -> Iterator[StreamView]:
        view = StreamView(self.theme, preview=self.stream_preview)
        self.stream_view = view
        try:
            yield view
        finally:
            self.stream_view = None

    def pause_live(self) -> None:
        if self.stream_view is not None:
            self.stream_view.paused = True

    def resume_live(self) -> None:
        if self.stream_view is not None:
            self.stream_view.paused = False


def attach(session: InteractiveSession) -> tuple[FullscreenRenderer, TranscriptSink]:
    size = shutil.get_terminal_size((80, 24))
    sink = TranscriptSink()
    system = COLOR_SYSTEMS[detect().color]
    console = Console(
        file=cast(IO[str], sink),
        force_terminal=True,
        color_system=system,
        no_color=system is None,
        width=pane_width(size.columns),
        height=size.lines,
        highlight=False,
    )
    renderer = FullscreenRenderer(console, session.renderer.theme, sink, system)
    renderer.markdown_enabled = session.renderer.markdown_enabled
    renderer.stream_preview = session.renderer.stream_preview
    session.renderer = renderer
    return renderer, sink


class FullscreenUI(PromptBox):
    def __init__(
        self,
        session: InteractiveSession,
        renderer: FullscreenRenderer,
        sink: TranscriptSink,
    ) -> None:
        self.session = session
        self.renderer = renderer
        self.sink = sink
        self.mode = "palette"
        self.question = ""
        self.items: tuple[tuple[str, str], ...] = ()
        self.follow = True
        self.top = 0
        self.interrupts = 0
        self.task: asyncio.Task[None] | None = None
        self.future: asyncio.Future[Any] | None = None
        self.overlay: ListOverlay | None = None
        self.overlay_allow_raw = False
        self._welcome_cache: dict[tuple[int, int, int, bool], list[str]] = {}
        super().__init__(renderer.theme, self._status, self._hint, COMMANDS)
        self.buffer = Buffer(
            multiline=True,
            completer=self.completer,
            complete_while_typing=True,
            history=self.history,
        )
        self.search_buffer = Buffer(multiline=False)
        self.window = Window(
            FormattedTextControl(self._render_main),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.field = Window(
            BufferControl(buffer=self.buffer),
            height=Dimension(min=1, max=MAX_INPUT_LINES),
            wrap_lines=True,
            dont_extend_height=True,
            style="class:input",
        )
        self.search_field = Window(
            BufferControl(buffer=self.search_buffer),
            height=1,
            dont_extend_height=True,
            style="class:input",
        )
        self.app: Application[None] = self._build()
        sink.on_change = self.app.invalidate
        self._open_palette()

    def _status(self, width: int) -> list[tuple[str, str]]:
        session = self.session
        return self.renderer.status_parts(
            session.state,
            session.config.agent.model,
            Path(session.config.permissions.workspace).expanduser(),
            session.usage,
            bool(session.retry_prompt),
            width,
            mode=session.config.permissions.mode,
            version=__version__,
        )

    def _hint(self, width: int) -> list[tuple[str, str]]:
        if self.mode in OVERLAY_MODES:
            return []
        if self.mode == "ask":
            return self.renderer.hint_parts(width, (("Enter", "answer"), ("Esc", "cancel")))
        if self.mode == "confirm":
            return self.renderer.hint_parts(width, self.items)
        return []

    def _size(self) -> tuple[int, int]:
        size = get_app().output.get_size()
        return int(size.columns), int(size.rows)

    def _height(self) -> int:
        info = self.window.render_info
        if info is not None:
            return max(int(info.window_height), 1)
        return max(self._size()[1] - 8, 3)

    def _limit(self) -> int:
        return max(len(self.sink.lines) - self._height(), 0)

    def _scroll(self, delta: int) -> None:
        current = self._limit() if self.follow else self.top
        self.top = min(max(current + delta, 0), self._limit())
        self.follow = self.top >= self._limit()

    def _is_welcome(self) -> bool:
        return len(self.sink.lines) <= 1 and not self.sink.lines[0]

    def _render_main(self) -> StyleAndTextTuples:
        columns, rows = self._size()
        width = pane_width(columns)
        if self._is_welcome():
            return self._render_welcome(width)
        return self._render_transcript()

    def _render_transcript(self) -> StyleAndTextTuples:
        columns, rows = self._size()
        console = self.renderer.console
        width = pane_width(columns)
        if console.width != width:
            console.width = width
        console.height = rows
        limit = self._limit()
        if self.follow:
            self.top = limit
        self.top = min(max(self.top, 0), limit)
        view = self.sink.lines[self.top : self.top + self._height()]
        out: StyleAndTextTuples = []
        for index, line in enumerate(view):
            out.extend(to_formatted_text(ANSI(line)))
            if index < len(view) - 1:
                out.append(("", "\n"))
        return out

    def _render_welcome(self, width: int) -> StyleAndTextTuples:
        lines = self._welcome_lines(width, self._height())
        out: StyleAndTextTuples = []
        for index, line in enumerate(lines):
            out.extend(to_formatted_text(ANSI(line)))
            if index < len(lines) - 1:
                out.append(("", "\n"))
        return out

    def _welcome_lines(self, width: int, height: int) -> list[str]:
        colored = self.renderer.console.color_system is not None
        key = (width, height, id(self.renderer.theme), colored)
        cached = self._welcome_cache.get(key)
        if cached is not None:
            return cached
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system=self.renderer.color_system,
            no_color=self.renderer.color_system is None,
            width=width,
            height=height,
            highlight=False,
        )
        console.print(welcome_art(width, height, self.theme, color=colored))
        lines = buffer.getvalue().split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        self._welcome_cache.clear()
        self._welcome_cache[key] = lines
        return lines

    def _render_header(self) -> StyleAndTextTuples:
        history = self.session.history
        label = "New Session" if not history else f"{len(history)} messages"
        return [
            ("class:header-glyph", ">_ "),
            ("class:header-title", "EURUS"),
            ("class:rule", "  │  "),
            ("class:dim", label),
        ]

    def _render_header_right(self) -> StyleAndTextTuples:
        return [
            ("class:dim", "EURUS · AI Agent Terminal  "),
            ("class:header-glyph", self.theme.glyphs.spark),
        ]

    def _render_stream(self) -> StyleAndTextTuples:
        view = self.renderer.stream_view
        if view is None:
            return []
        width = pane_width(self._size()[0])
        out: StyleAndTextTuples = []
        color = self.theme.state_style(view.state)
        if view.paused:
            out.append(("class:warn", " approval pending"))
            return out
        out.append((f"fg:{color}", f" {view.frame()} "))
        out.append((f"bold fg:{color}", view.state.value.upper()))
        out.append(("class:dim", f"  {view.elapsed():.0f}s  ·  ctrl+c cancel"))
        if view.preview:
            tail = view.tail(width - 2)
            if tail:
                out.append(("", "\n"))
                out.append(("class:dim", f" {cell_clip(tail, width - 2)}"))
        return out

    def _stream_height(self) -> int:
        view = self.renderer.stream_view
        if view is None:
            return 1
        lines = 1
        if not view.paused and view.preview and view.tail(pane_width(self._size()[0]) - 2):
            lines = 2
        return lines

    def _render_confirm(self) -> StyleAndTextTuples:
        return [("class:warn", f" {self.question}")]

    def _palette_items(self) -> list[PaletteItem]:
        glyph = self.theme.glyphs
        items = [
            PaletteItem(
                "Chat", "Start a new conversation", glyph.enter, "chat", "", "", "", glyph.chat
            ),
            PaletteItem(
                "New Session",
                "Create a new session",
                "Ctrl+N",
                "command",
                "/clear",
                "",
                "",
                glyph.folder,
            ),
            PaletteItem(
                "Switch Model",
                "Change AI model",
                "Ctrl+M",
                "command",
                "/model",
                "",
                "",
                glyph.gear,
            ),
            PaletteItem(
                "Workspaces",
                "Manage workspaces",
                "Ctrl+W",
                "command",
                "/workspace",
                "",
                "",
                glyph.folder,
            ),
            PaletteItem("Settings", "Open settings", "Ctrl+,", "settings", "", "", "", glyph.gear),
            PaletteItem(
                "Theme", "Change terminal theme", "Ctrl+T", "theme", "", "", "", glyph.palette
            ),
            PaletteItem(
                "Help", "Show help and shortcuts", "Ctrl+H", "command", "/help", "", "", glyph.help
            ),
        ]
        for command in COMMANDS:
            if command.name in PRIMARY_COMMANDS or command.name == "/theme":
                continue
            items.append(
                PaletteItem(
                    command.name,
                    command.summary,
                    command.shortcut,
                    "command",
                    command.name,
                    "",
                    "",
                    "",
                )
            )
        for prompt in SAMPLE_PROMPTS:
            items.append(
                PaletteItem(
                    clip(prompt, 44),
                    "Fill the composer with this prompt",
                    "",
                    "sample",
                    prompt,
                    "",
                    "",
                    "",
                )
            )
        return items

    def _settings_items(self) -> list[PaletteItem]:
        prefs = self.session.prefs
        glyph = self.theme.glyphs
        theme_name = themes.active_name().lower()
        glyph_choice = themes.glyph_choice() or "auto"
        return [
            PaletteItem(
                "Theme", "Open the theme list", "", "theme", "", "", theme_name, glyph.palette
            ),
            PaletteItem(
                "Glyphs", "Icon and symbol set", "", "glyphs", "", "", glyph_choice, glyph.gear
            ),
            PaletteItem(
                "Markdown",
                "Render assistant markdown",
                "",
                "toggle",
                "markdown",
                "",
                "on" if prefs.markdown else "off",
                glyph.chat,
            ),
            PaletteItem(
                "Stream preview",
                "Show text while the model works",
                "",
                "toggle",
                "stream_preview",
                "",
                "on" if prefs.stream_preview else "off",
                glyph.search,
            ),
        ]

    def _theme_items(self) -> list[PaletteItem]:
        current = themes.active_name().lower()
        glyph = self.theme.glyphs
        return [
            PaletteItem(
                name,
                themes.describe_theme(name),
                "",
                "theme-name",
                "",
                "",
                "active" if name == current else "",
                glyph.palette,
            )
            for name in themes.available()
        ]

    def _glyph_items(self) -> list[PaletteItem]:
        current = themes.glyph_choice() or "auto"
        glyph = self.theme.glyphs
        sets = ("auto", "unicode", "ascii", "nerd")
        notes = {
            "auto": "Follow the theme and terminal",
            "unicode": "Box drawing and symbols",
            "ascii": "Plain ASCII fallback",
            "nerd": "Nerd Font icons",
        }
        return [
            PaletteItem(
                name,
                notes[name],
                "",
                "glyph-set",
                "",
                "",
                "active" if name == current else "",
                glyph.gear,
            )
            for name in sets
        ]

    def _attach_items(self) -> list[PaletteItem]:
        glyph = self.theme.glyphs
        workspace = Path(self.session.config.permissions.workspace).expanduser()
        try:
            entries = sorted(workspace.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            entries = []
        items = [
            PaletteItem(
                entry.name + ("/" if entry.is_dir() else ""),
                "Insert @path into the prompt",
                "",
                "attach",
                entry.name,
                "",
                "",
                glyph.folder,
            )
            for entry in entries[:ATTACH_LIMIT]
        ]
        if not items:
            items.append(
                PaletteItem("Empty", "The workspace has no files", "", "chat", "", "", "", "")
            )
        return items

    def _open(self, mode: str, items: list[PaletteItem], title: str = "", raw: str = "") -> None:
        previous = self.overlay
        if previous is not None:
            previous.detach()
        self.mode = mode
        self.overlay_allow_raw = mode == "options"
        self.overlay = ListOverlay(
            items,
            self.theme,
            title,
            buffer=self.search_buffer,
            raw_hint=raw,
        )
        self.search_buffer.reset()
        if self.app.is_running:
            self.app.layout.focus(self.search_field)
        self.app.invalidate()

    def _close_overlay(self) -> None:
        if self.mode in OVERLAY_MODES and self.future is not None and not self.future.done():
            self.future.set_result(None)
        if self.overlay is not None:
            self.overlay.detach()
        self.mode = "chat"
        self.overlay = None
        self.overlay_allow_raw = False
        self.search_buffer.reset()
        if self.app.is_running:
            self.app.layout.focus(self.field)
        self.app.invalidate()

    def _open_palette(self) -> None:
        self._open("palette", self._palette_items())

    def open_settings(self) -> None:
        self._open("settings", self._settings_items())

    def open_theme(self) -> None:
        self._open("theme", self._theme_items())

    def open_glyphs(self) -> None:
        self._open("glyphs", self._glyph_items())

    def open_attach(self) -> None:
        self._open("attach", self._attach_items())

    def _refresh_overlay(self) -> None:
        overlay = self.overlay
        if overlay is None:
            return
        builders: dict[str, Any] = {
            "palette": self._palette_items,
            "settings": self._settings_items,
            "theme": self._theme_items,
            "glyphs": self._glyph_items,
            "attach": self._attach_items,
        }
        builder = builders.get(self.mode)
        if builder is not None:
            overlay.set_items(builder())
        self.app.invalidate()

    def _toggle_setting(self, key: str) -> None:
        prefs = self.session.prefs
        if not isinstance(prefs, ViewPrefs):
            return
        self.session.set_pref(key, not getattr(prefs, key))

    def _overlay_enter(self) -> None:
        overlay = self.overlay
        if overlay is None:
            self._close_overlay()
            return
        item = overlay.selected()
        if item is None:
            if self.overlay_allow_raw:
                raw = overlay.query.strip()
                self._settle(raw if raw else None)
            else:
                self._close_overlay()
            return
        action = item.action
        if self.mode == "options":
            self._settle(item.label)
            return
        if action == "chat":
            self._close_overlay()
            return
        if action == "attach":
            self._close_overlay()
            self.buffer.insert_text(f"@{item.payload} ")
            return
        if action == "command":
            self._close_overlay()
            self.dispatch(item.payload)
            return
        if action == "settings":
            self.open_settings()
            return
        if action == "theme":
            self.open_theme()
            return
        if action == "glyphs":
            self.open_glyphs()
            return
        if action == "toggle":
            self._toggle_setting(item.payload)
            self._refresh_overlay()
            return
        if action == "theme-name":
            self._close_overlay()
            self.dispatch(f"/theme {item.label}")
            return
        if action == "glyph-set":
            self._close_overlay()
            self.dispatch(f"/glyphs {item.label}")
            return
        if action == "sample":
            self._close_overlay()
            self.buffer.insert_text(item.payload)
            return
        self._close_overlay()

    def _overlay_escape(self) -> None:
        if self.mode == "options":
            self._settle(None)
            return
        self._close_overlay()

    def _overlay_page(self, delta: int) -> None:
        overlay = self.overlay
        if overlay is not None:
            overlay.move(delta)

    def _composer_width(self) -> int:
        return max(min(COMPOSER_WIDTH, self._size()[0] - 4), 20)

    def _palette_width(self) -> int:
        return max(min(PALETTE_WIDTH, self._size()[0] - 4), 24)

    def _pad_width(self, box: int) -> AnyContainer:
        def dimension() -> Dimension:
            return Dimension.exact(max((self._size()[0] - box) // 2, 0))

        return Window(width=dimension, dont_extend_width=True)

    def _composer(self) -> AnyContainer:
        chars = self.chars
        width = self._composer_width()
        glyph = Window(
            FormattedTextControl(self._render_glyph),
            width=lambda: Dimension.exact(len(self._label()) + 1),
            height=1,
            dont_extend_height=True,
        )
        placeholder = Window(
            FormattedTextControl([("class:placeholder", PLACEHOLDER)]),
            width=lambda: Dimension.exact(self._placeholder_width()),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
        )
        hints: StyleAndTextTuples = [
            ("class:dim", "Attach"),
            ("class:rule", "  │  "),
            ("class:key", "Ctrl+Enter"),
            ("class:dim", " Send "),
        ]
        top = VSplit(
            [
                Window(width=1, char=chars[0], style="class:composer-corner"),
                Window(char=chars[4], style="class:border"),
                Window(width=1, char=chars[1], style="class:border"),
            ],
            height=1,
        )
        input_row = VSplit(
            [
                Window(width=1, char=chars[5], style="class:composer-accent"),
                Window(width=1, char=" ", style="class:composer-accent"),
                glyph,
                placeholder,
                self.field,
                Window(width=1, char=chars[5], style="class:border"),
            ]
        )
        hint_row = VSplit(
            [
                Window(width=1, char=chars[5], style="class:composer-accent"),
                Window(width=1, char=" ", style="class:composer-accent"),
                Window(
                    FormattedTextControl(lambda: hints),
                    dont_extend_height=True,
                    wrap_lines=False,
                    align=WindowAlign.RIGHT,
                ),
                Window(width=1, char=chars[5], style="class:border"),
            ]
        )
        bottom = VSplit(
            [
                Window(width=1, char=chars[2], style="class:composer-corner"),
                Window(char=chars[4], style="class:border"),
                Window(width=1, char=chars[3], style="class:border"),
            ],
            height=1,
        )
        box = HSplit([top, input_row, hint_row, bottom], width=lambda: Dimension.exact(width))
        return VSplit(
            [self._pad_width(width), box, self._pad_width(width)],
            height=4,
        )

    def _overlay_container(self) -> AnyContainer:
        chars = self.chars
        border = "class:palette-border"
        width = self._palette_width()
        glyph = self.theme.glyphs
        placeholder = Window(
            FormattedTextControl(self._search_placeholder),
            width=lambda: Dimension.exact(self._search_placeholder_width()),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
        )
        search_input = VSplit(
            [
                Window(width=1, char=chars[5], style=border),
                Window(width=1, char=" ", style=border),
                Window(
                    FormattedTextControl([("class:palette-icon", f"{glyph.search} ")]),
                    width=2,
                    height=1,
                    dont_extend_height=True,
                ),
                placeholder,
                self.search_field,
                Window(width=1, char=" ", style=border),
                Window(width=1, char=chars[5], style=border),
            ],
            height=1,
        )
        items = Window(
            FormattedTextControl(self._render_overlay_items),
            height=lambda: Dimension.exact(self._overlay_items_height()),
            wrap_lines=False,
        )
        rows: list[AnyContainer] = [
            VSplit(
                [
                    Window(width=1, char=chars[0], style=border),
                    Window(char=chars[4], style=border),
                    Window(width=1, char=chars[1], style=border),
                ],
                height=1,
            ),
            search_input,
            VSplit(
                [
                    Window(width=1, char=chars[5], style=border),
                    Window(char=chars[4], style=border),
                    Window(width=1, char=chars[5], style=border),
                ],
                height=1,
            ),
            VSplit(
                [
                    Window(width=1, char=chars[5], style=border),
                    items,
                    Window(width=1, char=chars[5], style=border),
                ]
            ),
        ]
        if self.overlay is not None and self.overlay.raw_hint:
            rows.append(
                VSplit(
                    [
                        Window(width=1, char=chars[5], style=border),
                        Window(
                            FormattedTextControl(
                                [("class:palette-desc", f" {self.overlay.raw_hint}")]
                            ),
                            wrap_lines=False,
                        ),
                        Window(width=1, char=chars[5], style=border),
                    ],
                    height=1,
                )
            )
        rows.append(
            VSplit(
                [
                    Window(width=1, char=chars[2], style=border),
                    Window(char=chars[4], style=border),
                    Window(width=1, char=chars[3], style=border),
                ],
                height=1,
            )
        )
        return HSplit(rows, width=lambda: Dimension.exact(width))

    def _search_placeholder(self) -> StyleAndTextTuples:
        overlay = self.overlay
        text = SEARCH_PLACEHOLDER
        if overlay is not None and self.mode == "options" and overlay.title:
            text = f"{overlay.title} type to filter or enter a value"
        return [("class:placeholder", text)]

    def _search_placeholder_width(self) -> int:
        if self.search_buffer.text:
            return 0
        overlay = self.overlay
        if overlay is not None and self.mode == "options" and overlay.title:
            return len(overlay.title) + 30
        return len(SEARCH_PLACEHOLDER)

    def _render_overlay_items(self) -> StyleAndTextTuples:
        overlay = self.overlay
        if overlay is None:
            return []
        return overlay.render(self._palette_width() - 2)

    def _overlay_items_height(self) -> int:
        overlay = self.overlay
        if overlay is None:
            return 1
        return max(overlay.line_count(), 1)

    def _label(self) -> str:
        return self.question if self.mode == "ask" else self.theme.glyphs.prompt

    def _render_glyph(self) -> StyleAndTextTuples:
        return [("class:glyph", f"{self._label()} ")]

    def _placeholder_width(self) -> int:
        if self.buffer.text or self.mode in {"ask", "confirm"}:
            return 0
        return max(min(len(PLACEHOLDER), self._composer_width() - 12), 0)

    def _build(self) -> Application[None]:
        confirming = Condition(lambda: self.mode == "confirm")
        asking = Condition(lambda: self.mode in {"ask", "confirm"})
        overlay = Condition(lambda: self.mode in OVERLAY_MODES)
        streaming = Condition(lambda: self.renderer.stream_view is not None)
        header_right = Condition(lambda: self._size()[0] >= HEADER_COLUMNS)
        chars = self.chars
        header = HSplit(
            [
                VSplit(
                    [
                        Window(
                            FormattedTextControl(self._render_header),
                            height=1,
                            wrap_lines=False,
                            style="class:header-bar",
                        ),
                        ConditionalContainer(
                            Window(
                                FormattedTextControl(self._render_header_right),
                                dont_extend_height=True,
                                height=1,
                                wrap_lines=False,
                                style="class:header-bar",
                                align=WindowAlign.RIGHT,
                            ),
                            filter=header_right,
                        ),
                    ],
                    height=1,
                    style="class:header-bar",
                ),
                Window(height=1, char=chars[4], style="class:rule"),
            ]
        )
        footer = HSplit(
            [
                Window(height=1, char=chars[4], style="class:rule"),
                Window(
                    FormattedTextControl(lambda: fragments(self.status(self._size()[0]))),
                    height=1,
                    style="class:footer",
                ),
            ]
        )
        overlay_box = ConditionalContainer(self._overlay_container(), filter=overlay)
        stream_box = ConditionalContainer(
            Window(
                FormattedTextControl(self._render_stream),
                height=lambda: Dimension.exact(self._stream_height()),
                wrap_lines=False,
            ),
            filter=streaming,
        )
        bar = ConditionalContainer(
            Window(FormattedTextControl(self._render_confirm), height=1), filter=confirming
        )
        hint_bar = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: fragments(self.hint(self._size()[0]))),
                height=1,
            ),
            filter=asking,
        )
        roomy = Condition(lambda: self._size()[1] >= 26)
        gap = ConditionalContainer(HSplit([Window(height=1), Window(height=1)]), filter=roomy)
        root = FloatContainer(
            content=HSplit(
                [
                    header,
                    self.window,
                    stream_box,
                    bar,
                    hint_bar,
                    self._composer(),
                    gap,
                    footer,
                ]
            ),
            floats=[
                Float(
                    content=overlay_box,
                    top=10,
                    width=lambda: self._palette_width(),
                ),
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=MENU_ROWS, scroll_offset=1),
                ),
            ],
        )
        focused = self.search_field if self.mode in OVERLAY_MODES else self.field
        return Application(
            layout=Layout(root, focused_element=focused),
            key_bindings=self._keys(),
            style=DynamicStyle(lambda: self.style),
            full_screen=True,
            refresh_interval=0.25,
        )

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()
        confirming = Condition(lambda: self.mode == "confirm")
        asking = Condition(lambda: self.mode in {"ask", "confirm"})
        typing = Condition(lambda: self.mode not in {"confirm", *OVERLAY_MODES})
        empty = Condition(lambda: not self.buffer.text)
        overlay = Condition(lambda: self.mode in OVERLAY_MODES)
        free = Condition(
            lambda: (
                self.mode not in {"confirm", *OVERLAY_MODES}
                and not (self.task is not None and not self.task.done())
            )
        )

        @keys.add("enter", filter=overlay, eager=True)
        def overlay_enter(event: KeyPressEvent) -> None:
            self._overlay_enter()

        @keys.add("escape", filter=overlay, eager=True)
        def overlay_escape(event: KeyPressEvent) -> None:
            self._overlay_escape()

        @keys.add("up", filter=overlay, eager=True)
        def overlay_up(event: KeyPressEvent) -> None:
            if self.overlay is not None:
                self.overlay.move(-1)

        @keys.add("down", filter=overlay, eager=True)
        def overlay_down(event: KeyPressEvent) -> None:
            if self.overlay is not None:
                self.overlay.move(1)

        @keys.add("pageup", filter=overlay, eager=True)
        def overlay_page_up(event: KeyPressEvent) -> None:
            self._overlay_page(-5)

        @keys.add("pagedown", filter=overlay, eager=True)
        def overlay_page_down(event: KeyPressEvent) -> None:
            self._overlay_page(5)

        @keys.add("tab", filter=overlay, eager=True)
        def overlay_tab(event: KeyPressEvent) -> None:
            if self.overlay is not None:
                self.overlay.move(1)

        @keys.add("c-c", filter=overlay, eager=True)
        def overlay_interrupt(event: KeyPressEvent) -> None:
            self._overlay_escape()

        @keys.add("c-d", filter=overlay, eager=True)
        def overlay_eof(event: KeyPressEvent) -> None:
            self._overlay_escape()

        @keys.add("c-k", filter=free, eager=True)
        def palette_key(event: KeyPressEvent) -> None:
            if self.mode in OVERLAY_MODES:
                self._close_overlay()
            else:
                self._open_palette()

        @keys.add("c-n", filter=free, eager=True)
        def new_session(event: KeyPressEvent) -> None:
            self.dispatch("/clear")

        @keys.add("c-p", filter=free, eager=True)
        def switch_model(event: KeyPressEvent) -> None:
            self.dispatch("/model")

        @keys.add("c-w", filter=free, eager=True)
        def workspaces(event: KeyPressEvent) -> None:
            self.dispatch("/workspace")

        @keys.add("c-o", filter=free, eager=True)
        def settings(event: KeyPressEvent) -> None:
            self.open_settings()

        @keys.add("c-t", filter=free, eager=True)
        def theme(event: KeyPressEvent) -> None:
            self.open_theme()

        @keys.add("f1", filter=free, eager=True)
        def help_key(event: KeyPressEvent) -> None:
            self.dispatch("/help")

        @keys.add("escape", "a", filter=free, eager=True)
        def attach_key(event: KeyPressEvent) -> None:
            self.open_attach()

        @keys.add(Keys.Any, filter=confirming)
        def swallow(event: KeyPressEvent) -> None:
            return None

        for key in ("y", "a", "Y", "A"):
            keys.add(key, filter=confirming, eager=True)(lambda event: self._settle(True))
        for key in ("n", "d", "N", "D"):
            keys.add(key, filter=confirming, eager=True)(lambda event: self._settle(False))

        @keys.add("enter", filter=typing)
        def enter(event: KeyPressEvent) -> None:
            if self.mode == "confirm":
                self._settle(False)
                return
            text = self.buffer.text
            if text.endswith("\\"):
                self.buffer.text = text[:-1] + "\n"
                self.buffer.cursor_position = len(self.buffer.text)
                return
            self.buffer.append_to_history()
            self.buffer.reset()
            if self.mode == "ask":
                self._settle(text)
            else:
                self.dispatch(text)

        @keys.add("c-j", filter=typing)
        @keys.add("escape", "enter", filter=typing)
        def newline(event: KeyPressEvent) -> None:
            self.buffer.insert_text("\n")

        @keys.add("tab", filter=typing)
        def complete(event: KeyPressEvent) -> None:
            if self.buffer.complete_state is not None:
                self.buffer.complete_next()
            else:
                self.buffer.start_completion(select_first=True)

        @keys.add("escape", filter=asking, eager=True)
        def escape(event: KeyPressEvent) -> None:
            self._cancel_prompt()

        @keys.add("c-c")
        def interrupt(event: KeyPressEvent) -> None:
            if self.mode == "confirm":
                self._settle(INTERRUPT)
            elif self.mode == "ask":
                self._cancel_prompt()
            elif self.task is not None and not self.task.done():
                self.task.cancel()
            elif self.buffer.text:
                self.buffer.reset()
                self.interrupts = 0
            else:
                self.interrupts += 1
                if self.interrupts >= 2:
                    event.app.exit()
                else:
                    self.renderer.status("Press Ctrl+C again or Ctrl+D to exit.")

        @keys.add("c-d", filter=empty)
        def eof(event: KeyPressEvent) -> None:
            if self.mode != "chat":
                self._cancel_prompt()
                return
            if self.task is not None and not self.task.done():
                self.task.cancel()
            event.app.exit()

        @keys.add("pageup")
        def page_up(event: KeyPressEvent) -> None:
            self._scroll(-max(self._height() - 2, 1))

        @keys.add("pagedown")
        def page_down(event: KeyPressEvent) -> None:
            self._scroll(max(self._height() - 2, 1))

        @keys.add("c-up")
        def line_up(event: KeyPressEvent) -> None:
            self._scroll(-SCROLL_STEP)

        @keys.add("c-down")
        def line_down(event: KeyPressEvent) -> None:
            self._scroll(SCROLL_STEP)

        return keys

    def _cancel_prompt(self) -> None:
        self.buffer.reset()
        self._settle(False if self.mode == "confirm" else None)

    def _settle(self, value: Any) -> None:
        if self.mode in OVERLAY_MODES and self.future is not None and not self.future.done():
            if value is None:
                if self.overlay is not None:
                    self.overlay.detach()
                self.mode = "chat"
                self.overlay = None
                self.overlay_allow_raw = False
                self.search_buffer.reset()
                if self.app.is_running:
                    self.app.layout.focus(self.field)
            self.future.set_result(value)
            return
        if self.future is not None and not self.future.done():
            self.future.set_result(value)

    def dispatch(self, text: str) -> None:
        if self.task is not None and not self.task.done():
            self.renderer.status("Busy. Press Ctrl+C to cancel the current task.")
            return
        self.interrupts = 0
        self.follow = True
        self.task = asyncio.get_running_loop().create_task(self._run(text))

    async def _run(self, text: str) -> None:
        try:
            await self.session.submit(text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.renderer.error(f"Unexpected error: {type(exc).__name__}: {exc}")
        finally:
            if not self.session.running:
                with contextlib.suppress(Exception):
                    self.app.exit()
            self.app.invalidate()

    async def _ask_user(self, mode: str, question: str, completer: WordCompleter | None) -> Any:
        self.future = asyncio.get_running_loop().create_future()
        self.mode = mode
        self.question = question
        if completer is not None:
            self.buffer.completer = completer
        self.buffer.reset()
        self.app.invalidate()
        try:
            return await self.future
        finally:
            self.mode = "chat"
            self.question = ""
            self.future = None
            self.buffer.completer = self.completer
            self.buffer.reset()
            self.app.invalidate()

    async def confirm(self, question: str, yes: str, no: str, yes_keys: Any = ("y",)) -> bool:
        self.items = ((str(yes_keys[0]), yes), ("Enter", no))
        result = await self._ask_user("confirm", question, None)
        if result is INTERRUPT:
            raise KeyboardInterrupt
        return bool(result)

    async def ask_options(self, label: str, options: list[str], active: str = "") -> str | None:
        items = [
            PaletteItem(option, "", "", "", "", "", "active" if option == active else "")
            for option in options
        ]
        index = next((i for i, item in enumerate(items) if item.value == "active"), 0)
        answer: str | None = await self._ask_overlay("options", items, label, OPTIONS_HINT, index)
        return answer

    async def _ask_overlay(
        self, mode: str, items: list[PaletteItem], title: str, raw_hint: str, index: int = 0
    ) -> Any:
        self.future = asyncio.get_running_loop().create_future()
        self._open(mode, items, title, raw_hint)
        if self.overlay is not None:
            self.overlay.index = index
        try:
            return await self.future
        finally:
            if self.overlay is not None:
                self.overlay.detach()
            self.mode = "chat"
            self.overlay = None
            self.overlay_allow_raw = False
            self.future = None
            self.search_buffer.reset()
            if self.app.is_running:
                self.app.layout.focus(self.field)
            self.app.invalidate()


async def run_fullscreen(session: InteractiveSession) -> None:
    renderer, sink = attach(session)
    ui = FullscreenUI(session, renderer, sink)
    session.picker = ui
    renderer.startup(session.config)
    try:
        await ui.app.run_async()
    finally:
        if ui.task is not None and not ui.task.done():
            ui.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ui.task
        await session.close()
        renderer.shutdown()
