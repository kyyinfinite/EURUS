from __future__ import annotations

import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".cache",
        ".eurus-tmp",
    }
)
MAX_CHILDREN = 2000
SEARCH_BUDGET_ENTRIES = 8000
SEARCH_BUDGET_SECONDS = 0.25
MAX_RECENT = 12
SORT_KEY_CACHE_MAX = 4096


@dataclass(slots=True)
class ExplorerNode:
    relative: str
    name: str
    is_dir: bool
    depth: int
    expanded: bool = False
    loaded: bool = False
    size: int = 0
    modified: float = 0.0
    error: str = ""
    truncated: bool = False
    recent: bool = False

    def sort_key(self) -> tuple[int, str]:
        return (0 if self.is_dir else 1, self.name.lower())


@dataclass(slots=True)
class SearchHit:
    relative: str
    name: str
    is_dir: bool


def _safe_iterdir(path: Path) -> tuple[list[Path], str]:
    try:
        entries = list(path.iterdir())
        return entries, ""
    except PermissionError:
        return [], "permission denied"
    except OSError as exc:
        return [], str(exc)


def _stat_info(path: Path) -> tuple[int, float]:
    try:
        info = path.lstat()
    except OSError:
        return 0, 0.0
    return int(info.st_size), float(info.st_mtime)


