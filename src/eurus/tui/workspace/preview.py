from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eurus.coding.paths import MAX_FILE_BYTES, read_text_at
from eurus.errors import ToolError

BINARY_SNIFF_BYTES = 8192


@dataclass(slots=True)
class LoadedFile:
    relative: str
    content: str
    error: str = ""
    binary: bool = False
    size: int = 0
    too_large: bool = False

    @property
    def lines(self) -> list[str]:
        if self.error or self.binary or self.too_large:
            return []
        return self.content.split("\n")


def load_preview(root: Path, relative: str) -> LoadedFile:
    path = root / relative
    try:
        size = path.stat().st_size
    except OSError as exc:
        return LoadedFile(relative, "", error=str(exc))
    try:
        with open(path, "rb") as handle:
            head = handle.read(BINARY_SNIFF_BYTES)
    except OSError as exc:
        return LoadedFile(relative, "", error=str(exc))
    if b"\x00" in head:
        return LoadedFile(relative, "", binary=True, size=size)
    if size > MAX_FILE_BYTES:
        return LoadedFile(relative, "", too_large=True, size=size)
    try:
        content = read_text_at(root, relative)
    except ToolError as exc:
        return LoadedFile(relative, "", error=str(exc), size=size)
    return LoadedFile(relative, content, size=size)


class FilePreview:
    def __init__(self, root: Path, relative: str) -> None:
        self.root = root
        self.relative = relative
        self.loaded = load_preview(root, relative)
        self.offset = 0
        self.max_line = max(len(self.loaded.lines), 1)
        self.number_width = max(len(str(self.max_line)), 3)

    @property
    def title(self) -> str:
        return self.relative.rsplit("/", 1)[-1]

    def scroll(self, delta: int, visible_height: int) -> None:
        limit = max(len(self.loaded.lines) - visible_height, 0)
        self.offset = min(max(self.offset + delta, 0), limit)

    def ensure_visible(self, line_index: int, visible_height: int) -> None:
        if visible_height <= 0:
            return
        top = self.offset
        bottom = top + visible_height - 1
        if line_index < top:
            self.offset = max(line_index, 0)
        elif line_index > bottom:
            self.offset = max(line_index - visible_height + 1, 0)
        limit = max(len(self.loaded.lines) - visible_height, 0)
        self.offset = min(self.offset, limit)
