from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from eurus.office.errors import OfficeValidationError
from eurus.office.models.document import (
    BibliographyBlock,
    CalloutBlock,
    DocumentBlock,
    HeadingBlock,
    ImageBlock,
    ListItemBlock,
    PageBreakBlock,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    TableBlock,
    TimelineBlock,
    TimelineItem,
    TwoColumnBlock,
)

HEADING_PATTERN = re.compile(r"^(#{1,4})\s+(.*)$")
BULLET_PATTERN = re.compile(r"^[-*+]\s+(.*)$")
ORDERED_PATTERN = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|\s*$")
TABLE_DIVIDER_PATTERN = re.compile(r"^\|?\s*:?-{3,}.*\|.*$")
IMAGE_PATTERN = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
QUOTE_PATTERN = re.compile(r"^>\s?(.*)$")
CALLOUT_PATTERN = re.compile(r"^\[!(info|warning|success|risk)\]\s*(.*)$", re.IGNORECASE)
SEPARATOR_PATTERN = re.compile(r"^(\*\*\*|---|___)\s*$")
TIMELINE_ITEM_PATTERN = re.compile(r"^-\s*\[(?P<label>[^\]]+)\]\s*(?P<title>.*)$")
EMPHASIS_STRIP_PATTERN = re.compile(r"(\*\*|__|\*|_|`)")


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    title: str
    blocks: tuple[DocumentBlock, ...]


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _is_table_line(line: str) -> bool:
    return bool(TABLE_ROW_PATTERN.match(line.strip()))


def parse_markdown_text(text: str) -> MarkdownDocument:
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[DocumentBlock] = []
    title = ""
    paragraph: list[str] = []
    list_items: list[tuple[str, bool]] = []
    table_rows: list[list[str]] = []
    table_active = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(part.strip() for part in paragraph).strip()
            if joined:
                blocks.append(ParagraphBlock(rich=RichText.from_inline(joined)))
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            for item_text, ordered in list_items:
                blocks.append(ListItemBlock(text=item_text.strip(), ordered=ordered))
            list_items = []

    def flush_table() -> None:
        nonlocal table_rows, table_active
        if table_rows:
            headers = table_rows[0]
            rows = tuple(tuple(cell) for cell in table_rows[1:] if cell)
            if any(header.strip() for header in headers):
                blocks.append(TableBlock(headers=tuple(headers), rows=rows))
            table_rows = []
        table_active = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if _is_table_line(stripped):
            flush_paragraph()
            flush_list()
            if TABLE_DIVIDER_PATTERN.match(stripped) and table_rows and len(table_rows) == 1:
                continue
            table_rows.append(_split_row(stripped))
            table_active = True
            continue
        if table_active:
            flush_table()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if SEPARATOR_PATTERN.match(stripped):
            flush_paragraph()
            flush_list()
            blocks.append(PageBreakBlock())
            continue
        heading = HEADING_PATTERN.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if level == 1 and not title and not blocks:
                title = heading_text
                continue
            blocks.append(HeadingBlock(level=level, text=heading_text))
            continue
        image = IMAGE_PATTERN.match(stripped)
        if image:
            flush_paragraph()
            flush_list()
            blocks.append(ImageBlock(path=image.group(2).strip(), caption=image.group(1).strip()))
            continue
        quote = QUOTE_PATTERN.match(stripped)
        if quote:
            flush_paragraph()
            flush_list()
            blocks.append(QuoteBlock(rich=RichText.from_inline(quote.group(1).strip())))
            continue
        callout = CALLOUT_PATTERN.match(stripped)
        if callout:
            flush_paragraph()
            flush_list()
            blocks.append(
                CalloutBlock(
                    title=callout.group(2).strip() or callout.group(1).lower(),
                    body="",
                    variant=callout.group(1).lower(),
                )
            )
            continue
        bullet = BULLET_PATTERN.match(stripped)
        if bullet:
            flush_paragraph()
            list_items.append((bullet.group(1), False))
            continue
        ordered = ORDERED_PATTERN.match(stripped)
        if ordered:
            flush_paragraph()
            list_items.append((ordered.group(2), True))
            continue
        flush_list()
        paragraph.append(stripped)
    flush_paragraph()
    flush_list()
    flush_table()
    if not title:
        title = "Untitled Document"
    return MarkdownDocument(title=title, blocks=tuple(blocks))


