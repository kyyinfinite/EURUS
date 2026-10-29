from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterator
from typing import IO, TYPE_CHECKING, cast

from rich.console import Console

from eurus.config import Config
from eurus.tui.capabilities import detect
from eurus.tui.renderer import COLOR_SYSTEMS, ColorSystem, StreamView, TerminalRenderer
from eurus.tui.theme import Theme
from eurus.tui.transcript import TranscriptSink

if TYPE_CHECKING:
    from eurus.tui.session import InteractiveSession

__all__ = ["FullscreenRenderer", "attach", "pane_width"]


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
