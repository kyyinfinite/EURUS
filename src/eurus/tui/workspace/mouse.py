from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

DOUBLE_CLICK_SECONDS = 0.4


@dataclass(slots=True)
class DragState:
    side: str = ""
    active: bool = False


def click_handler(action: Callable[[], None]) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_UP:
            action()

    return handler


def double_click_handler(
    state: dict[str, float | None], action: Callable[[], None]
) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type != MouseEventType.MOUSE_UP:
            return
        now = time.monotonic()
        last = state.get("last")
        if last is not None and now - last < DOUBLE_CLICK_SECONDS:
            action()
            state["last"] = None
        else:
            state["last"] = now

    return handler


def wheel_handler(
    scroll_up: Callable[[], None], scroll_down: Callable[[], None]
) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            scroll_up()
        elif event.event_type == MouseEventType.SCROLL_DOWN:
            scroll_down()

    return handler


def splitter_handler(
    drag: DragState,
    side: str,
    on_down: Callable[[], None],
    on_drag: Callable[[], None],
    on_up: Callable[[], None],
) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_DOWN:
            drag.side = side
            drag.active = True
            on_down()
        elif event.event_type == MouseEventType.MOUSE_UP:
            if drag.active:
                drag.active = False
                on_up()
        elif event.event_type == MouseEventType.MOUSE_MOVE and drag.active and drag.side == side:
            on_drag()

    return handler


def drag_move_handler(
    drag: DragState, apply_width: Callable[[MouseEvent], None]
) -> Callable[[MouseEvent], None]:
    def handler(event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_MOVE and drag.active:
            apply_width(event)
        elif event.event_type == MouseEventType.MOUSE_UP and drag.active:
            drag.active = False

    return handler


def unused_marker() -> Any:
    return None