class FileExplorer:
    def __init__(self, root: Path, on_change: Callable[[], None] | None = None) -> None:
        self.root = root
        self.on_change = on_change
        self.filter = ""
        self.selected = 0
        self.offset = 0
        self.dirty: set[str] = set()
        self.recent: list[str] = []
        self._expanded: set[str] = set()
        self._rows: list[ExplorerNode] = []
        self._hits: list[SearchHit] = []
        self._search_walked = 0
        self._search_truncated = False
        self.refresh()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def workspace_path(self, relative: str) -> Path:
        return (self.root / relative).resolve()

    def inside_root(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            root = self.root.resolve()
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    def _children_of(self, relative: str) -> tuple[list[ExplorerNode], bool, str]:
        directory = self.root / relative if relative else self.root
        entries, error = _safe_iterdir(directory)
        if error:
            return [], False, error
        visible: list[ExplorerNode] = []
        truncated = False
        for entry in sorted(entries, key=lambda item: (item.is_dir(), item.name.lower())):
            if entry.name in IGNORED_DIRECTORIES and entry.is_dir():
                continue
            if entry.is_symlink():
                continue
            if len(visible) >= MAX_CHILDREN:
                truncated = True
                break
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                continue
            child_relative = f"{relative}/{entry.name}" if relative else entry.name
            if stat.S_ISDIR(mode):
                visible.append(
                    ExplorerNode(
                        child_relative,
                        entry.name,
                        True,
                        0,
                        expanded=child_relative in self._expanded,
                    )
                )
            elif stat.S_ISREG(mode):
                size, modified = _stat_info(entry)
                visible.append(
                    ExplorerNode(
                        child_relative,
                        entry.name,
                        False,
                        0,
                        size=size,
                        modified=modified,
                    )
                )
        for node in visible:
            node.depth = relative.count("/") + 1 if relative else 1
        return visible, truncated, ""

    def refresh(self) -> None:
        if self.filter:
            self._run_search()
            return
        rows: list[ExplorerNode] = []
        root_entries, truncated, error = self._children_of("")
        if error:
            rows.append(ExplorerNode("", error, False, 0, error=error))
            self._rows = rows
            self._clamp_selection()
            return
        if not self._expanded and self.recent:
            rows.append(ExplorerNode("", "recent", False, -1, truncated=True))
            for relative in self.recent:
                if (self.root / relative).is_file():
                    size, modified = _stat_info(self.root / relative)
                    rows.append(
                        ExplorerNode(
                            relative,
                            relative.rsplit("/", 1)[-1],
                            False,
                            0,
                            size=size,
                            modified=modified,
                            recent=True,
                        )
                    )
        stack: list[tuple[list[ExplorerNode], bool]] = [(root_entries, truncated)]
        while stack:
            entries, entry_truncated = stack.pop()
            for node in entries:
                node.expanded = node.relative in self._expanded
                rows.append(node)
                if node.is_dir and node.expanded:
                    children, child_truncated, child_error = self._children_of(node.relative)
                    if child_error:
                        rows.append(
                            ExplorerNode(
                                f"{node.relative}/",
                                child_error,
                                False,
                                node.depth + 1,
                                error=child_error,
                            )
                        )
                    else:
                        stack.append((children, child_truncated))
            if entry_truncated and entries:
                last = entries[-1]
                rows.append(
                    ExplorerNode(
                        f"{last.relative}~",
                        "… more entries hidden",
                        False,
                        last.depth,
                        truncated=True,
                    )
                )
        self._rows = rows
        self._clamp_selection()

    def _run_search(self) -> None:
        needle = self.filter.strip().lower()
        hits: list[SearchHit] = []
        walked = 0
        truncated = False
        started = time.monotonic()
        stack: list[str] = [""]
        while stack:
            relative = stack.pop()
            entries, error = _safe_iterdir(self.root / relative if relative else self.root)
            if error:
                continue
            for entry in sorted(entries, key=lambda item: item.name.lower()):
                if (
                    walked >= SEARCH_BUDGET_ENTRIES
                    or time.monotonic() - started > SEARCH_BUDGET_SECONDS
                ):
                    truncated = True
                    break
                if entry.is_symlink():
                    continue
                name = entry.name
                if entry.is_dir():
                    if name in IGNORED_DIRECTORIES:
                        continue
                    walked += 1
                    child = f"{relative}/{name}" if relative else name
                    if needle in child.lower():
                        hits.append(SearchHit(child, name, True))
                    stack.append(child)
                else:
                    walked += 1
                    child = f"{relative}/{name}" if relative else name
                    if needle in child.lower():
                        hits.append(SearchHit(child, name, False))
            if truncated:
                break
        hits.sort(key=lambda hit: (hit.is_dir, hit.relative.lower()))
        self._hits = hits[:MAX_CHILDREN]
        self._search_walked = walked
        self._search_truncated = truncated
        self._clamp_selection()

    @property
    def rows(self) -> list[ExplorerNode]:
        return self._rows

    @property
    def hits(self) -> list[SearchHit]:
        return self._hits

    @property
    def search_truncated(self) -> bool:
        return self._search_truncated

    def row_count(self) -> int:
        if self.filter:
            return len(self._hits)
        return len(self._rows)

    def selected_relative(self) -> str:
        if self.filter:
            if 0 <= self.selected < len(self._hits):
                return self._hits[self.selected].relative
            return ""
        if 0 <= self.selected < len(self._rows):
            return self._rows[self.selected].relative
        return ""

    def selected_is_dir(self) -> bool:
        if self.filter:
            return 0 <= self.selected < len(self._hits) and self._hits[self.selected].is_dir
        return 0 <= self.selected < len(self._rows) and self._rows[self.selected].is_dir

    def _clamp_selection(self) -> None:
        count = self.row_count()
        self.selected = min(max(self.selected, 0), max(count - 1, 0))
        if self.selected < self.offset:
            self.offset = self.selected
        if count == 0:
            self.offset = 0

    def move(self, delta: int) -> None:
        if self.row_count() == 0:
            return
        self.selected = min(max(self.selected + delta, 0), self.row_count() - 1)
        self._changed()

    def scroll_to(self, visible_height: int) -> None:
        if visible_height <= 0:
            return
        top = self.offset
        bottom = top + visible_height - 1
        if self.selected < top:
            self.offset = self.selected
        elif self.selected > bottom:
            self.offset = self.selected - visible_height + 1
        self.offset = max(min(self.offset, max(self.row_count() - visible_height, 0)), 0)

    def visible_rows(self, visible_height: int) -> Sequence[ExplorerNode | SearchHit]:
        if self.filter:
            return self._hits[self.offset : self.offset + max(visible_height, 1)]
        return self._rows[self.offset : self.offset + max(visible_height, 1)]

    def toggle_selected(self) -> bool:
        if self.filter:
            relative = self.selected_relative()
            if not relative:
                return False
            hit = self._hits[self.selected]
            if hit.is_dir:
                self._expanded.add(relative)
                self.filter = ""
                self.refresh()
                self._select_relative(relative)
                self._changed()
                return True
            return False
        if not (0 <= self.selected < len(self._rows)):
            return False
        node = self._rows[self.selected]
        if node.truncated:
            return False
        if not node.is_dir or node.error:
            return False
        if node.relative in self._expanded:
            self._expanded.discard(node.relative)
        else:
            self._expanded.add(node.relative)
        self.refresh()
        self._select_relative(node.relative)
        self._changed()
        return True

    def collapse_or_parent(self) -> None:
        if self.filter:
            self.filter = ""
            self.refresh()
            self._changed()
            return
        if not (0 <= self.selected < len(self._rows)):
            return
        node = self._rows[self.selected]
        if node.is_dir and node.relative in self._expanded:
            self._expanded.discard(node.relative)
            self.refresh()
            self._select_relative(node.relative)
        elif not node.is_dir and "/" in node.relative:
            parent = node.relative.rsplit("/", 1)[0]
            self.refresh()
            self._select_relative(parent)
        elif not node.is_dir:
            return
        else:
            parent = node.relative.rsplit("/", 1)[0] if "/" in node.relative else ""
            if parent:
                self._expanded.discard(parent)
                self.refresh()
                self._select_relative(parent)
        self._changed()

    def _select_relative(self, relative: str) -> None:
        if self.filter:
            for index, hit in enumerate(self._hits):
                if hit.relative == relative:
                    self.selected = index
                    return
            return
        for index, node in enumerate(self._rows):
            if node.relative == relative and not node.truncated:
                self.selected = index
                return

    def open_selected(self) -> str | None:
        relative = self.selected_relative()
        if not relative or self.selected_is_dir():
            return None
        if self.filter:
            pass
        elif 0 <= self.selected < len(self._rows) and self._rows[self.selected].truncated:
            return None
        path = self.root / relative
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except OSError:
            return None
        if not stat.S_ISREG(mode):
            return None
        if relative in self.recent:
            self.recent.remove(relative)
        self.recent.insert(0, relative)
        del self.recent[MAX_RECENT:]
        if not self._expanded:
            self.refresh()
        self._changed()
        return relative

    def type_filter(self, text: str) -> None:
        self.filter = text
        self.selected = 0
        self.offset = 0
        self.refresh()
        self._changed()

    def backspace_filter(self) -> None:
        self.type_filter(self.filter[:-1])

    def clear_filter(self) -> bool:
        if not self.filter:
            return False
        self.filter = ""
        self.refresh()
        self._changed()
        return True

    def mark_dirty(self, relative: str, is_dirty: bool) -> None:
        if is_dirty:
            self.dirty.add(relative)
        else:
            self.dirty.discard(relative)
        self._changed()

    def is_dirty(self, relative: str) -> bool:
        return relative in self.dirty

    def expand_to(self, relative: str) -> None:
        parts = relative.split("/")[:-1]
        prefix = ""
        for part in parts:
            prefix = f"{prefix}/{part}" if prefix else part
            self._expanded.add(prefix)
        self.refresh()
        self._select_relative(relative)
        self._changed()

    def format_size(self, size: int) -> str:
        if size >= 1_000_000:
            return f"{size / 1_000_000:.1f}M"
        if size >= 1_000:
            return f"{size / 1_000:.0f}K"
        return str(size)

    def describe_selection(self) -> str:
        relative = self.selected_relative()
        if not relative:
            return self.filter and f"no matches for {self.filter}" or "empty workspace"
        if self.filter:
            hit = self._hits[self.selected]
            return hit.relative
        node = self._rows[self.selected]
        if node.is_dir:
            return f"{node.relative}/"
        if node.error:
            return node.error
        return f"{node.relative} · {self.format_size(node.size)}"

    def rescan(self) -> None:
        self.refresh()
        self._changed()
