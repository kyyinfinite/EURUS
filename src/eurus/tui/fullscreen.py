from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import (
    CompletionsMenu,
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import DynamicStyle

from eurus.tui.fullscreen_renderer import FullscreenRenderer, attach, pane_width
from eurus.tui.prompt import MENU_ROWS, fragments
from eurus.tui.session_ui import HEADER_COLUMNS, INTERRUPT, OVERLAY_MODES, SessionUI

if TYPE_CHECKING:
    from eurus.tui.session import InteractiveSession

__all__ = [
    "FullscreenRenderer",
    "FullscreenUI",
    "INTERRUPT",
    "attach",
    "pane_width",
    "run_fullscreen",
]


class FullscreenUI(SessionUI):
    def on_created(self) -> None:
        self.window = Window(
            FormattedTextControl(self._render_main),
            wrap_lines=False,
            always_hide_cursor=True,
        )

    def _main_width(self, columns: int) -> int:
        return pane_width(columns)

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

    def _header(self) -> HSplit:
        chars = self.chars
        header_right = Condition(lambda: self._size()[0] >= HEADER_COLUMNS)
        return HSplit(
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
                Window(height=1, char=chars[4], style="class:rule"),
            ]
        )

    def _footer(self) -> HSplit:
        chars = self.chars
        return HSplit(
            [
                Window(height=1, char=chars[4], style="class:rule"),
                Window(
                    FormattedTextControl(lambda: fragments(self._status(self._size()[0]))),
                    height=1,
                    style="class:footer",
                ),
            ]
        )

    def _build(self) -> Application[None]:
        confirming = Condition(lambda: self.mode == "confirm")
        asking = Condition(lambda: self.mode in {"ask", "confirm"})
        overlay = Condition(lambda: self.mode in OVERLAY_MODES)
        streaming = Condition(lambda: self.renderer.stream_view is not None)
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
                FormattedTextControl(lambda: fragments(self._hint(self._size()[0]))),
                height=1,
            ),
            filter=asking,
        )
        roomy = Condition(lambda: self._size()[1] >= 26)
        gap = ConditionalContainer(HSplit([Window(height=1), Window(height=1)]), filter=roomy)
        root = FloatContainer(
            content=HSplit(
                [
                    self._header(),
                    self.window,
                    stream_box,
                    bar,
                    hint_bar,
                    self._composer(),
                    gap,
                    self._footer(),
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
