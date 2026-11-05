from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from eurus.coding.project_index import IGNORED_DIRS, ModuleInfo, index_project

MAX_SUMMARY_CHARS = 6_000
MAX_MANIFESTS = 12
MAX_TEST_DIRS = 8
MAX_DOCS = 8
MAX_ENTRY_POINTS = 8
MAX_LANGUAGE_EXTENSIONS = 8
SCAN_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".cs",
}
MANIFEST_NAMES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
)
TEST_DIR_NAMES = ("tests", "test", "spec", "e2e")
DOC_PATTERNS = ("README*", "ARCHITECTURE*", "CONTRIBUTING*", "docs/*.md")
ENTRY_POINT_NAMES = (
    "main.py",
    "__main__.py",
    "cli.py",
    "app.py",
    "server.py",
    "main.go",
    "index.ts",
    "index.js",
)


@dataclass(frozen=True, slots=True)
class ProjectUnderstanding:
    root: str
    manifests: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    test_dirs: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    module_count: int = 0
    parse_errors: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "manifests": list(self.manifests),
            "languages": list(self.languages),
            "test_dirs": list(self.test_dirs),
            "docs": list(self.docs),
            "entry_points": list(self.entry_points),
            "module_count": self.module_count,
            "parse_errors": self.parse_errors,
            "notes": list(self.notes),
            "summary": self.summary,
        }


def understand_project(
    root: Path | str,
    *,
    max_files: int = 2_000,
    max_chars: int = MAX_SUMMARY_CHARS,
) -> ProjectUnderstanding:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("workspace must be an existing directory")
    if not 200 <= max_chars <= MAX_SUMMARY_CHARS:
        raise ValueError("max_chars must be between 200 and 6000")

    modules = index_project(base, max_files=max_files)
    manifests = _find_manifests(base)
    languages = _detect_languages(base)
    test_dirs = _find_test_dirs(base)
    docs = _find_docs(base)
    entry_points = _find_entry_points(base)
    parse_errors = sum(1 for module in modules if module.parse_error)

    notes: list[str] = []
    if len(modules) >= max_files:
        notes.append(f"file scan stopped at max_files={max_files}; the map may be partial")
    if parse_errors:
        notes.append(f"{parse_errors} Python file(s) failed to parse")
    if not manifests:
        notes.append("no project manifest found; treat build/test commands as unverified")
    if not test_dirs:
        notes.append("no test directory detected; regression planning needs user input")

    understanding = ProjectUnderstanding(
        root=str(base),
        manifests=manifests,
        languages=languages,
        test_dirs=test_dirs,
        docs=docs,
        entry_points=entry_points,
        module_count=len(modules),
        parse_errors=parse_errors,
        notes=tuple(notes),
    )
    understanding = replace_summary(
        understanding, _render(understanding, modules, max_chars=max_chars)
    )
    return understanding


def replace_summary(understanding: ProjectUnderstanding, summary: str) -> ProjectUnderstanding:
    object.__setattr__(understanding, "summary", summary)
    return understanding


def _walk_limited(base: Path) -> list[str]:
    collected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for name in sorted(filenames):
            collected.append(os.path.relpath(Path(dirpath) / name, base))
    return collected


def _find_manifests(base: Path) -> tuple[str, ...]:
    found = [name for name in MANIFEST_NAMES if (base / name).is_file()]
    return tuple(found[:MAX_MANIFESTS])


def _detect_languages(base: Path) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for relative in _walk_limited(base):
        suffix = Path(relative).suffix.lower()
        if suffix in SCAN_EXTENSIONS:
            counts[suffix] = counts.get(suffix, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(suffix.lstrip(".") for suffix, _ in ordered[:MAX_LANGUAGE_EXTENSIONS])


def _find_test_dirs(base: Path) -> tuple[str, ...]:
    found: list[str] = []
    for dirpath, dirnames, _ in os.walk(base, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for name in dirnames:
            if name in TEST_DIR_NAMES:
                found.append(os.path.relpath(Path(dirpath) / name, base))
        if len(found) >= MAX_TEST_DIRS:
            break
    return tuple(sorted(found)[:MAX_TEST_DIRS])


def _find_docs(base: Path) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in DOC_PATTERNS:
        for path in sorted(base.glob(pattern)):
            if path.is_file():
                found.append(path.relative_to(base).as_posix())
        if len(found) >= MAX_DOCS:
            break
    return tuple(found[:MAX_DOCS])


def _find_entry_points(base: Path) -> tuple[str, ...]:
    found: list[str] = []
    for relative in _walk_limited(base):
        name = Path(relative).name
        if name in ENTRY_POINT_NAMES:
            found.append(relative)
        if len(found) >= MAX_ENTRY_POINTS:
            break
    return tuple(sorted(found)[:MAX_ENTRY_POINTS])


def _render(
    understanding: ProjectUnderstanding,
    modules: list[ModuleInfo],
    *,
    max_chars: int,
) -> str:
    lines: list[str] = [
        f"Repository map for {understanding.root}",
        f"- modules indexed: {understanding.module_count} (Python)",
        f"- languages: {', '.join(understanding.languages) or 'unknown'}",
        f"- manifests: {', '.join(understanding.manifests) or 'none found'}",
        f"- test locations: {', '.join(understanding.test_dirs) or 'none detected'}",
        f"- docs: {', '.join(understanding.docs) or 'none detected'}",
        (
            "- entry points: "
            + (", ".join(understanding.entry_points) if understanding.entry_points else "none")
        ),
        "Navigation hints (verify files before editing):",
    ]
    for module in modules[:24]:
        definitions = ", ".join(module.definitions[:8]) or "no top-level definitions"
        line = f"- {module.path}: {definitions}"
        if sum(map(len, lines)) + len(line) > max_chars:
            break
        lines.append(line)
    if understanding.notes:
        lines.append("Notes:")
        for note in understanding.notes:
            lines.append(f"- {note}")
    if len(lines) > 200:
        lines = lines[:200]
    summary = "\n".join(lines)
    return summary[:max_chars]
