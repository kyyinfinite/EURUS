from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
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
from prompt_toolkit.layout.containers import AnyContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import DynamicStyle

from eurus.tui.commands import COMMANDS
from eurus.tui.fullscreen_renderer import FullscreenRenderer
from eurus.tui.fullscreen_renderer import attach as attach_classic
from eurus.tui.layout import cell_clip
from eurus.tui.palette import PaletteItem
from eurus.tui.prompt import MENU_ROWS, fragments
from eurus.tui.session_ui import OVERLAY_MODES, SessionUI
from eurus.tui.theme import Theme
from eurus.tui.transcript import TranscriptSink
from eurus.tui.workspace.activity import ActivityFeed
from eurus.tui.workspace.clipboard import copy_to_terminal_clipboard
from eurus.tui.workspace.completer import WorkspaceCompleter
from eurus.tui.workspace.diffsession import DiffSession
from eurus.tui.workspace.diffview import render_diff
from eurus.tui.workspace.editor import EditorSession
from eurus.tui.workspace.explorer import FileExplorer
from eurus.tui.workspace.fileindex import FileIndex, build_index
from eurus.tui.workspace.geometry import PanelLayout, compute_layout
from eurus.tui.workspace.mouse import DoubleClickTracker, DragState, drag_move_handler
from eurus.tui.workspace.preview import FilePreview
from eurus.tui.workspace.store import WorkspaceStore
from eurus.tui.workspace.syntax import TOKEN_STYLE_KEYS, highlight_line, lexer_for
from eurus.tui.workspace.views import (
    TabRegistry,
    WorkspaceTab,
    panel_glyphs,
    render_footer_hints,
    render_header,
    render_panel_title,
    render_tab_bar,
    status_glyph,
)

if TYPE_CHECKING:
    from eurus.tui.session import InteractiveSession

__all__ = ["WorkspaceApp", "run_workspace"]


def attach_workspace(session: InteractiveSession) -> tuple[FullscreenRenderer, TranscriptSink]:
    renderer, sink = attach_classic(session)
    store = WorkspaceStore()
    size = shutil.get_terminal_size((120, 34))
    layout = compute_layout(max(size.columns, 100), size.lines, store)
    renderer.console.width = max(layout.main.width, 40)
    return renderer, sink


def _diff_row_count(session: DiffSession) -> int:
    model = session.model
    if model.identical:
        return 1
    count = len(model.hunks)
    for hunk in model.hunks:
        count += len(hunk.context_before)
        count += len(hunk.context_after)
        count += len(hunk.old_lines) + len(hunk.new_lines)
    return count


def _hunk_row_offset(session: DiffSession) -> int:
    model = session.model
    offset = 0
    for hunk in model.hunks:
        if hunk.index == session.current_hunk:
            return max(offset - 1, 0)
        offset += 1
        offset += len(hunk.context_before)
        offset += len(hunk.old_lines) + len(hunk.new_lines)
        offset += len(hunk.context_after)
    return offset


INDEX_TTL_SECONDS = 30.0
DEFAULT_WORKSPACE_SOURCE_THEME = "eurus"


def _apply_workspace_theme(session: InteractiveSession, renderer: FullscreenRenderer) -> None:
    from eurus.tui import themes

    if themes.active_name() != DEFAULT_WORKSPACE_SOURCE_THEME:
        return
    try:
        charcoal = themes.resolve(
            "charcoal",
            renderer.theme.unicode,
            None,
            themes.glyph_choice(),
        )
    except ValueError:
        return
    renderer.apply_theme(charcoal)


