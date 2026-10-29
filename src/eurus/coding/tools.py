from __future__ import annotations

import difflib
import hashlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from eurus.coding.paths import MAX_FILE_BYTES, atomic_write_text, resolve_workspace_path
from eurus.errors import ToolError
from eurus.tools.base import Tool, ToolContext

MIN_HASH_PREFIX = 12
MAX_SEARCH_RESULTS = 100
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
}


def _resolve(context: ToolContext, relative: str) -> Path:
    return resolve_workspace_path(context.workspace, relative, label="path")


def _walk_files(directory: Path) -> Iterator[Path]:
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if entry.name in IGNORED_DIRS:
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_files(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
        except OSError:
            continue


def _read_text(path: Path) -> str:
    try:
        if not path.is_file():
            raise ToolError("file does not exist or is not a regular file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("file exceeds 1000000 bytes")
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("file is not valid UTF-8 text") from exc
    except OSError as exc:
        raise ToolError(f"cannot read file: {exc}") from exc


async def _list_files(arguments: dict[str, Any], context: ToolContext) -> str:
    relative = str(arguments.get("path", "."))
    target = _resolve(context, relative)
    if not target.is_dir():
        raise ToolError("path is not a directory")
    limit = int(arguments.get("limit", 100))
    if not 1 <= limit <= 500:
        raise ToolError("limit must be between 1 and 500")
    root = context.workspace.resolve()
    found: list[str] = []
    for item in _walk_files(target):
        found.append(item.relative_to(root).as_posix())
        if len(found) >= limit:
            break
    return "\n".join(found) if found else "(no files found)"


def _slice_lines(text: str, start: int | None, end: int | None) -> tuple[str, bool, int, int, int]:
    lines = text.splitlines()
    total = len(lines)
    first = 1 if start is None else start
    last = total if end is None else end
    if first < 1:
        raise ToolError("start_line must be at least 1")
    if end is not None and last < first:
        raise ToolError("end_line must not be smaller than start_line")
    if total == 0:
        return "", False, 1, 0, 0
    if first > total:
        raise ToolError(f"start_line {first} is beyond the end of the file ({total} lines)")
    clamped_last = min(last, total)
    selected = lines[first - 1 : clamped_last]
    body = "\n".join(selected)
    truncated = first != 1 or clamped_last != total
    return body, truncated, first, clamped_last, total


async def _read_file(arguments: dict[str, Any], context: ToolContext) -> str:
    target = _resolve(context, str(arguments["path"]))
    if target.is_symlink():
        raise ToolError("symbolic links are not readable through coding tools")
    text = _read_text(target)
    start = _int_arg(arguments.get("start_line"), "start_line")
    end = _int_arg(arguments.get("end_line"), "end_line")
    body, truncated, first, last, total = _slice_lines(text, start, end)
    if total == 0:
        return "[read_file] empty file (0 lines)"
    marker = (
        f"[read_file] showing lines {first}-{last} of {total} total; "
        "output truncated; pass start_line/end_line to read other ranges"
        if truncated
        else f"[read_file] lines 1-{total} of {total} total"
    )
    if not body:
        return marker
    return f"{marker}\n{body}"


def _int_arg(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be an integer") from exc


async def _search_code(arguments: dict[str, Any], context: ToolContext) -> str:
    query = str(arguments["query"])
    if not query or len(query) > 500:
        raise ToolError("query must be 1 to 500 characters")
    relative = str(arguments.get("path", "."))
    start = _resolve(context, relative)
    if not start.exists():
        raise ToolError("search path does not exist")
    limit = int(arguments.get("limit", 30))
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ToolError("limit must be between 1 and 100")
    files: Iterable[Path] = [start] if start.is_file() else _walk_files(start)
    root = context.workspace.resolve()
    matches: list[str] = []
    for file in files:
        if len(matches) >= limit:
            break
        try:
            relative_parts = file.relative_to(root).parts
            if any(part in IGNORED_DIRS for part in relative_parts):
                continue
            if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
                continue
            resolved = file.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if query.casefold() in line.casefold():
                matches.append(f"{file.relative_to(root).as_posix()}:{line_number}: {line[:500]}")
                if len(matches) >= limit:
                    break
    return "\n".join(matches) if matches else "(no matches)"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_base_sha256(current: str, base: Any) -> None:
    prefix = str(base).strip().lower()
    if len(prefix) < MIN_HASH_PREFIX or not _digest(current).startswith(prefix):
        raise ToolError(
            "base_sha256 does not match the current file; re-read the file and retry with "
            "its current hash"
        )


async def _propose_patch(arguments: dict[str, Any], context: ToolContext) -> str:
    relative = str(arguments["path"])
    target = _resolve(context, relative)
    if target.is_symlink():
        raise ToolError("symbolic links cannot be patched")
    old = _read_text(target)
    new = str(arguments["new_content"])
    if len(new.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError("proposed file exceeds 1000000 bytes")
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    output = "".join(diff)
    if not output:
        return "No changes proposed."
    return f"base_sha256: {_digest(old)}\n{output}"


async def _apply_patch(arguments: dict[str, Any], context: ToolContext) -> str:
    relative = str(arguments["path"])
    target = _resolve(context, relative)
    if target.is_symlink():
        raise ToolError("symbolic links cannot be patched")
    if not target.exists() or not target.is_file():
        raise ToolError("target file does not exist")
    replacement = str(arguments["new_content"])
    current = _read_text(target)
    base = arguments.get("base_sha256")
    expected = arguments.get("expected_content")
    if base is not None:
        _check_base_sha256(current, base)
    elif expected is None:
        raise ToolError("provide base_sha256 from propose_patch, or expected_content")
    elif current != str(expected):
        if str(expected) == replacement:
            raise ToolError(
                "expected_content must be the file's current content before your change, "
                "but it equals new_content. Run propose_patch and pass the base_sha256 it "
                "returns instead"
            )
        raise ToolError("file changed since it was read; re-read and propose a fresh patch")
    if len(replacement.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError("replacement exceeds 1000000 bytes")
    atomic_write_text(target, replacement)
    relative_path = target.relative_to(context.workspace.resolve()).as_posix()
    return f"updated {relative_path} ({len(current)} -> {len(replacement)} characters)"


async def _edit_file(arguments: dict[str, Any], context: ToolContext) -> str:
    target = _resolve(context, str(arguments["path"]))
    if target.is_symlink():
        raise ToolError("symbolic links cannot be edited")
    old_string = str(arguments["old_string"])
    new_string = str(arguments["new_string"])
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical; nothing to change")
    current = _read_text(target)
    base = arguments.get("base_sha256")
    if base is not None:
        _check_base_sha256(current, base)
    occurrences = current.count(old_string)
    if occurrences == 0:
        raise ToolError("old_string was not found in the file; re-read it and copy exactly")
    replace_all = bool(arguments.get("replace_all", False))
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"old_string matches {occurrences} locations; provide more surrounding context "
            "or set replace_all to true"
        )
    updated = (
        current.replace(old_string, new_string)
        if replace_all
        else current.replace(old_string, new_string, 1)
    )
    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError("edited file exceeds 1000000 bytes")
    atomic_write_text(target, updated)
    count = occurrences if replace_all else 1
    relative_path = target.relative_to(context.workspace.resolve()).as_posix()
    return (
        f"edited {relative_path}: replaced {count} occurrence(s) "
        f"({len(current)} -> {len(updated)} characters)"
    )


async def _create_file(arguments: dict[str, Any], context: ToolContext) -> str:
    target = _resolve(context, str(arguments["path"]))
    if target.is_symlink():
        raise ToolError("symbolic links cannot be created")
    content = str(arguments["content"])
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ToolError("content exceeds 1000000 bytes")
    if target.exists():
        if not bool(arguments.get("overwrite", False)):
            raise ToolError(
                "file already exists; read it and use edit_file or apply_patch, or pass "
                "overwrite=true to replace it"
            )
        if not target.is_file():
            raise ToolError("target exists and is not a regular file")
    atomic_write_text(target, content)
    relative_path = target.relative_to(context.workspace.resolve()).as_posix()
    return f"created {relative_path} ({len(content)} characters)"


def coding_tools() -> list[Tool]:
    from eurus.coding.document_tools import document_tools
    from eurus.coding.git_tools import git_tools
    from eurus.coding.office_tools import office_tools

    string = {"type": "string"}
    return [
        *git_tools(),
        *document_tools(),
        *office_tools(),
        Tool(
            "list_files",
            "List files inside the configured workspace, skipping common generated directories.",
            {
                "type": "object",
                "properties": {"path": string, "limit": {"type": "integer"}},
                "additionalProperties": False,
            },
            _list_files,
        ),
        Tool(
            "read_file",
            "Read a UTF-8 text file inside the workspace. Optional start_line and end_line "
            "(1-based, inclusive) read large files in slices; the reply reports the total line "
            "count and marks truncated output. Files over 1 MB and symbolic links are rejected.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            _read_file,
        ),
        Tool(
            "search_code",
            "Search text in workspace files and return file/line matches.",
            {
                "type": "object",
                "properties": {
                    "query": string,
                    "path": string,
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            _search_code,
        ),
        Tool(
            "propose_patch",
            "Show a unified diff for proposed full-file content without modifying the file.",
            {
                "type": "object",
                "properties": {"path": string, "new_content": string},
                "required": ["path", "new_content"],
                "additionalProperties": False,
            },
            _propose_patch,
        ),
        Tool(
            "edit_file",
            "Replace an exact old_string with new_string inside an existing file. Fails when "
            "old_string is missing or matches more than once unless replace_all is true. "
            "Optional base_sha256 protects against stale writes. Requires owner approval.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "old_string": string,
                    "new_string": string,
                    "replace_all": {"type": "boolean"},
                    "base_sha256": string,
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            _edit_file,
            mutates=True,
            requires_approval=True,
            path_keys=("path",),
        ),
        Tool(
            "create_file",
            "Create a new file with content, creating parent directories inside the workspace. "
            "Refuses to overwrite an existing file unless overwrite is true. Requires owner "
            "approval.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "content": string,
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            _create_file,
            mutates=True,
            requires_approval=True,
            path_keys=("path",),
        ),
        Tool(
            "apply_patch",
            "Replace a file with new_content. Pass base_sha256 from propose_patch (preferred), "
            "or expected_content, which must be the file's current content before the change. "
            "Requires owner approval.",
            {
                "type": "object",
                "properties": {
                    "path": string,
                    "new_content": string,
                    "base_sha256": string,
                    "expected_content": string,
                },
                "required": ["path", "new_content"],
                "additionalProperties": False,
            },
            _apply_patch,
            mutates=True,
            requires_approval=True,
            path_keys=("path",),
        ),
    ]


def describe_diff(old: str, new: str) -> str:
    """Utility kept public for tests and integrations that want a unified diff."""
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True)))
