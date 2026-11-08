from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples

from eurus.tui.layout import cell_clip, cell_len
from eurus.tui.theme import Theme

LABEL_ROOM = 12
SHORTCUT_ROOM = 7
MIN_DESC_WIDTH = 44
LABEL_COL = 5
DESC_COL = 18


@dataclass(frozen=True, slots=True)
class PaletteItem:
    label: str
    description: str = ""
    shortcut: str = ""
    action: str = ""
    payload: str = ""
    category: str = ""
    value: str = ""
    icon: str = ""
    header: bool = False


def header_item(category: str) -> PaletteItem:
    return PaletteItem("", category=category, header=True)


class ListOverlay:
    def __init__(
        self,
        items: list[PaletteItem] | tuple[PaletteItem, ...],
        theme: Theme,
        title: str = "",
        max_rows: int = 7,
        buffer: Buffer | None = None,
        raw_hint: str = "",
    ) -> None:
        self.items: tuple[PaletteItem, ...] = tuple(items)
        self.theme = theme
        self.title = title
        self.max_rows = max_rows
        self.index = 0
        self.buffer = buffer
        self.raw_hint = raw_hint
        if buffer is not None:
            buffer.on_text_changed += self._changed

    def _changed(self, _: Buffer) -> None:
        self.index = 0

    def detach(self) -> None:
        if self.buffer is not None:
            self.buffer.on_text_changed -= self._changed
            self.buffer = None

    def set_items(self, items: list[PaletteItem] | tuple[PaletteItem, ...]) -> None:
        self.items = tuple(items)
        self.index = min(max(self.index, 0), max(len(self.selectable()) - 1, 0))

    @property
    def query(self) -> str:
        return self.buffer.text if self.buffer is not None else ""

    def matches(self) -> list[PaletteItem]:
        needle = self.query.strip().lower()
        if not needle:
            return list(self.items)
        tokens = needle.split()
        found: list[PaletteItem] = []
        for item in self.items:
            if item.header:
                continue
            hay = f"{item.label} {item.description} {item.category}".lower()
            if all(token in hay for token in tokens):
                found.append(item)
        return found

    def rows(self) -> list[PaletteItem]:
        if self.query.strip():
            return self.matches()
        return list(self.items)

    def selectable(self) -> list[PaletteItem]:
        return [item for item in self.rows() if not item.header]

    def selected(self) -> PaletteItem | None:
        options = self.selectable()
        if not options:
            return None
        self.index = min(max(self.index, 0), len(options) - 1)
        return options[self.index]

    def move(self, delta: int) -> None:
        options = self.selectable()
        if not options:
            return
        self.index = min(max(self.index + delta, 0), len(options) - 1)

    def window(self) -> list[PaletteItem]:
        rows = self.rows()
        if len(rows) <= self.max_rows:
            return rows
        target = self.selected()
        position = rows.index(target) if target in rows else 0
        start = max(min(position - self.max_rows // 2, len(rows) - self.max_rows), 0)
        return rows[start : start + self.max_rows]

    def line_count(self) -> int:
        count = len(self.window())
        if not self.selectable():
            count += 1
        return max(count, 1)

    def _item_row(self, item: PaletteItem, width: int, selected: bool) -> StyleAndTextTuples:
        ellipsis = self.theme.glyphs.ellipsis
        selected_style = "class:palette-selected"
        label_style = selected_style if selected else "class:palette-item"
        icon_style = selected_style if selected else "class:palette-icon"
        desc_style = selected_style if selected else "class:palette-desc"
        key_style = selected_style if selected else "class:palette-key"
        icon = cell_clip(item.icon, 1, "") if item.icon else " "
        right = cell_clip(item.shortcut or item.value, SHORTCUT_ROOM, ellipsis)
        out: StyleAndTextTuples = [(icon_style, f"  {icon}  ")]
        if width < MIN_DESC_WIDTH:
            label = cell_clip(item.label, max(width - 5 - len(right) - 2, 4), ellipsis)
            out.append((label_style, label))
            pad = width - 5 - cell_len(label) - (len(right) + 1 if right else 0)
            if pad > 0:
                out.append((label_style, " " * pad))
            if right:
                out.append((key_style, f"{right} "))
            return out
        label = cell_clip(item.label, LABEL_ROOM, ellipsis)
        out.append((label_style, label.ljust(DESC_COL - LABEL_COL)))
        desc_room = max(width - DESC_COL - len(right) - 2, 6)
        desc = cell_clip(item.description, desc_room, ellipsis)
        if desc:
            out.append((desc_style, desc))
        pad = width - DESC_COL - cell_len(desc) - (len(right) + 2 if right else 2)
        if pad > 0:
            out.append((desc_style, " " * pad))
        out.append((key_style, f" {right} "))
        return out

    def render(self, width: int) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        target = self.selected()
        for row in self.window():
            if row.header:
                out.append(("class:palette-category", f" {row.category.upper()}"))
                out.append(("", "\n"))
                continue
            out.extend(self._item_row(row, width, row is target))
            out.append(("", "\n"))
        if target is None:
            query = cell_clip(self.query.strip(), max(width - 24, 8), self.theme.glyphs.ellipsis)
            out.append(("class:palette-desc", f" No matches for '{query}'"))
            out.append(("", "\n"))
        return out
