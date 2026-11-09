from __future__ import annotations

from dataclasses import dataclass

from eurus.tui.workspace.store import WorkspaceStore

MIN_EXPLORER_WIDTH = 18
MIN_ACTIVITY_WIDTH = 24
MIN_MAIN_WIDTH = 40
SPLITTER_WIDTH = 1
WORKSPACE_MIN_COLUMNS = 100
WORKSPACE_MIN_ROWS = 22


@dataclass(frozen=True, slots=True)
class PanelFrame:
    left: int
    top: int
    width: int
    height: int

    def contains(self, column: int, row: int) -> bool:
        return (
            self.left <= column < self.left + self.width
            and self.top <= row < self.top + self.height
        )


@dataclass(frozen=True, slots=True)
class PanelLayout:
    columns: int
    rows: int
    explorer: PanelFrame | None
    activity: PanelFrame | None
    main: PanelFrame
    explorer_splitter: int | None
    activity_splitter: int | None

    @property
    def panels(self) -> tuple[PanelFrame, ...]:
        return tuple(frame for frame in (self.explorer, self.main, self.activity) if frame)


def workspace_supported(columns: int) -> bool:
    return columns >= WORKSPACE_MIN_COLUMNS


def clamp_panel_width(
    requested: int,
    minimum: int,
    available: int,
) -> int:
    if available < minimum:
        return 0
    return min(max(requested, minimum), available)


def compute_layout(columns: int, rows: int, store: WorkspaceStore) -> PanelLayout:
    fixed_columns = columns
    frames: dict[str, PanelFrame | None] = {"explorer": None, "activity": None}
    splitters: dict[str, int | None] = {"explorer": None, "activity": None}
    remaining = fixed_columns
    if store.explorer_visible:
        activity_room = MIN_ACTIVITY_WIDTH + SPLITTER_WIDTH if store.activity_visible else 0
        room = remaining - MIN_MAIN_WIDTH - activity_room
        width = clamp_panel_width(store.explorer_width, MIN_EXPLORER_WIDTH, room)
        if width > 0:
            frames["explorer"] = PanelFrame(0, 0, width, rows)
            remaining -= width + SPLITTER_WIDTH
            splitters["explorer"] = width
    if store.activity_visible:
        room = remaining - MIN_MAIN_WIDTH
        width = clamp_panel_width(store.activity_width, MIN_ACTIVITY_WIDTH, room)
        if width > 0:
            frames["activity"] = PanelFrame(columns - width, 0, width, rows)
            remaining -= width + SPLITTER_WIDTH
            splitters["activity"] = columns - width - SPLITTER_WIDTH
    main_width = max(remaining, MIN_MAIN_WIDTH)
    main = PanelFrame(
        (splitters["explorer"] + SPLITTER_WIDTH) if splitters["explorer"] is not None else 0,
        0,
        main_width,
        rows,
    )
    return PanelLayout(
        columns=columns,
        rows=rows,
        explorer=frames["explorer"],
        activity=frames["activity"],
        main=main,
        explorer_splitter=splitters["explorer"],
        activity_splitter=splitters["activity"],
    )
