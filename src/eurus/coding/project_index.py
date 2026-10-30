from __future__ import annotations

import ast
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
}
MAX_INDEX_FILE_BYTES = 1_000_000
REFRESH_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    path: str
    imports: tuple[str, ...]
    definitions: tuple[str, ...]
    parse_error: str = ""


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _walk_python_files(root: Path, *, max_files: int) -> list[Path]:
    """Collect up to max_files *.py paths, pruning ignored directories during the walk."""
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                if not _inside(root, resolved) or path.stat().st_size > MAX_INDEX_FILE_BYTES:
                    continue
                candidates.append(path)
            except (OSError, ValueError):
                continue
            if len(candidates) >= max_files:
                return candidates
    return candidates


def index_project(workspace: Path, *, max_files: int = 2_000) -> list[ModuleInfo]:
    """Create a lightweight, stdlib-only index of Python files in a workspace."""
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    if not 1 <= max_files <= 20_000:
        raise ValueError("max_files must be between 1 and 20000")

    candidates = _walk_python_files(root, max_files=max_files)
    return [_parse_module(path, root) for path in sorted(candidates)]


def _parse_module(path: Path, root: Path) -> ModuleInfo:
    relative_name = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_name)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return ModuleInfo(relative_name, (), (), f"{type(exc).__name__}: {exc}")

    imports: set[str] = set()
    definitions: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node.name)
    return ModuleInfo(
        path=relative_name,
        imports=tuple(sorted(imports)),
        definitions=tuple(sorted(set(definitions))),
    )


def render_project_index(workspace: Path, *, max_modules: int = 40, max_chars: int = 6_000) -> str:
    modules = index_project(workspace)
    return _render_modules(modules, max_modules=max_modules, max_chars=max_chars)


def _render_modules(modules: list[ModuleInfo], *, max_modules: int, max_chars: int) -> str:
    lines = ["Workspace Python index (navigation hints; verify files before editing):"]
    for module in modules[:max_modules]:
        definitions = ", ".join(module.definitions[:12]) or "no top-level definitions"
        imports = ", ".join(module.imports[:6])
        line = f"- {module.path}: {definitions}"
        if imports:
            line += f" | imports: {imports}"
        if module.parse_error:
            line += f" | parse issue: {module.parse_error[:100]}"
        if sum(map(len, lines)) + len(line) + len(lines) > max_chars:
            lines.append("- Additional modules omitted to keep context bounded.")
            break
        lines.append(line)
    has_omission_note = any("Additional modules omitted" in line for line in lines)
    if len(modules) > max_modules and not has_omission_note:
        lines.append(f"- {len(modules) - max_modules} more modules not listed.")
    return "\n".join(lines)


@dataclass
class _CacheEntry:
    rendered: str = ""
    modules: dict[str, ModuleInfo] = field(default_factory=dict)
    stamps: dict[str, tuple[int, int]] = field(default_factory=dict)
    checked_at: float = 0.0
    computing: bool = False


class ProjectIndexCache:
    """Non-blocking workspace index with incremental background refresh.

    ``render`` never walks or parses on the calling thread: the first call
    schedules a background build and returns "" until the index is ready;
    later calls return the previous render while a stale copy refreshes in the
    background. Files whose (mtime_ns, size) stamp is unchanged are reused
    without re-parsing.
    """

    def __init__(
        self, *, max_files: int = 2_000, refresh_interval: float = REFRESH_INTERVAL_SECONDS
    ) -> None:
        self._max_files = max_files
        self._refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}

    def _entry(self, root: str) -> _CacheEntry:
        with self._lock:
            entry = self._entries.get(root)
            if entry is None:
                entry = _CacheEntry()
                self._entries[root] = entry
            return entry

    def render(self, workspace: Path) -> str:
        root = workspace.expanduser().resolve()
        if not root.is_dir():
            return ""
        entry = self._entry(str(root))
        now = time.monotonic()
        fresh = entry.stamps and now - entry.checked_at < self._refresh_interval
        if fresh or entry.computing:
            return entry.rendered
        entry.computing = True
        thread = threading.Thread(target=self._compute, args=(root, entry), daemon=True)
        thread.start()
        return entry.rendered

    def _compute(self, root: Path, entry: _CacheEntry) -> None:
        try:
            modules, stamps = self._index_incremental(root, entry)
            entry.modules = modules
            entry.stamps = stamps
            entry.rendered = _render_modules(
                list(modules.values()), max_modules=40, max_chars=6_000
            )
            entry.checked_at = time.monotonic()
        except (OSError, ValueError):
            pass
        finally:
            entry.computing = False

    def _index_incremental(
        self, root: Path, entry: _CacheEntry
    ) -> tuple[dict[str, ModuleInfo], dict[str, tuple[int, int]]]:
        candidates = _walk_python_files(root, max_files=self._max_files)
        modules: dict[str, ModuleInfo] = {}
        stamps: dict[str, tuple[int, int]] = {}
        for path in sorted(candidates):
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
            stamps[relative] = stamp
            if entry.stamps.get(relative) == stamp:
                cached = entry.modules.get(relative)
                if cached is not None:
                    modules[relative] = cached
                    continue
            modules[relative] = _parse_module(path, root)
        return modules, stamps

    def wait_ready(self, workspace: Path, timeout: float = 10.0) -> bool:
        """Block until the background build finishes (used by tests and benchmarks)."""
        entry = self._entry(str(workspace.expanduser().resolve()))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if entry.stamps and not entry.computing:
                return True
            time.sleep(0.01)
        return bool(entry.stamps) and not entry.computing


_CACHE = ProjectIndexCache()


def render_project_index_cached(workspace: Path) -> str:
    """Non-blocking render used on request paths; see ProjectIndexCache."""
    return _CACHE.render(workspace)


def cache_wait_ready(workspace: Path, timeout: float = 10.0) -> bool:
    return _CACHE.wait_ready(workspace, timeout)
