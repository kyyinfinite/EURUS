from __future__ import annotations

from prompt_toolkit.formatted_text import StyleAndTextTuples

from eurus.tui.workspace.diffmodel import DiffHunk, DiffLine, DiffModel
from eurus.tui.workspace.diffsession import VIEW_SIDE_BY_SIDE, DiffSession


def render_diff(session: DiffSession, width: int, height: int) -> StyleAndTextTuples:
    model = session.model
    out: StyleAndTextTuples = []
    if model.identical:
        out.append(("class:diff-header", f" {session.relative}"))
        out.append(("", "\n"))
        out.append(("class:dim", " no changes"))
        return out
    header = f" {session.relative}  {model.stats.summary()}"
    out.append(("class:diff-header", header[:width]))
    out.append(("", "\n"))
    out.append(("class:dim", "─" * width))
    out.append(("", "\n"))
    if session.view == VIEW_SIDE_BY_SIDE:
        out.extend(_render_side_by_side(session, width, height - 3))
    else:
        out.extend(_render_unified(session, width, height - 3))
    out.append(("", "\n"))
    out.extend(_render_review_bar(session, width))
    return out


def _hunk_header(hunk: DiffHunk, current: bool, decision: str) -> str:
    marks = {
        "pending": "○",
        "accepted": "✓",
        "rejected": "✗",
    }
    mark = marks.get(decision, "○")
    star = "▸" if current else " "
    return (
        f"{star}@@ hunk {hunk.index + 1} "
        f"-{hunk.old_start},{len(hunk.old_lines)} "
        f"+{hunk.new_start},{len(hunk.new_lines)} {mark} "
    )


def _render_unified(session: DiffSession, width: int, height: int) -> StyleAndTextTuples:
    model: DiffModel = session.model
    out: StyleAndTextTuples = []
    number_width = _number_width(model)
    body_width = max(width - number_width * 2 - 6, 1)
    rows: list[tuple[str, StyleAndTextTuples]] = []
    for hunk in model.hunks:
        current = hunk.index == session.current_hunk
        header_style = "class:diff-hunk-current" if current else "class:diff-hunk"
        rows.append(
            (
                "header",
                [(header_style, _hunk_header(hunk, current, hunk.decision)[:width])],
            )
        )
        for line in hunk.unified():
            rows.append(("line", _unified_line(line, number_width, body_width, session)))
    visible = rows[session.scroll_offset : session.scroll_offset + max(height, 1)]
    for _, fragments in visible:
        out.extend(fragments)
        out.append(("", "\n"))
    if len(rows) > max(height, 1):
        hidden = len(rows) - session.scroll_offset - max(height, 1)
        out.append(("class:dim", f" … {max(hidden, 0)} more lines"))
        out.append(("", "\n"))
    return out


def _unified_line(
    line: DiffLine, number_width: int, body_width: int, session: DiffSession
) -> StyleAndTextTuples:
    old_number = f"{line.old_number:>{number_width}}" if line.old_number else " " * number_width
    new_number = f"{line.new_number:>{number_width}}" if line.new_number else " " * number_width
    text = line.text[session.column_offset : session.column_offset + body_width]
    if line.kind == "added":
        return [
            ("class:diff-line-number", old_number),
            ("class:dim", " "),
            ("class:diff-added-number", new_number),
            ("class:diff-added", f" +{text}"),
        ]
    if line.kind == "removed":
        return [
            ("class:diff-removed-number", old_number),
            ("class:diff-removed", f" -{text}"),
            ("class:diff-line-number", " " * (number_width + 1)),
        ]
    return [
        ("class:diff-line-number", old_number),
        ("class:dim", " "),
        ("class:diff-line-number", new_number),
        ("class:dim", f"  {text}"),
    ]


def _render_side_by_side(session: DiffSession, width: int, height: int) -> StyleAndTextTuples:
    model: DiffModel = session.model
    out: StyleAndTextTuples = []
    number_width = _number_width(model)
    half = max((width - number_width * 2 - 7) // 2, 4)
    rows: list[tuple[str, StyleAndTextTuples]] = []
    for hunk in model.hunks:
        current = hunk.index == session.current_hunk
        header_style = "class:diff-hunk-current" if current else "class:diff-hunk"
        rows.append(
            (
                "header",
                [(header_style, _hunk_header(hunk, current, hunk.decision)[:width])],
            )
        )
        for _kind, left, right in hunk.side_by_side():
            rows.append(("line", _side_row(left, right, number_width, half, session)))
    visible = rows[session.scroll_offset : session.scroll_offset + max(height, 1)]
    for _, fragments in visible:
        out.extend(fragments)
        out.append(("", "\n"))
    if len(rows) > max(height, 1):
        hidden = len(rows) - session.scroll_offset - max(height, 1)
        out.append(("class:dim", f" … {max(hidden, 0)} more lines"))
        out.append(("", "\n"))
    return out


def _side_row(
    left: DiffLine, right: DiffLine, number_width: int, half: int, session: DiffSession
) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    for line, added in ((left, False), (right, True)):
        number = (
            f"{line.old_number:>{number_width}}"
            if not added and line.old_number
            else (
                f"{line.new_number:>{number_width}}"
                if added and line.new_number
                else " " * number_width
            )
        )
        text = line.text[session.column_offset : session.column_offset + half]
        if line.kind == "added":
            out.append(("class:diff-added-number", number))
            out.append(("class:diff-added", f" +{text}".ljust(half + 2)))
        elif line.kind == "removed":
            out.append(("class:diff-removed-number", number))
            out.append(("class:diff-removed", f" -{text}".ljust(half + 2)))
        elif line.kind == "blank":
            out.append(("class:dim", " " * (number_width + half + 2)))
        else:
            out.append(("class:diff-line-number", number))
            out.append(("class:dim", f"  {text}".ljust(half + 2)))
        out.append(("class:dim", " "))
    return out


def _number_width(model: DiffModel) -> int:
    largest = 0
    for hunk in model.hunks:
        largest = max(
            largest,
            hunk.old_start + len(hunk.old_lines),
            hunk.new_start + len(hunk.new_lines),
        )
    return max(len(str(largest)), 3)


def _render_review_bar(session: DiffSession, width: int) -> StyleAndTextTuples:
    out: StyleAndTextTuples = [
        ("class:dim", "─" * width),
        ("", "\n"),
        ("class:footer-mode", " REVIEW"),
        ("class:dim", f" {session.review_summary()}"),
    ]
    if session.message:
        out.append(("class:dim", f" · {session.message}"))
    return out