class WorkspaceApp(SessionUI):
    def __init__(
        self,
        session: InteractiveSession,
        renderer: FullscreenRenderer,
        sink: TranscriptSink,
        store: WorkspaceStore | None = None,
    ) -> None:
        self.store = store or WorkspaceStore()
        self.tabs = TabRegistry()
        root = Path(session.config.permissions.workspace).expanduser().resolve()
        self.explorer = FileExplorer(root, self.app_invalidate)
        self.previews: dict[str, FilePreview] = {}
        self.editors: dict[str, EditorSession] = {}
        self.diffs: dict[str, DiffSession] = {}
        self.activity = ActivityFeed(self.app_invalidate)
        self._file_index: FileIndex | None = None
        self._drag = DragState()
        self._explorer_clicks = DoubleClickTracker(lambda: self._click_clock())
        self.find_active = False
        self.replace_active = False
        self.find_buffer = Buffer(multiline=False)
        self.replace_buffer = Buffer(multiline=False)
        super().__init__(session, renderer, sink)
        self.buffer.completer = WorkspaceCompleter(COMMANDS, self._ensure_file_index)

    def _click_clock(self) -> float:
        return time.monotonic()

    def _ensure_file_index(self, force: bool = False) -> FileIndex:
        now = time.monotonic()
        if force or self._file_index is None or now - self._file_index.built_at > INDEX_TTL_SECONDS:
            self._file_index = build_index(self.explorer.root)
        return self._file_index

    def workspace_root_path(self) -> Path:
        return Path(self.session.config.permissions.workspace).expanduser().resolve()

    def on_created(self) -> None:
        self.window = Window(
            FormattedTextControl(self._render_main),
            wrap_lines=False,
            always_hide_cursor=True,
        )

    def workspace_root(self) -> Path:
        return Path(self.session.config.permissions.workspace).expanduser()

    def _panel_geometry(self) -> PanelLayout:
        columns, rows = self._size()
        return compute_layout(max(columns, 1), max(rows, 1), self.store)

    def _main_width(self, columns: int) -> int:
        layout = compute_layout(max(columns, 1), 24, self.store)
        return max(layout.main.width, 20)

    def focus_target(self) -> AnyContainer:
        target = self.store.focus
        if target == "explorer" and self.store.explorer_visible:
            return self.explorer_window
        if target == "activity" and self.store.activity_visible:
            return self.activity_window
        if target == "workspace":
            return self.workspace_window
        return self.field

    def _apply_focus(self) -> None:
        if not self.app.is_running:
            return
        if self.mode in OVERLAY_MODES:
            self.app.layout.focus(self.search_field)
        else:
            self.app.layout.focus(self.focus_target())
        self.app.invalidate()

    def _focus_panel(self, target: str) -> None:
        if target == "explorer" and not self.store.explorer_visible:
            return
        if target == "activity" and not self.store.activity_visible:
            return
        self.store.set_focus(target)
        self._apply_focus()

    def _cycle_focus(self, delta: int) -> None:
        self.store.cycle_focus(delta)
        self._apply_focus()

    def _set_focus_from_container(self, target: str) -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                self._focus_panel(target)

        return handler

    def apply_theme(self, theme: Theme) -> None:
        super().apply_theme(theme)
        workspace_rules = {
            "panel": f"fg:{theme.text}",
            "panel-title": f"bold fg:{theme.steel}",
            "panel-title-focused": f"bold fg:{theme.copper}",
            "splitter": f"fg:{theme.border}",
            "tab": f"fg:{theme.dim}",
            "tab-active": f"bold fg:{theme.copper}",
            "tab-bar": f"bg:{theme.header_bar}",
            "tab-bar-fill": f"fg:{theme.border} bg:{theme.header_bar}",
            "explorer-selected": f"bg:{theme.selected_row} fg:{theme.selected_text}",
            "explorer-dir": f"fg:{theme.steel}",
            "explorer-file": f"fg:{theme.text}",
            "explorer-recent": f"fg:{theme.copper}",
            "explorer-dirty": f"fg:{theme.yellow}",
            "explorer-error": f"fg:{theme.red}",
            "preview-line-number": f"fg:{theme.border}",
            "editor-cursor": f"fg:{theme.background} bg:{theme.copper}",
            "editor-cursor-line": f"bold fg:{theme.copper}",
            "editor-selection": f"bg:{theme.selected_row} fg:{theme.text}",
            "activity-running": f"fg:{theme.steel}",
            "activity-ok": f"fg:{theme.green}",
            "activity-error": f"fg:{theme.red}",
            "activity-pending": f"fg:{theme.amber}",
            "diff-header": f"bold fg:{theme.steel}",
            "diff-hunk": f"fg:{theme.teal}",
            "diff-hunk-current": f"bold fg:{theme.copper}",
            "diff-added": f"fg:{theme.green}",
            "diff-added-number": f"fg:{theme.green}",
            "diff-removed": f"fg:{theme.red}",
            "diff-removed-number": f"fg:{theme.red}",
            "diff-line-number": f"fg:{theme.border}",
        }
        for token_key, theme_key in TOKEN_STYLE_KEYS.items():
            workspace_rules[token_key] = f"fg:{getattr(theme, theme_key)}"
        from prompt_toolkit.styles import Style

        self.style = Style([*self.style.style_rules, *workspace_rules.items()])

    def _scroll_panel(self, target: str, delta: int) -> None:
        if target == "explorer":
            self.explorer.offset = max(self.explorer.offset + delta, 0)
            if self.explorer.selected < self.explorer.offset:
                self.explorer.selected = self.explorer.offset
            self.app_invalidate()
        elif target == "activity":
            self.activity.move(delta)
        elif target == "main":
            self._scroll_focused_main(delta)

    def _explorer_click(self, index: int) -> None:
        self.explorer.selected = index
        self.explorer.clear_filter() or None
        self._focus_panel("explorer")
        self.app_invalidate()

    def _explorer_open_click(self, index: int) -> None:
        self.explorer.selected = index
        if self.explorer.selected_is_dir():
            self.explorer.toggle_selected()
        else:
            self._open_explorer_selection()
        self.app_invalidate()

    def _apply_drag_width(self, side: str, event: MouseEvent) -> None:
        columns, _ = self._size()
        layout = compute_layout(max(columns, 1), 24, self.store)
        if side == "explorer" and layout.explorer is not None:
            absolute = layout.explorer.left + event.position.x
            self.store.explorer_width = max(min(absolute, 120), 8)
        elif side == "activity" and layout.activity is not None:
            absolute = layout.activity.left + event.position.x
            self.store.activity_width = max(min(columns - absolute, 120), 8)
        self.app_invalidate()

    def _drag_handlers(self, side: str) -> Callable[[MouseEvent], None]:
        return drag_move_handler(
            self._drag,
            lambda event: self._apply_drag_width(side, event),
        )

    def _copy_editor_selection_to_clipboard(self) -> None:
        editor = self._active_editor()
        if editor is None:
            return
        text = editor.buffer.selected_text() or editor.buffer.lines[editor.buffer.row]
        if not text:
            return
        self.app.clipboard.set_text(text)
        copied = copy_to_terminal_clipboard(text, self.app.output)
        editor.message = "copied" + ("" if copied else " (session clipboard only)")
        self.app_invalidate()

    def _panel_height(self, window: AnyContainer) -> int:
        info = getattr(window, "render_info", None)
        if info is not None:
            return max(int(info.window_height), 1)
        return max(self._size()[1] - 10, 3)

    def _with_handler(
        self, fragments: StyleAndTextTuples, handler: Callable[[MouseEvent], None]
    ) -> StyleAndTextTuples:
        if not fragments:
            return fragments
        first = fragments[0]
        return [(*first, handler), *fragments[1:]]

    def _explorer_row_handler(self, index: int) -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                if self._explorer_clicks.register(index):
                    self._explorer_open_click(index)
                else:
                    self._explorer_click(index)
            elif event.event_type == MouseEventType.SCROLL_UP:
                self._scroll_panel("explorer", -3)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll_panel("explorer", 3)

        return handler

    def _title_handler(self, target: str, drag_side: str = "") -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                self._focus_panel(target)
            elif event.event_type == MouseEventType.SCROLL_UP:
                self._scroll_panel("explorer" if target == "explorer" else "main", -3)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll_panel("explorer" if target == "explorer" else "main", 3)
            elif event.event_type == MouseEventType.MOUSE_MOVE and self._drag.active:
                self._apply_active_drag(event)

        return handler

    def _apply_active_drag(self, event: MouseEvent) -> None:
        if not self._drag.active:
            return
        self._apply_drag_width(self._drag.side, event)

    def _main_handler(self) -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.SCROLL_UP:
                self._scroll_panel("main", -3)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll_panel("main", 3)
            elif event.event_type == MouseEventType.MOUSE_MOVE and self._drag.active:
                self._apply_active_drag(event)
            elif event.event_type == MouseEventType.MOUSE_UP and self._drag.active:
                self._drag.active = False

        return handler

    def _splitter_window(self, side: str) -> Window:
        def render() -> StyleAndTextTuples:
            rows = max(self._size()[1], 3)
            handler = self._splitter_handler(side)
            splitter_char = panel_glyphs(self.theme).splitter
            out: StyleAndTextTuples = []
            for _ in range(rows):
                out.append(("class:splitter", splitter_char, handler))
                out.append(("", "\n"))
            if out:
                out.pop()
            return out

        return Window(
            FormattedTextControl(render),
            width=1,
            wrap_lines=False,
            style="class:splitter",
        )

    def _splitter_handler(self, side: str) -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self._drag.side = side
                self._drag.active = True
            elif event.event_type == MouseEventType.MOUSE_UP:
                self._drag.active = False
            elif event.event_type == MouseEventType.MOUSE_MOVE and self._drag.active:
                self._apply_drag_width(side, event)

        return handler

    def _render_explorer(self) -> StyleAndTextTuples:
        width = max(self._explorer_width(), 8)
        explorer = self.explorer
        out: StyleAndTextTuples = self._with_handler(
            list(
                render_panel_title(
                    self.theme,
                    "Explorer",
                    width,
                    right=self.workspace_root_path().name[: max(width // 3, 1)],
                    focused=self.store.focus == "explorer",
                )
            ),
            self._title_handler("explorer", "explorer"),
        )
        out.append(("", "\n"))
        out.append(("class:dim", panel_glyphs(self.theme).rule * width))
        body_height = self._panel_height(self.explorer_window) - 3
        explorer.scroll_to(body_height)
        if explorer.filter:
            out.append(("class:explorer-recent", f" search: {explorer.filter[: width - 10]}"))
            out.append(("", "\n"))
            body_height -= 1
        rows = explorer.visible_rows(body_height)
        for position, row in enumerate(rows):
            index = explorer.offset + position
            selected = index == explorer.selected
            rendered = self._explorer_row(row, index, selected, width)
            out.extend(self._with_handler(rendered, self._explorer_row_handler(index)))
            out.append(("", "\n"))
        if not rows:
            message = (
                f" no files match {explorer.filter}" if explorer.filter else " workspace is empty"
            )
            out.append(("class:dim", message[:width]))
            out.append(("", "\n"))
        out.append(("class:dim", "─" * width))
        out.append(("", "\n"))
        description = explorer.describe_selection()
        out.append(("class:dim", f" {cell_clip(description, width - 2)}"))
        return out

    def _explorer_row(self, row: Any, index: int, selected: bool, width: int) -> StyleAndTextTuples:
        from eurus.tui.workspace.explorer import SearchHit

        is_dir = row.is_dir
        name = row.name
        depth = getattr(row, "depth", 0)
        if isinstance(row, SearchHit):
            depth = row.relative.count("/")
            name = row.relative
        indent = " " * min(depth * 2, max(width - 8, 0))
        if getattr(row, "truncated", False) and not getattr(row, "recent", False):
            style = "class:dim"
            if selected:
                style = "class:explorer-selected"
            label = f" {indent}{name}"[:width]
            return [(style, label)]
        glyphs = panel_glyphs(self.theme)
        marker = (
            glyphs.expanded
            if getattr(row, "expanded", False)
            else (glyphs.collapsed if is_dir else " ")
        )
        dirty = self.explorer.is_dirty(getattr(row, "relative", ""))
        recent = getattr(row, "recent", False)
        if selected:
            base = "class:explorer-selected"
        elif getattr(row, "error", ""):
            base = "class:explorer-error"
        elif is_dir:
            base = "class:explorer-dir"
        elif recent:
            base = "class:explorer-recent"
        else:
            base = "class:explorer-file"
        label = f"{name}/" if is_dir else name
        prefix = f" {indent}{marker} "
        glyphs_ref = panel_glyphs(self.theme)
        suffix = "" if is_dir else (f" {glyphs_ref.dirty}" if dirty else "")
        room = width - len(prefix) - len(suffix)
        label = label[: max(room, 1)]
        out: StyleAndTextTuples = [(base, prefix + label)]
        if suffix:
            out.append(("class:explorer-dirty" if not selected else base, suffix))
        if not selected:
            pad = width - len(prefix) - len(label) - len(suffix)
            if pad > 0:
                out.append(("", " " * pad))
        return out

    def _explorer_width(self) -> int:
        layout = self._panel_geometry()
        if layout.explorer is None:
            return min(self.store.explorer_width, max(self._size()[0] - 2, 1))
        return layout.explorer.width

    def _activity_width(self) -> int:
        layout = self._panel_geometry()
        if layout.activity is None:
            return min(self.store.activity_width, max(self._size()[0] - 2, 1))
        return layout.activity.width

    def _render_activity(self) -> StyleAndTextTuples:
        width = max(self._activity_width(), 12)
        feed = self.activity
        title = self._with_handler(
            list(
                render_panel_title(
                    self.theme,
                    "Activity",
                    width,
                    right=f"{len(feed.items)} events",
                    focused=self.store.focus == "activity",
                )
            ),
            self._title_handler("activity", "activity"),
        )
        out: StyleAndTextTuples = list(title)
        out.append(("", "\n"))
        out.append(("class:dim", panel_glyphs(self.theme).rule * width))
        out.append(("", "\n"))
        body_height = self._panel_height(self.activity_window) - 4
        rows = feed.visible_rows(body_height)
        if not rows:
            out.append(("class:dim", " Agent activity will appear here"))
            out.append(("", "\n"))
        for item, index in rows:
            if index < 0:
                detail_line = item.detail.split("\n")[0]
                out.append(("class:dim", f"   {cell_clip(detail_line, width - 4)}"))
                out.append(("", "\n"))
                continue
            out.extend(
                self._with_handler(
                    self._activity_row(item, index == feed.selected, width),
                    self._activity_row_handler(index),
                )
            )
            out.append(("", "\n"))
        out.append(("class:dim", panel_glyphs(self.theme).rule * width))
        out.append(("", "\n"))
        running = feed.running_count()
        state = f"{running} running" if running else "idle"
        out.append(("class:dim", f" {state} · {feed.selected + 1}/{max(len(feed.items), 1)}"))
        return out

    def _activity_row(self, item: Any, selected: bool, width: int) -> StyleAndTextTuples:
        indent = "  " * item.depth
        glyph = status_glyph(panel_glyphs(self.theme), item.status)
        if item.status == "running":
            glyph_style = "class:activity-running"
        elif item.status in {"ok", "allowed", "done"}:
            glyph_style = "class:activity-ok"
        elif item.status in {"error", "denied"}:
            glyph_style = "class:activity-error"
        elif item.status == "pending":
            glyph_style = "class:activity-pending"
        else:
            glyph_style = "class:dim"
        base = "class:explorer-selected" if selected else "class:explorer-file"
        title_style = (
            base
            if selected
            else ("class:activity-running" if item.status == "running" else "class:explorer-file")
        )
        duration = ""
        if item.duration:
            duration = f" {item.duration:.1f}s"
        elif item.status == "running":
            duration = f" {time.monotonic() - item.started:.0f}s"
        if item.usage_text:
            duration += f" · {item.usage_text}"
        prefix = f" {indent}{glyph} "
        room = max(width - len(prefix) - len(duration) - 1, 4)
        title = item.title[:room]
        out: StyleAndTextTuples = [(glyph_style, prefix), (title_style, title)]
        if not selected:
            pad = width - len(prefix) - len(title) - len(duration) - 1
            if pad > 0:
                out.append(("", " " * pad))
        if duration:
            out.append(("class:dim", duration))
        return out

    def _activity_row_handler(self, index: int) -> Callable[[MouseEvent], None]:
        def handler(event: MouseEvent) -> None:
            if event.event_type == MouseEventType.MOUSE_UP:
                self.activity.selected = min(index, len(self.activity.items) - 1)
                self.activity.follow = False
                self._focus_panel("activity")
                self.app_invalidate()
            elif event.event_type == MouseEventType.SCROLL_UP:
                self.activity.move(-3)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self.activity.move(3)

        return handler

    def _render_tab_bar(self) -> StyleAndTextTuples:
        columns, _ = self._size()
        layout = compute_layout(max(columns, 1), 24, self.store)
        return render_tab_bar(
            self.theme,
            self.tabs.tabs,
            self.tabs.index,
            layout.main.width,
            on_select=self.tabs.select,
        )

    def _render_workspace_panel_with_mouse(self) -> StyleAndTextTuples:
        return self._with_handler(self._render_workspace_panel(), self._main_handler())

    def _render_workspace_panel(self) -> StyleAndTextTuples:
        tab = self.tabs.active
        if tab.kind == "chat":
            return self._render_main()
        if tab.kind == "editor":
            return self._render_editor(tab.path)
        if tab.kind == "diff":
            return self._render_diff(tab.key)
        if tab.kind == "preview":
            return self._render_preview(tab.path)
        return [("class:dim", " This view opens once a file or diff is selected.")]

    def _active_diff(self) -> DiffSession | None:
        tab = self.tabs.active
        if tab.kind != "diff":
            return None
        return self.diffs.get(tab.key)

    def _open_buffer_diff(self, relative: str) -> DiffSession | None:
        editor = self.editors.get(relative)
        if editor is None:
            return None
        key = f"buffer-diff:{relative}"
        session = DiffSession.for_buffer(
            relative,
            relative,
            editor.original_text,
            editor.buffer.to_text(),
        )
        self.diffs[key] = session
        self.tabs.open(WorkspaceTab("diff", f"diff {editor.title}", path=relative, key=key))
        self.store.set_focus("workspace")
        self._apply_focus()
        return session

    def _render_diff(self, key: str) -> StyleAndTextTuples:
        session = self.diffs.get(key)
        if session is None:
            return [("class:dim", " opening diff…")]
        width = self._main_width(self._size()[0])
        height = self._panel_height(self.workspace_window)
        session.scroll_offset = min(
            max(session.scroll_offset, 0), max(_diff_row_count(session) - 1, 0)
        )
        return render_diff(session, width, height)

    def _apply_diff_review(self) -> None:
        session = self._active_diff()
        if session is None:
            return
        if session.source_kind == "buffer":
            editor = self.editors.get(session.editor_key)
            if editor is None:
                return
            if session.apply_review_to_buffer(editor.buffer):
                self._sync_dirty(editor.relative)
                editor.message = "diff review applied"
        else:
            if session.apply_review_to_file():
                editor = self.editors.get(session.relative)
                if editor is not None:
                    editor.reload_from_disk()
                    self._sync_dirty(editor.relative)
        self.app_invalidate()

    def _open_editor(self, relative: str) -> EditorSession:
        editor = self.editors.get(relative)
        if editor is None:
            editor = EditorSession.open(self.explorer.root, relative)
            self.editors[relative] = editor
        self.tabs.open(WorkspaceTab("editor", editor.title, path=relative, key=relative))
        self.explorer.expand_to(relative)
        self._sync_dirty(relative)
        self.store.set_focus("workspace")
        self._apply_focus()
        return editor

    def _active_editor(self) -> EditorSession | None:
        tab = self.tabs.active
        if tab.kind != "editor":
            return None
        return self.editors.get(tab.path)

    def _sync_dirty(self, relative: str) -> None:
        editor = self.editors.get(relative)
        if editor is None:
            return
        dirty = editor.is_dirty()
        self.tabs.mark_dirty(relative, dirty)
        self.explorer.mark_dirty(relative, dirty)

    def _render_editor(self, relative: str) -> StyleAndTextTuples:
        editor = self.editors.get(relative)
        if editor is None:
            return [("class:dim", " opening…")]
        width = self._main_width(self._size()[0])
        height = self._panel_height(self.workspace_window)
        if self.find_active:
            height -= 1
        out: StyleAndTextTuples = []
        if editor.readonly:
            out.append(("class:explorer-error", f" READ-ONLY · {editor.readonly_reason}"))
            out.append(("", "\n"))
            height -= 1
        buffer = editor.buffer
        total = len(buffer.lines)
        number_width = max(len(str(total)), 3)
        gutter = number_width + 2
        body_width = max(width - gutter - 2, 1)
        row, col = buffer.cursor
        scroll = editor.scroll_offset
        if row < scroll:
            scroll = row
        if row >= scroll + height:
            scroll = row - height + 1
        scroll = min(max(scroll, 0), max(total - 1, 0))
        editor.scroll_offset = scroll
        col_offset = (
            max(col - body_width + 1, 0)
            if col >= editor.col_offset + body_width
            else editor.col_offset
        )
        col_offset = min(max(col_offset, 0), max(col - 4, 0))
        editor.col_offset = col_offset
        start, end = buffer.selection_range()
        sel_row, sel_col = start
        sel_erow, sel_ecol = end
        lexer = lexer_for(editor.title)
        for position in range(scroll, min(scroll + height, total)):
            line = buffer.lines[position]
            if position == row:
                out.append(("class:editor-cursor-line", f"{position + 1:>{number_width}} "))
            else:
                out.append(("class:preview-line-number", f"{position + 1:>{number_width}} "))
            visible_line = line[col_offset : col_offset + body_width]
            if position == row and sel_row == sel_erow and sel_col == sel_ecol:
                out.extend(self._render_cursor_line(visible_line, col - col_offset, lexer))
            else:
                out.extend(
                    self._render_selection_line(
                        visible_line,
                        position,
                        col - col_offset,
                        (sel_row, sel_col, sel_erow, sel_ecol),
                        lexer,
                    )
                )
            out.append(("", "\n"))
        out.extend(self._render_editor_status(editor, width, total))
        return out

    def _render_cursor_line(self, line: str, cursor_col: int, lexer: Any) -> StyleAndTextTuples:
        cut = min(max(cursor_col, 0), len(line))
        before = line[:cut]
        after = line[cut + 1 :]
        char = line[cut] if cut < len(line) else " "
        out: StyleAndTextTuples = []
        for style, text in highlight_line(before, lexer):
            out.append((style, text))
        out.append(("class:editor-cursor", char))
        for style, text in highlight_line(after, lexer):
            out.append((style, text))
        return out

    def _render_selection_line(
        self,
        line: str,
        row: int,
        cursor_col: int,
        bounds: tuple[int, int, int, int],
        lexer: Any,
    ) -> StyleAndTextTuples:
        sel_row, sel_col, sel_erow, sel_ecol = bounds
        has_selection = (sel_row, sel_col) != (sel_erow, sel_ecol)
        if not has_selection or row < sel_row or row > sel_erow:
            rendered = highlight_line(line, lexer)
            return list(rendered) if rendered else [("", "")]
        if row == sel_row and row == sel_erow:
            start_col = sel_col
            end_col = sel_ecol
        elif row == sel_row:
            start_col = sel_col
            end_col = len(line)
        elif row == sel_erow:
            start_col = 0
            end_col = sel_ecol
        else:
            start_col = 0
            end_col = len(line)
        start_col = min(start_col, len(line))
        end_col = min(end_col, len(line))
        out: StyleAndTextTuples = []
        for style, text in highlight_line(line[:start_col], lexer):
            out.append((style, text))
        if line[start_col:end_col]:
            out.append(("class:editor-selection", line[start_col:end_col]))
        for style, text in highlight_line(line[end_col:], lexer):
            out.append((style, text))
        return out

    def _render_editor_status(
        self, editor: EditorSession, width: int, total: int
    ) -> StyleAndTextTuples:
        buffer = editor.buffer
        row, col = buffer.cursor
        if editor.conflict:
            state = "CONFLICT"
            state_style = "class:explorer-error"
            actions = " · o overwrite · r reload"
        elif editor.readonly:
            state = "READ-ONLY"
            state_style = "class:dim"
            actions = ""
        elif editor.is_dirty():
            state = "MODIFIED"
            state_style = "class:explorer-dirty"
            actions = " · Ctrl+S save · Alt+D discard"
        else:
            state = editor.status.upper()
            state_style = "class:footer-mode"
            actions = ""
        out: StyleAndTextTuples = [
            ("class:dim", panel_glyphs(self.theme).rule * width),
            ("", "\n"),
            (state_style, f" {state}"),
            ("class:dim", f" · {editor.relative} · Ln {row + 1}, Col {col + 1} of {total}"),
            ("class:dim", actions),
        ]
        if editor.message:
            out.append(("class:dim", f" · {editor.message}"))
        return out

    def _render_preview(self, relative: str) -> StyleAndTextTuples:
        preview = self.previews.get(relative)
        if preview is None:
            preview = FilePreview(self.workspace_root_path(), relative)
            self.previews[relative] = preview
        width = self._main_width(self._size()[0])
        height = self._panel_height(self.workspace_window)
        out: StyleAndTextTuples = []
        loaded = preview.loaded
        if loaded.error:
            out.append(("class:explorer-error", f" {loaded.error}"))
            return out
        if loaded.binary:
            out.append(("class:dim", f" binary file · {preview.title} · {loaded.size} bytes"))
            return out
        if loaded.too_large:
            out.append(
                ("class:dim", f" file too large to preview · {loaded.size} bytes (cap 1 MB)")
            )
            return out
        lexer = lexer_for(preview.title)
        lines = loaded.lines
        gutter = preview.number_width + 2
        body_width = max(width - gutter - 2, 1)
        visible = lines[preview.offset : preview.offset + height]
        for position, line in enumerate(visible):
            number = preview.offset + position + 1
            out.append(("class:preview-line-number", f"{number:>{preview.number_width}} "))
            if line.strip():
                for style, text in highlight_line(line[:body_width], lexer):
                    out.append((style, text))
            out.append(("", "\n"))
        if not lines:
            out.append(("class:dim", " empty file"))
        return out

    def _render_footer(self) -> StyleAndTextTuples:
        width = self._size()[0]
        return render_footer_hints(self._footer_items(), width)

    def _footer_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        if self.store.focus == "composer":
            items.append(("Ctrl+K", "palette"))
        else:
            items.append(("Esc", "composer"))
        items.append(("Alt+1-4", "focus"))
        items.append(("F2", "files"))
        items.append(("F3", "activity"))
        items.append(("F8", f"layout:{self.store.preset}"))
        items.append(("Alt+←→", "tabs"))
        items.append(("Ctrl+C", "cancel"))
        return items

    def _render_status_line(self) -> StyleAndTextTuples:
        return fragments(self._status(self._size()[0]))

    def _build(self) -> Application[None]:
        confirming = Condition(lambda: self.mode == "confirm")
        asking = Condition(lambda: self.mode in {"ask", "confirm"})
        overlay = Condition(lambda: self.mode in OVERLAY_MODES)
        streaming = Condition(lambda: self.renderer.stream_view is not None)
        explorer_visible = Condition(lambda: self.store.explorer_visible)
        activity_visible = Condition(lambda: self.store.activity_visible)

        self.explorer_window = Window(
            FormattedTextControl(
                self._render_explorer,
                focusable=True,
                show_cursor=False,
                key_bindings=self._panel_keys("explorer"),
            ),
            wrap_lines=False,
            style="class:panel",
            width=lambda: Dimension.exact(self._explorer_width() or 1),
        )
        self.activity_window = Window(
            FormattedTextControl(
                self._render_activity,
                focusable=True,
                show_cursor=False,
                key_bindings=self._panel_keys("activity"),
            ),
            wrap_lines=False,
            style="class:panel",
            width=lambda: Dimension.exact(self._activity_width() or 1),
        )
        self.tab_bar_window = Window(
            FormattedTextControl(self._render_tab_bar),
            height=1,
            wrap_lines=False,
            style="class:tab-bar",
        )
        self.find_field_window = Window(
            BufferControl(buffer=self.find_buffer, key_bindings=self._find_keys()),
            height=1,
            dont_extend_height=True,
            style="class:input",
        )
        self.replace_field_window = Window(
            BufferControl(buffer=self.replace_buffer, key_bindings=self._replace_keys()),
            height=1,
            dont_extend_height=True,
            style="class:input",
        )
        find_bar = ConditionalContainer(
            HSplit(
                [
                    VSplit(
                        [
                            Window(
                                FormattedTextControl([("class:key", " find:")]),
                                width=7,
                                height=1,
                                dont_extend_width=True,
                            ),
                            self.find_field_window,
                            Window(
                                FormattedTextControl(
                                    lambda: [
                                        ("class:dim", "  enter next · tab replace · esc close")
                                    ]
                                ),
                                height=1,
                                wrap_lines=False,
                            ),
                        ]
                    ),
                    ConditionalContainer(
                        VSplit(
                            [
                                Window(
                                    FormattedTextControl([("class:key", " replace:")]),
                                    width=10,
                                    height=1,
                                    dont_extend_width=True,
                                ),
                                self.replace_field_window,
                                Window(
                                    FormattedTextControl(
                                        lambda: [
                                            ("class:dim", "  enter next · ctrl+a all · esc close")
                                        ]
                                    ),
                                    height=1,
                                    wrap_lines=False,
                                ),
                            ]
                        ),
                        filter=Condition(lambda: self.replace_active),
                    ),
                ]
            ),
            filter=Condition(lambda: self.find_active and self.tabs.active.kind == "editor"),
        )
        self.workspace_window = Window(
            FormattedTextControl(
                self._render_workspace_panel_with_mouse,
                focusable=True,
                show_cursor=False,
                key_bindings=self._panel_keys("workspace"),
            ),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.stream_window = Window(
            FormattedTextControl(self._render_stream),
            height=lambda: Dimension.exact(self._stream_height()),
            wrap_lines=False,
        )
        self.confirm_window = Window(FormattedTextControl(self._render_confirm), height=1)
        self.hint_window = Window(
            FormattedTextControl(lambda: fragments(self._hint(self._size()[0]))),
            height=1,
        )
        self.header_window = Window(
            FormattedTextControl(self._render_header_line),
            height=1,
            wrap_lines=False,
            style="class:header-bar",
        )
        self.footer_window = Window(
            FormattedTextControl(self._render_footer),
            height=1,
            style="class:footer",
        )
        self.status_window = Window(
            FormattedTextControl(self._render_status_line),
            height=1,
            style="class:footer",
        )

        explorer_side = ConditionalContainer(self.explorer_window, filter=explorer_visible)
        activity_side = ConditionalContainer(self.activity_window, filter=activity_visible)
        left_splitter = ConditionalContainer(
            self._splitter_window("explorer"), filter=explorer_visible
        )
        right_splitter = ConditionalContainer(
            self._splitter_window("activity"), filter=activity_visible
        )
        main_area = HSplit(
            [
                self.tab_bar_window,
                find_bar,
                self.workspace_window,
            ],
            style="class:panel",
        )
        main_row = VSplit([explorer_side, left_splitter, main_area, right_splitter, activity_side])
        center = HSplit(
            [
                self.header_window,
                main_row,
                ConditionalContainer(self.stream_window, filter=streaming),
                ConditionalContainer(self.confirm_window, filter=confirming),
                ConditionalContainer(self.hint_window, filter=asking),
                self._composer(),
                self.footer_window,
                self.status_window,
            ]
        )
        overlay_box = ConditionalContainer(self._overlay_container(), filter=overlay)
        root = FloatContainer(
            content=center,
            floats=[
                Float(
                    content=overlay_box,
                    top=8,
                    width=lambda: self._palette_width(),
                ),
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=MENU_ROWS, scroll_offset=1),
                ),
            ],
        )
        self.store.subscribe(self._apply_focus)
        self.tabs.on_change = self.app_invalidate
        focused = self.search_field if self.mode in OVERLAY_MODES else self.field
        app: Application[None] = Application(
            layout=Layout(root, focused_element=focused),
            key_bindings=self._keys(),
            style=DynamicStyle(lambda: self.style),
            full_screen=True,
            refresh_interval=0.25,
            mouse_support=True,
        )
        app.timeoutlen = 0.3
        return app

    def app_invalidate(self) -> None:
        with contextlib.suppress(Exception):
            self.app.invalidate()

    def _render_header_line(self) -> StyleAndTextTuples:
        columns, _ = self._size()
        session = self.session
        state = session.state.value.upper()
        model = session.config.agent.model
        try:
            name = self.workspace_root().name or "workspace"
        except OSError:
            name = "workspace"
        return render_header(self.theme, name, self.store.preset, state, model, columns)

    def _panel_keys(self, target: str) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("escape")
        def back(event: KeyPressEvent) -> None:
            if target == "explorer" and self.explorer.clear_filter():
                return
            self.store.set_focus("composer")
            self._apply_focus()

        if target == "explorer":

            @keys.add(Keys.Any, eager=True)
            def explorer_type(event: KeyPressEvent) -> None:
                char = event.data
                if char.isprintable() and char != " ":
                    self.explorer.type_filter(self.explorer.filter + char)
                    self.app_invalidate()

            @keys.add("up", eager=True)
            def explorer_up(event: KeyPressEvent) -> None:
                self.explorer.move(-1)
                self.app_invalidate()

            @keys.add("down", eager=True)
            def explorer_down(event: KeyPressEvent) -> None:
                self.explorer.move(1)
                self.app_invalidate()

            @keys.add("right", eager=True)
            @keys.add("enter")
            def explorer_open(event: KeyPressEvent) -> None:
                if self.explorer.toggle_selected():
                    self.app_invalidate()
                    return
                self._open_explorer_selection()

            @keys.add("left", eager=True)
            def explorer_collapse(event: KeyPressEvent) -> None:
                self.explorer.collapse_or_parent()
                self.app_invalidate()

            @keys.add("home", eager=True)
            def explorer_home(event: KeyPressEvent) -> None:
                self.explorer.selected = 0
                self.explorer.offset = 0
                self.app_invalidate()

            @keys.add("end", eager=True)
            def explorer_end(event: KeyPressEvent) -> None:
                self.explorer.selected = max(self.explorer.row_count() - 1, 0)
                self.app_invalidate()

            @keys.add("backspace", eager=True)
            def explorer_backspace(event: KeyPressEvent) -> None:
                self.explorer.backspace_filter()
                self.app_invalidate()

            @keys.add("f5", eager=True)
            def explorer_refresh(event: KeyPressEvent) -> None:
                self.explorer.rescan()
                self.app_invalidate()

        elif target == "workspace":
            editor_mode = Condition(lambda: self.tabs.active.kind == "editor")

            @keys.add(Keys.Any, filter=editor_mode, eager=True)
            def editor_type(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                if event.data and event.data.isprintable():
                    if editor.buffer.selection_range()[0] != editor.buffer.selection_range()[1]:
                        editor.buffer.delete_selection()
                    self._editor_insert(editor, event.data)

            @keys.add("up", filter=editor_mode, eager=True)
            def editor_up(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move(-1, 0)
                self.app_invalidate()

            @keys.add("s-up", filter=editor_mode, eager=True)
            def editor_up_extend(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.move(-1, 0, extend=True)
                self.app_invalidate()

            @keys.add("down", filter=editor_mode, eager=True)
            def editor_down(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move(1, 0)
                self.app_invalidate()

            @keys.add("s-down", filter=editor_mode, eager=True)
            def editor_down_extend(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.move(1, 0, extend=True)
                self.app_invalidate()

            @keys.add("left", filter=editor_mode, eager=True)
            def editor_left(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move(0, -1)
                self.app_invalidate()

            @keys.add("s-left", filter=editor_mode, eager=True)
            def editor_left_extend(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.move(0, -1, extend=True)
                self.app_invalidate()

            @keys.add("right", filter=editor_mode, eager=True)
            def editor_right(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move(0, 1)
                self.app_invalidate()

            @keys.add("s-right", filter=editor_mode, eager=True)
            def editor_right_extend(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.move(0, 1, extend=True)
                self.app_invalidate()

            @keys.add("home", filter=editor_mode, eager=True)
            def editor_home(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move_home()
                self.app_invalidate()

            @keys.add("end", filter=editor_mode, eager=True)
            def editor_end(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                editor.buffer.move_end()
                self.app_invalidate()

            @keys.add("pageup", filter=editor_mode, eager=True)
            def editor_page_up(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                height = self._panel_height(self.workspace_window)
                editor.buffer.move_page(-1, height)
                self.app_invalidate()

            @keys.add("pagedown", filter=editor_mode, eager=True)
            def editor_page_down(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                height = self._panel_height(self.workspace_window)
                editor.buffer.move_page(1, height)
                self.app_invalidate()

            @keys.add("enter", filter=editor_mode, eager=True)
            def editor_enter(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                self._editor_insert(editor, "\n")

            @keys.add("backspace", filter=editor_mode, eager=True)
            def editor_backspace(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                if editor.buffer.selection_range()[0] != editor.buffer.selection_range()[1]:
                    editor.buffer.delete_selection()
                else:
                    editor.buffer.backspace()
                self._after_edit(editor)

            @keys.add("delete", filter=editor_mode, eager=True)
            def editor_delete(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                if editor.buffer.selection_range()[0] != editor.buffer.selection_range()[1]:
                    editor.buffer.delete_selection()
                else:
                    editor.buffer.delete_forward()
                self._after_edit(editor)

            @keys.add("tab", filter=editor_mode, eager=True)
            def editor_tab(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                self._editor_insert(editor, " " * 4)

            @keys.add("c-s", filter=editor_mode, eager=True)
            def editor_save(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None:
                    return
                editor.check_external_change()
                if editor.save():
                    self._sync_dirty(editor.relative)
                self.app_invalidate()

            @keys.add("c-f", filter=editor_mode, eager=True)
            def editor_find(event: KeyPressEvent) -> None:
                self._enter_find_mode()

            @keys.add("c-h", filter=editor_mode, eager=True)
            def editor_replace(event: KeyPressEvent) -> None:
                self._enter_find_mode(replace=True)

            @keys.add("c-z", filter=editor_mode, eager=True)
            def editor_undo(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.undo()
                self._after_edit(editor)

            @keys.add("c-y", filter=editor_mode, eager=True)
            def editor_redo(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.buffer.redo()
                self._after_edit(editor)

            @keys.add("escape", "c", filter=editor_mode, eager=True)
            def editor_copy(event: KeyPressEvent) -> None:
                self._copy_editor_selection_to_clipboard()

            @keys.add("escape", "v", filter=editor_mode, eager=True)
            def editor_paste(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or self._editor_guard(editor):
                    return
                clip = self.app.clipboard.get_data()
                text = clip.text if clip is not None else ""
                if text:
                    self._editor_insert(editor, text.replace("\r\n", "\n").replace("\r", "\n"))

            @keys.add("escape", "d", filter=editor_mode, eager=True)
            def editor_discard(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or editor.readonly:
                    return
                editor.pending_discard = not editor.pending_discard
                editor.message = "discard all changes? y/n" if editor.pending_discard else ""
                self.app_invalidate()

            @keys.add("y", filter=Condition(lambda: self._discard_pending()), eager=True)
            def editor_discard_yes(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None:
                    return
                editor.pending_discard = False
                editor.discard()
                self._sync_dirty(editor.relative)
                self.app_invalidate()

            @keys.add("n", filter=Condition(lambda: self._discard_pending()), eager=True)
            def editor_discard_no(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None:
                    return
                editor.pending_discard = False
                editor.message = "discard cancelled"
                self.app_invalidate()

            @keys.add("o", filter=Condition(lambda: self._conflict_pending()), eager=True)
            def editor_overwrite(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None:
                    return
                if editor.overwrite():
                    self._sync_dirty(editor.relative)
                self.app_invalidate()

            @keys.add("r", filter=Condition(lambda: self._conflict_pending()), eager=True)
            def editor_reload(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None:
                    return
                if editor.reload_from_disk():
                    self._sync_dirty(editor.relative)
                self.app_invalidate()

            scroll_mode = Condition(lambda: self.tabs.active.kind != "editor")
            diff_mode = Condition(lambda: self.tabs.active.kind == "diff")

            @keys.add("n", filter=diff_mode, eager=True)
            def diff_next(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.next_hunk()
                    session.scroll_offset = _hunk_row_offset(session)
                    self.app_invalidate()

            @keys.add("p", filter=diff_mode, eager=True)
            def diff_previous(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.previous_hunk()
                    session.scroll_offset = _hunk_row_offset(session)
                    self.app_invalidate()

            @keys.add("a", filter=diff_mode, eager=True)
            def diff_accept(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.accept_current()
                    self.app_invalidate()

            @keys.add("x", filter=diff_mode, eager=True)
            def diff_reject(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.reject_current()
                    self.app_invalidate()

            @keys.add("A", filter=diff_mode, eager=True)
            def diff_accept_all(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.accept_every()
                    self.app_invalidate()

            @keys.add("X", filter=diff_mode, eager=True)
            def diff_reject_all(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.reject_every()
                    self.app_invalidate()

            @keys.add("u", filter=diff_mode, eager=True)
            def diff_toggle_view(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.toggle_view()
                    self.app_invalidate()

            @keys.add("c", filter=diff_mode, eager=True)
            def diff_toggle_context(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.toggle_collapse()
                    self.app_invalidate()

            @keys.add("y", filter=diff_mode, eager=True)
            def diff_apply(event: KeyPressEvent) -> None:
                self._apply_diff_review()

            @keys.add("<", filter=diff_mode, eager=True)
            def diff_scroll_left(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.column_offset = max(session.column_offset - 8, 0)
                    self.app_invalidate()

            @keys.add(">", filter=diff_mode, eager=True)
            def diff_scroll_right(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.column_offset += 8
                    self.app_invalidate()

            @keys.add("g", "c", filter=editor_mode, eager=True)
            def editor_changes(event: KeyPressEvent) -> None:
                editor = self._active_editor()
                if editor is None or not editor.is_dirty():
                    return
                self._open_buffer_diff(editor.relative)

            @keys.add("up", filter=diff_mode, eager=True)
            def diff_up(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.scroll_offset = max(session.scroll_offset - 1, 0)
                    self.app_invalidate()

            @keys.add("down", filter=diff_mode, eager=True)
            def diff_down(event: KeyPressEvent) -> None:
                session = self._active_diff()
                if session is not None:
                    session.scroll_offset = min(
                        session.scroll_offset + 1, max(_diff_row_count(session) - 1, 0)
                    )
                    self.app_invalidate()

            @keys.add("up", filter=scroll_mode, eager=True)
            def workspace_up(event: KeyPressEvent) -> None:
                self._scroll_focused_main(-3)

            @keys.add("down", filter=scroll_mode, eager=True)
            def workspace_down(event: KeyPressEvent) -> None:
                self._scroll_focused_main(3)

            @keys.add("pageup", filter=scroll_mode, eager=True)
            def workspace_page_up(event: KeyPressEvent) -> None:
                self._scroll_focused_main(-max(self._height() - 2, 1))

            @keys.add("pagedown", filter=scroll_mode, eager=True)
            def workspace_page_down(event: KeyPressEvent) -> None:
                self._scroll_focused_main(max(self._height() - 2, 1))

        else:

            @keys.add("up", eager=True)
            def activity_up(event: KeyPressEvent) -> None:
                self.activity.move(-1)
                self.app_invalidate()

            @keys.add("down", eager=True)
            def activity_down(event: KeyPressEvent) -> None:
                self.activity.move(1)
                self.app_invalidate()

            @keys.add("enter", eager=True)
            def activity_expand(event: KeyPressEvent) -> None:
                self.activity.toggle_expand()
                self.app_invalidate()

            @keys.add("home", eager=True)
            def activity_home(event: KeyPressEvent) -> None:
                self.activity.selected = 0
                self.activity.offset = 0
                self.activity.follow = False
                self.app_invalidate()

            @keys.add("end", eager=True)
            def activity_end(event: KeyPressEvent) -> None:
                self.activity.selected = max(len(self.activity.items) - 1, 0)
                self.activity.follow = True
                self.app_invalidate()

        return keys

    def _discard_pending(self) -> bool:
        editor = self._active_editor()
        return editor is not None and editor.pending_discard

    def _conflict_pending(self) -> bool:
        editor = self._active_editor()
        return editor is not None and editor.conflict != "" and not editor.pending_discard

    def _editor_guard(self, editor: EditorSession) -> bool:
        if editor.pending_discard:
            editor.pending_discard = False
            editor.message = "discard cancelled"
            self.app_invalidate()
            return True
        return editor.readonly

    def _editor_insert(self, editor: EditorSession, text: str) -> None:
        editor.buffer.insert_text(text)
        self._after_edit(editor)

    def _after_edit(self, editor: EditorSession) -> None:
        self._sync_dirty(editor.relative)
        self.app_invalidate()

    def _enter_find_mode(self, replace: bool = False) -> None:
        editor = self._active_editor()
        if editor is None:
            return
        self.find_active = True
        if replace:
            self.replace_active = True
        if self.app.is_running:
            self.app.layout.focus(self.find_field_window)
        self.app_invalidate()

    def _exit_find_mode(self) -> None:
        self.find_active = False
        self.replace_active = False
        self.store.set_focus("workspace")
        self._apply_focus()

    def _find_next(self) -> None:
        editor = self._active_editor()
        if editor is None:
            return
        query = self.find_buffer.text
        if not query:
            return
        match = editor.buffer.find_next(query)
        editor.message = (
            f"{len(editor.buffer.find_matches(query))} matches" if match else "no matches"
        )
        self.app_invalidate()

    def _replace_current(self) -> None:
        editor = self._active_editor()
        if editor is None or editor.readonly:
            return
        query = self.find_buffer.text
        replacement = self.replace_buffer.text
        if not query:
            return
        if editor.buffer.replace_next(query, replacement):
            self._after_edit(editor)

    def _replace_every(self) -> None:
        editor = self._active_editor()
        if editor is None or editor.readonly:
            return
        query = self.find_buffer.text
        replacement = self.replace_buffer.text
        if not query:
            return
        count = editor.buffer.replace_all(query, replacement)
        editor.message = f"replaced {count} occurrences"
        self._after_edit(editor)

    def _find_keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("enter")
        def find_next(event: KeyPressEvent) -> None:
            self._find_next()

        @keys.add("tab")
        def find_replace(event: KeyPressEvent) -> None:
            if self.replace_active and self.app.is_running:
                self.app.layout.focus(self.replace_field_window)
            elif self.app.is_running:
                self.replace_active = True
                self.app.layout.focus(self.replace_field_window)
            self.app_invalidate()

        @keys.add("escape", eager=True)
        def find_escape(event: KeyPressEvent) -> None:
            self._exit_find_mode()

        @keys.add("c-a")
        def find_replace_all(event: KeyPressEvent) -> None:
            self._replace_every()

        return keys

    def _replace_keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("enter")
        def replace_next(event: KeyPressEvent) -> None:
            self._replace_current()

        @keys.add("tab")
        def replace_back(event: KeyPressEvent) -> None:
            if self.app.is_running:
                self.app.layout.focus(self.find_field_window)

        @keys.add("escape", eager=True)
        def replace_escape(event: KeyPressEvent) -> None:
            self._exit_find_mode()

        return keys

    def _scroll_focused_main(self, delta: int) -> None:
        tab = self.tabs.active
        if tab.kind == "chat":
            self._scroll(delta)
            return
        preview = self.previews.get(tab.path)
        if preview is not None:
            preview.scroll(delta, self._panel_height(self.workspace_window))
            self.app_invalidate()

    def _open_explorer_selection(self) -> None:
        relative = self.explorer.open_selected()
        if relative is None:
            return
        session = EditorSession.open(self.explorer.root, relative)
        if session.loaded_ok:
            self._open_editor(relative)
            return
        self.tabs.open(
            WorkspaceTab("preview", relative.rsplit("/", 1)[-1], path=relative, key=relative)
        )
        self.explorer.expand_to(relative)
        self.store.set_focus("workspace")
        self._apply_focus()

    def _keys(self) -> KeyBindings:
        keys = super()._keys()
        free = Condition(
            lambda: (
                self.mode not in {"confirm", *OVERLAY_MODES}
                and not (self.task is not None and not self.task.done())
            )
        )
        typing = Condition(lambda: self.mode not in {"confirm", *OVERLAY_MODES})

        @keys.add("escape", "1", filter=typing, eager=True)
        def focus_composer(event: KeyPressEvent) -> None:
            self._focus_panel("composer")

        @keys.add("escape", "2", filter=typing, eager=True)
        def focus_explorer(event: KeyPressEvent) -> None:
            self._focus_panel("explorer")

        @keys.add("escape", "3", filter=typing, eager=True)
        def focus_workspace(event: KeyPressEvent) -> None:
            self._focus_panel("workspace")

        @keys.add("escape", "4", filter=typing, eager=True)
        def focus_activity(event: KeyPressEvent) -> None:
            self._focus_panel("activity")

        @keys.add("f2", filter=free, eager=True)
        def toggle_explorer(event: KeyPressEvent) -> None:
            self.store.toggle_explorer()
            self._apply_focus()

        @keys.add("f3", filter=free, eager=True)
        def toggle_activity(event: KeyPressEvent) -> None:
            self.store.toggle_activity()
            self._apply_focus()

        @keys.add("f8", filter=free, eager=True)
        def cycle_preset(event: KeyPressEvent) -> None:
            self.store.cycle_preset()
            self.app_invalidate()

        @keys.add("escape", "left", filter=typing, eager=True)
        def previous_tab(event: KeyPressEvent) -> None:
            self.tabs.cycle(-1)
            self.app_invalidate()

        @keys.add("escape", "right", filter=typing, eager=True)
        def next_tab(event: KeyPressEvent) -> None:
            self.tabs.cycle(1)
            self.app_invalidate()

        @keys.add("escape", "-", filter=typing, eager=True)
        @keys.add("escape", "_", filter=typing, eager=True)
        def shrink_panel(event: KeyPressEvent) -> None:
            self._resize_focused_side(-2)

        @keys.add("escape", "=", filter=typing, eager=True)
        @keys.add("escape", "+", filter=typing, eager=True)
        def grow_panel(event: KeyPressEvent) -> None:
            self._resize_focused_side(2)

        return keys

    def _resize_focused_side(self, delta: int) -> None:
        if self.store.focus == "explorer":
            self.store.resize_explorer(delta)
        elif self.store.focus == "activity":
            self.store.resize_activity(delta)
        else:
            return
        self.app_invalidate()

    def _palette_items(self) -> list[PaletteItem]:
        items = super()._palette_items()
        glyph = self.theme.glyphs
        workspace_items = [
            PaletteItem("", category="Workspace", header=True),
            PaletteItem(
                "Open File",
                "Quick-open any workspace file",
                "",
                "workspace-action",
                "quick-open",
                "",
                "",
                glyph.search,
            ),
            PaletteItem(
                "Toggle Explorer",
                "Show or hide the file tree",
                "F2",
                "workspace-action",
                "toggle-explorer",
                "",
                "",
                glyph.folder,
            ),
            PaletteItem(
                "Toggle Activity",
                "Show or hide the agent activity feed",
                "F3",
                "workspace-action",
                "toggle-activity",
                "",
                "",
                glyph.gear,
            ),
            PaletteItem(
                "Focus Mode",
                "Chat and composer only",
                "",
                "workspace-action",
                "preset:focus",
                "",
                "active" if self.store.preset == "focus" else "",
                glyph.chat,
            ),
            PaletteItem(
                "Workspace Mode",
                "Explorer, chat and activity",
                "",
                "workspace-action",
                "preset:workspace",
                "",
                "active" if self.store.preset == "workspace" else "",
                glyph.chat,
            ),
            PaletteItem(
                "Coding Mode",
                "Explorer, editor and activity",
                "",
                "workspace-action",
                "preset:coding",
                "",
                "active" if self.store.preset == "coding" else "",
                glyph.chat,
            ),
            PaletteItem(
                "Save Editor",
                "Persist the active editor buffer",
                "Ctrl+S",
                "workspace-action",
                "editor-save",
                "",
                "",
                glyph.ok,
            ),
            PaletteItem(
                "Review Editor Changes",
                "Open a diff of unsaved edits",
                "",
                "workspace-action",
                "open-diff",
                "",
                "",
                glyph.chevron,
            ),
            PaletteItem(
                "Apply Diff Review",
                "Apply accepted hunks to the buffer",
                "",
                "workspace-action",
                "diff-apply",
                "",
                "",
                glyph.ok,
            ),
            PaletteItem(
                "Close Tab",
                "Close the active workspace tab",
                "",
                "workspace-action",
                "close-tab",
                "",
                "",
                glyph.fail,
            ),
        ]
        return items + workspace_items

    async def _quick_open(self) -> None:
        index = self._ensure_file_index()
        files = index.files[:400]
        if not files:
            self.renderer.status("The workspace has no files to open.")
            return
        choice = await self.ask_options("open file>", files)
        if not choice or choice not in index.files:
            return
        self._open_editor(choice)

    def run_workspace_action(self, payload: str) -> None:
        kind, _, argument = payload.partition(":")
        if kind == "preset":
            self.store.set_preset(argument)
        elif kind == "toggle-explorer":
            self.store.toggle_explorer()
        elif kind == "toggle-activity":
            self.store.toggle_activity()
        elif kind == "focus":
            self._focus_panel(argument)
        elif kind == "quick-open":
            try:
                asyncio.get_running_loop().create_task(self._quick_open())
            except RuntimeError:
                return
            return
        elif kind == "editor-save":
            editor = self._active_editor()
            if editor is not None:
                editor.check_external_change()
                if editor.save():
                    self._sync_dirty(editor.relative)
        elif kind == "editor-discard":
            editor = self._active_editor()
            if editor is not None and not editor.readonly:
                editor.pending_discard = True
                editor.message = "discard all changes? y/n"
                self.store.set_focus("workspace")
                if self.tabs.active.kind != "editor":
                    self.tabs.open(
                        WorkspaceTab(
                            "editor", editor.title, path=editor.relative, key=editor.relative
                        )
                    )
        elif kind == "open-diff":
            editor = self._active_editor()
            if editor is not None and editor.is_dirty():
                self._open_buffer_diff(editor.relative)
        elif kind == "diff-apply":
            self._apply_diff_review()
        elif kind == "close-tab":
            self.tabs.close()
        self._apply_focus()
        self.app_invalidate()


async def run_workspace(session: InteractiveSession) -> None:
    renderer, sink = attach_workspace(session)
    _apply_workspace_theme(session, renderer)
    ui = WorkspaceApp(session, renderer, sink)
    session.picker = ui
    session.activity_sink = ui.activity
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
