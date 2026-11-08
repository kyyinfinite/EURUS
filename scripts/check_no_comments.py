from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterable
from pathlib import Path

SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "tool-results",
        "venv",
    }
)

CHECKED_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".css",
        ".html",
        ".htm",
        ".sql",
        ".sh",
        ".toml",
        ".ini",
        ".cfg",
        ".yml",
        ".yaml",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".gz",
        ".mp3",
        ".mp4",
        ".wav",
        ".pdf",
        ".pyc",
        ".whl",
    }
)

SHEBANG = re.compile(r"^#!\S")
YAML_COMMENT = re.compile(r"(^|\s)#")


class Violation:
    def __init__(self, path: Path, line: int, text: str) -> None:
        self.path = path
        self.line = line
        self.text = text

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.text}"


def iter_targets(root: Path) -> Iterable[Path]:
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        if item.suffix.lower() in BINARY_SUFFIXES:
            continue
        if item.suffix.lower() not in CHECKED_SUFFIXES:
            continue
        yield item


def python_violations(text: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [(max(exc.lineno or 1, 1), "syntax error", f"syntax error: {exc.msg}")]
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                name = getattr(node, "name", "<module>")
                found.append((first.lineno, "docstring", f"docstring on {name}"))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        line_no = token.start[0]
        line_text = text.splitlines()[line_no - 1] if text.splitlines() else ""
        if line_no == 1 and SHEBANG.match(line_text):
            continue
        found.append((line_no, "comment", f"comment: {token.string[:60]}"))
    return found


def _strip_line_comment(line: str, marker: str) -> str | None:
    index = line.find(marker)
    if index == -1:
        return None
    before = line[:index]
    return before


def hash_comment_violations(
    text: str, marker: str, allow_shebang: bool
) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if allow_shebang and number == 1 and SHEBANG.match(line):
            continue
        target = _strip_line_comment(line, marker)
        if target is None:
            continue
        if not target.strip():
            found.append((number, "comment", f"comment: {line.strip()[:60]}"))
    return found


def c_block_violations(
    text: str, line_marker: str | None, block_start: str, block_end: str
) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    in_block = False
    for number, line in enumerate(lines, start=1):
        if in_block:
            found.append((number, "comment", f"comment: {line.strip()[:60]}"))
            if block_end in line:
                in_block = False
            continue
        if block_start in line:
            closing = line.find(block_end, line.find(block_start) + len(block_start))
            found.append((number, "comment", f"comment: {line.strip()[:60]}"))
            if closing == -1:
                in_block = True
            continue
        if line_marker is not None:
            target = _strip_line_comment(line, line_marker)
            if target is not None and not target.strip():
                found.append((number, "comment", f"comment: {line.strip()[:60]}"))
    return found


def yaml_violations(text: str) -> list[tuple[int, str, str]]:
    return hash_comment_violations(text, "#", allow_shebang=False)


def js_violations(text: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    in_block = False
    quote: str | None = None
    escape = False
    for number, line in enumerate(lines, start=1):
        if in_block:
            found.append((number, "comment", f"comment: {line.strip()[:60]}"))
            if "*/" in line:
                in_block = False
            continue
        index = 0
        while index < len(line):
            char = line[index]
            if quote is not None:
                if escape:
                    escape = False
                    index += 1
                    continue
                if char == "\\":
                    escape = True
                    index += 1
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"', "`"}:
                quote = char
                index += 1
                continue
            if char == "/" and line.startswith("//", index):
                if not line[:index].strip():
                    found.append((number, "comment", f"comment: {line.strip()[:60]}"))
                break
            if char == "/" and line.startswith("/*", index):
                found.append((number, "comment", f"comment: {line.strip()[:60]}"))
                rest = line[index + 2 :]
                if "*/" not in rest:
                    in_block = True
                break
            index += 1
        if quote in {"'", '"'}:
            quote = None
            escape = False
    return found


def scan(path: Path) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return [Violation(path, 1, f"unreadable: {exc}")]
    suffix = path.suffix.lower()
    raw: list[tuple[int, str, str]] = []
    if suffix == ".py":
        raw = python_violations(text)
    elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        raw = js_violations(text)
    elif suffix == ".css":
        raw = c_block_violations(text, None, "/*", "*/")
    elif suffix in {".html", ".htm"}:
        raw = c_block_violations(text, None, "<!--", "-->")
    elif suffix == ".sql":
        raw = c_block_violations(text, "--", "/*", "*/")
    elif suffix == ".sh":
        raw = hash_comment_violations(text, "#", allow_shebang=True)
    elif suffix in {".toml", ".ini", ".cfg", ".yml", ".yaml"}:
        raw = hash_comment_violations(text, "#", allow_shebang=False)
    else:
        raw = []
    return [Violation(path, line, kind) for line, kind, _detail in raw]


def fix(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0
    lines = text.splitlines(keepends=True)
    violations = scan(path)
    drop: set[int] = {item.line for item in violations}
    if not drop:
        return 0
    kept: list[str] = []
    for number, line in enumerate(lines, start=1):
        if number in drop:
            continue
        kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
    return len(drop)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="*", default=["."])
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    targets: list[Path] = []
    for root_name in args.roots:
        root = Path(root_name)
        if root.is_file():
            targets.append(root)
        else:
            targets.extend(iter_targets(root))
    all_violations: list[Violation] = []
    for target in targets:
        all_violations.extend(scan(target))
    if args.fix:
        fixed = 0
        for target in targets:
            fixed += fix(target)
        print(f"removed {fixed} comment lines across {len(targets)} files")
        remaining: list[Violation] = []
        for target in targets:
            remaining.extend(scan(target))
        for item in remaining:
            print(item.render())
        return 1 if remaining else 0
    for item in all_violations:
        print(item.render())
    print(f"{len(all_violations)} violations across {len(targets)} checked files")
    return 1 if all_violations else 0


if __name__ == "__main__":
    sys.exit(main())
