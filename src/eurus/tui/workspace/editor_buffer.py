from __future__ import annotations

from dataclasses import dataclass, field

MAX_UNDO = 100
TAB_WIDTH = 4

Cursor = tuple[int, int]


@dataclass(slots=True)
class EditDelta:
    start: int
    removed: list[str]
    added: list[str]
    cursor_before: Cursor
    cursor_after: Cursor


@dataclass(slots=True)
class FindMatch:
    row: int
    col: int
    length: int


@dataclass(slots=True)
class EditorBuffer:
    lines: list[str] = field(default_factory=lambda: [""])
    cursor: Cursor = (0, 0)
    anchor: Cursor | None = None
    undo_stack: list[EditDelta] = field(default_factory=list)
    redo_stack: list[EditDelta] = field(default_factory=list)
    modified: int = 0

    @classmethod
    def from_text(cls, text: str) -> EditorBuffer:
        return cls(lines=text.split("\n"))

    def to_text(self) -> str:
        return "\n".join(self.lines)

    @property
    def row(self) -> int:
        return self.cursor[0]

    @property
    def col(self) -> int:
        return self.cursor[1]

    def clamp_cursor(self) -> None:
        row, col = self.cursor
        row = min(max(row, 0), len(self.lines) - 1)
        col = min(max(col, 0), len(self.lines[row]))
        self.cursor = (row, col)

    def _record(self, delta: EditDelta) -> None:
        self.undo_stack.append(delta)
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.modified += 1

    def _splice(self, start: int, removed: list[str], added: list[str]) -> None:
        self.lines[start : start + len(removed)] = added

    def apply_delta(self, delta: EditDelta, record: bool = True) -> None:
        self._splice(delta.start, delta.removed, delta.added)
        if record:
            self._record(delta)
        self.cursor = delta.cursor_after
        self.clamp_cursor()

    def insert_text(self, text: str) -> None:
        if not text:
            return
        row, col = self.cursor
        current = self.lines[row]
        head = current[:col]
        tail = current[col:]
        pieces = text.split("\n")
        if len(pieces) == 1:
            added = [head + pieces[0] + tail]
            cursor_after = (row, col + len(pieces[0]))
        else:
            added = [head + pieces[0]]
            added.extend(pieces[1:-1])
            added.append(pieces[-1] + tail)
            cursor_after = (row + len(pieces) - 1, len(pieces[-1]))
        delta = EditDelta(
            start=row,
            removed=[current],
            added=added,
            cursor_before=(row, col),
            cursor_after=cursor_after,
        )
        self.apply_delta(delta)

    def backspace(self) -> None:
        row, col = self.cursor
        if row == 0 and col == 0:
            return
        if col > 0:
            current = self.lines[row]
            removed = [current]
            added = [current[: col - 1] + current[col:]]
            delta = EditDelta(
                start=row,
                removed=removed,
                added=added,
                cursor_before=(row, col),
                cursor_after=(row, col - 1),
            )
        else:
            previous = self.lines[row - 1]
            current = self.lines[row]
            delta = EditDelta(
                start=row - 1,
                removed=[previous, current],
                added=[previous + current],
                cursor_before=(row, col),
                cursor_after=(row - 1, len(previous)),
            )
        self.apply_delta(delta)

    def delete_forward(self) -> None:
        row, col = self.cursor
        current = self.lines[row]
        if col < len(current):
            delta = EditDelta(
                start=row,
                removed=[current],
                added=[current[:col] + current[col + 1 :]],
                cursor_before=(row, col),
                cursor_after=(row, col),
            )
        elif row + 1 < len(self.lines):
            following = self.lines[row + 1]
            delta = EditDelta(
                start=row,
                removed=[current, following],
                added=[current + following],
                cursor_before=(row, col),
                cursor_after=(row, col),
            )
        else:
            return
        self.apply_delta(delta)

    def delete_selection(self) -> None:
        start, end = self.selection_range()
        if start == end:
            return
        srow, scol = start
        erow, ecol = end
        first = self.lines[srow]
        last = self.lines[erow]
        removed = self.lines[srow : erow + 1]
        joined = first[:scol] + last[ecol:]
        delta = EditDelta(
            start=srow,
            removed=removed,
            added=[joined],
            cursor_before=self.cursor,
            cursor_after=(srow, scol),
        )
        self.apply_delta(delta)
        self.anchor = None

    def selection_range(self) -> tuple[Cursor, Cursor]:
        if self.anchor is None:
            return self.cursor, self.cursor
        start, end = sorted((self.cursor, self.anchor))
        return start, end

    def selected_text(self) -> str:
        start, end = self.selection_range()
        if start == end:
            return ""
        srow, scol = start
        erow, ecol = end
        if srow == erow:
            return self.lines[srow][scol:ecol]
        parts = [self.lines[srow][scol:]]
        parts.extend(self.lines[srow + 1 : erow])
        parts.append(self.lines[erow][:ecol])
        return "\n".join(parts)

    def select_all(self) -> None:
        self.anchor = (0, 0)
        self.cursor = (len(self.lines) - 1, len(self.lines[-1]))

    def clear_selection(self) -> None:
        self.anchor = None

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        delta = self.undo_stack.pop()
        inverse = EditDelta(
            start=delta.start,
            removed=delta.added,
            added=delta.removed,
            cursor_before=delta.cursor_after,
            cursor_after=delta.cursor_before,
        )
        self._splice(inverse.start, inverse.removed, inverse.added)
        self.cursor = inverse.cursor_after
        self.clamp_cursor()
        self.redo_stack.append(delta)
        self.modified += 1
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        delta = self.redo_stack.pop()
        self._splice(delta.start, delta.removed, delta.added)
        self.cursor = delta.cursor_after
        self.clamp_cursor()
        self.undo_stack.append(delta)
        self.modified += 1
        return True

    def move(self, row_delta: int, col_delta: int, extend: bool = False) -> None:
        if not extend:
            self.anchor = None
        elif self.anchor is None:
            self.anchor = self.cursor
        row, col = self.cursor
        row = min(max(row + row_delta, 0), len(self.lines) - 1)
        if col_delta:
            target = self.lines[row]
            col = min(max(col + col_delta, 0), len(target))
        elif row_delta:
            col = min(col, len(self.lines[row]))
        self.cursor = (row, col)

    def move_word(self, direction: int, extend: bool = False) -> None:
        if not extend:
            self.anchor = None
        elif self.anchor is None:
            self.anchor = self.cursor
        row, col = self.cursor
        line = self.lines[row]
        if direction < 0:
            if col == 0:
                if row > 0:
                    self.cursor = (row - 1, len(self.lines[row - 1]))
                return
            index = col - 1
            while index > 0 and line[index - 1].isalnum():
                index -= 1
            while index > 0 and not line[index - 1].isalnum() and line[index - 1] != " ":
                index -= 1
            self.cursor = (row, index)
        else:
            if col >= len(line):
                if row + 1 < len(self.lines):
                    self.cursor = (row + 1, 0)
                return
            index = col
            while index < len(line) and line[index].isalnum():
                index += 1
            while index < len(line) and not line[index].isalnum():
                index += 1
            self.cursor = (row, index)

    def move_home(self, extend: bool = False) -> None:
        if not extend:
            self.anchor = None
        elif self.anchor is None:
            self.anchor = self.cursor
        self.cursor = (self.row, 0)

    def move_end(self, extend: bool = False) -> None:
        if not extend:
            self.anchor = None
        elif self.anchor is None:
            self.anchor = self.cursor
        row, _ = self.cursor
        self.cursor = (row, len(self.lines[row]))

    def move_page(self, step: int, page_height: int, extend: bool = False) -> None:
        self.move(step * max(page_height - 1, 1), 0, extend=extend)

    def goto(self, row: int, col: int) -> None:
        self.cursor = (max(row, 0), max(col, 0))
        self.clamp_cursor()
        self.anchor = None

    def insert_tab(self) -> None:
        self.insert_text(" " * TAB_WIDTH)

    def find_matches(self, query: str, case_sensitive: bool = False) -> list[FindMatch]:
        if not query:
            return []
        matches: list[FindMatch] = []
        haystack = query if case_sensitive else query.lower()
        for row, line in enumerate(self.lines):
            content = line if case_sensitive else line.lower()
            start = 0
            while True:
                col = content.find(haystack, start)
                if col < 0:
                    break
                matches.append(FindMatch(row, col, len(query)))
                start = col + max(len(query), 1)
        return matches

    def find_next(self, query: str, case_sensitive: bool = False) -> FindMatch | None:
        matches = self.find_matches(query, case_sensitive)
        if not matches:
            return None
        row, col = self.cursor
        for match in matches:
            if (match.row, match.col) > (row, col):
                self.goto(match.row, match.col)
                self.anchor = (match.row, match.col + match.length)
                return match
        first = matches[0]
        self.goto(first.row, first.col)
        self.anchor = (first.row, first.col + first.length)
        return first

    def replace_next(self, query: str, replacement: str) -> bool:
        match = self.find_next(query)
        if match is None:
            return False
        row, col = self.cursor
        current = self.lines[row]
        delta = EditDelta(
            start=row,
            removed=[current],
            added=[current[:col] + replacement + current[col + match.length :]],
            cursor_before=(row, col),
            cursor_after=(row, col + len(replacement)),
        )
        self.apply_delta(delta)
        self.anchor = None
        return True

    def replace_all(self, query: str, replacement: str) -> int:
        count = 0
        while self.replace_next(query, replacement):
            count += 1
            if count > 100000:
                break
        return count
