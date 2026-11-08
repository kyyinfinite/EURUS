from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from eurus.tui.workspace.explorer import IGNORED_DIRECTORIES

INDEX_BUDGET_ENTRIES = 12000
INDEX_BUDGET_SECONDS = 0.5
MAX_RESULTS = 40


@dataclass(slots=True)
class FileIndex:
    root: Path
    files: list[str] = field(default_factory=list)
    built_at: float = 0.0
    truncated: bool = False

    def matches(self, query: str, limit: int = MAX_RESULTS) -> list[str]:
        needle = query.strip().lower()
        if not needle:
            return self.files[:limit]
        scored: list[tuple[int, int, str]] = []
        for position, relative in enumerate(self.files):
            score = fuzzy_score(relative.lower(), needle)
            if score >= 0:
                scored.append((score, position, relative))
        scored.sort()
        return [relative for _, _, relative in scored[:limit]]

    def newest_entries(self, limit: int = 8) -> list[str]:
        return self.files[:limit]


def fuzzy_score(candidate: str, needle: str) -> int:
    if not needle:
        return 0
    score = 0
    index = 0
    consecutive = 0
    for char in needle:
        found = candidate.find(char, index)
        if found < 0:
            return -1
        if found == index and index > 0:
            consecutive += 2
        score += found - index + 1
        index = found + 1
    base = candidate.rfind("/") + 1
    basename = candidate[base:]
    if basename.lower().startswith(needle):
        score = max(score - len(candidate), 1)
    return max(score - consecutive, 0)


def build_index(root: Path) -> FileIndex:
    index = FileIndex(root=root)
    started = time.monotonic()
    files: list[str] = []
    truncated = False
    stack: list[str] = [""]
    walked = 0
    while stack:
        relative = stack.pop()
        try:
            entries = list((root / relative).iterdir() if relative else root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if walked >= INDEX_BUDGET_ENTRIES or time.monotonic() - started > INDEX_BUDGET_SECONDS:
                truncated = True
                break
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    if entry.name in IGNORED_DIRECTORIES or entry.name.startswith("."):
                        continue
                    child = f"{relative}/{entry.name}" if relative else entry.name
                    walked += 1
                    stack.append(child)
                    continue
                if entry.name.startswith("."):
                    continue
            except OSError:
                continue
            walked += 1
            child = f"{relative}/{entry.name}" if relative else entry.name
            files.append(child)
        if truncated:
            break
    files.sort()
    index.files = files
    index.built_at = time.monotonic()
    index.truncated = truncated
    return index