def coerce_timeline_blocks(blocks: tuple[DocumentBlock, ...]) -> tuple[DocumentBlock, ...]:
    if not blocks:
        return blocks
    merged: list[DocumentBlock] = []
    pending: list[TimelineItem] = []
    for block in blocks:
        if isinstance(block, ListItemBlock) and not block.ordered:
            match = TIMELINE_ITEM_PATTERN.match(f"- {block.text}")
            if match:
                pending.append(
                    TimelineItem(
                        label=match.group("label").strip(), title=match.group("title").strip()
                    )
                )
                continue
        if pending and not isinstance(block, ListItemBlock):
            merged.append(TimelineBlock(items=tuple(pending)))
            pending = []
        merged.append(block)
    if pending:
        merged.append(TimelineBlock(items=tuple(pending)))
    return tuple(merged)


def build_document_spec(
    text: str,
    *,
    style: str = "business",
    include_toc: bool = False,
    bibliography_entries: tuple[str, ...] = (),
) -> tuple[str, tuple[DocumentBlock, ...]]:
    if not text or not text.strip():
        raise OfficeValidationError("markdown content must not be empty")
    if len(text) > 2_000_000:
        raise OfficeValidationError("markdown content exceeds the size limit")
    parsed = parse_markdown_text(text)
    blocks = list(coerce_timeline_blocks(parsed.blocks))
    if bibliography_entries:
        blocks.append(BibliographyBlock(entries=tuple(bibliography_entries)))
    if not any(isinstance(block, HeadingBlock) for block in blocks):
        first_paragraph = next(
            (block.rich.plain for block in blocks if isinstance(block, ParagraphBlock)), ""
        )
        derived = (parsed.title if parsed.title != "Untitled Document" else "") or first_paragraph[
            :120
        ]
        blocks.insert(0, HeadingBlock(level=1, text=derived or "Untitled Document"))
        return derived or "Untitled Document", tuple(blocks)
    return parsed.title, tuple(blocks)


def group_two_columns(
    blocks: tuple[DocumentBlock, ...],
    *,
    left_marker: str = "[col-left]",
    right_marker: str = "[col-right]",
) -> tuple[DocumentBlock, ...]:
    left_index = _find_marker(blocks, left_marker)
    right_index = _find_marker(blocks, right_marker)
    if left_index < 0 or right_index < 0 or right_index <= left_index:
        return blocks
    left = _collect_until(blocks, left_index + 1, right_index)
    right = _collect_until(blocks, right_index + 1, len(blocks))
    head = blocks[:left_index]
    remaining = blocks[right_index + 1 :]
    tail = tuple(remaining[len(right) :])
    if not left and not right:
        return blocks
    return (*head, TwoColumnBlock(left=tuple(left), right=tuple(right)), *tail)


def _find_marker(blocks: tuple[DocumentBlock, ...], marker: str) -> int:
    for index, block in enumerate(blocks):
        if isinstance(block, ParagraphBlock) and block.rich.plain.strip() == marker:
            return index
    return -1


def _collect_until(blocks: tuple[DocumentBlock, ...], start: int, end: int) -> list[DocumentBlock]:
    collected: list[DocumentBlock] = []
    for block in blocks[start:end]:
        if isinstance(block, ParagraphBlock) and block.rich.plain.strip() in {
            "[col-left]",
            "[col-right]",
        }:
            continue
        collected.append(block)
    return collected


def resolve_image_path(path_text: str, roots: tuple[Path, ...]) -> str:
    candidate = Path(path_text.strip())
    if not candidate.is_absolute():
        for root in roots:
            resolved = (root / candidate).resolve()
            if resolved.is_file():
                return str(resolved)
    resolved = candidate.resolve()
    if resolved.is_file():
        return str(resolved)
    return ""
