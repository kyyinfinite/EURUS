from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eurus.office.errors import OfficeValidationError

MAX_BLOCKS = 400
MAX_TABLE_ROWS = 500
MAX_TABLE_COLUMNS = 12
MAX_HEADING_LEVEL = 4
MAX_TEXT_CHARS = 20_000

DOCUMENT_STYLES = (
    "academic",
    "editorial",
    "business",
    "technical",
    "research",
    "educational",
    "proposal",
)

BLOCK_KINDS = (
    "heading",
    "paragraph",
    "list",
    "table",
    "image",
    "quote",
    "callout",
    "timeline",
    "two_column",
    "page_break",
    "bibliography",
)


@dataclass(frozen=True, slots=True)
class TextRun:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass(frozen=True, slots=True)
class RichText:
    runs: tuple[TextRun, ...] = ()

    @property
    def plain(self) -> str:
        return "".join(run.text for run in self.runs)

    @classmethod
    def from_text(cls, text: str) -> RichText:
        return cls(runs=(TextRun(text=text),))

    @classmethod
    def from_inline(cls, text: str) -> RichText:
        return cls(runs=tuple(parse_inline(text)))


@dataclass(frozen=True, slots=True)
class HeadingBlock:
    level: int
    text: str

    def __post_init__(self) -> None:
        if not 1 <= self.level <= MAX_HEADING_LEVEL:
            raise OfficeValidationError(f"heading level must be 1-{MAX_HEADING_LEVEL}")


@dataclass(frozen=True, slots=True)
class ParagraphBlock:
    rich: RichText


@dataclass(frozen=True, slots=True)
class ListItemBlock:
    text: str
    ordered: bool = False


@dataclass(frozen=True, slots=True)
class TableBlock:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str = ""

    def __post_init__(self) -> None:
        if not self.headers:
            raise OfficeValidationError("table headers must not be empty")
        if len(self.headers) > MAX_TABLE_COLUMNS:
            raise OfficeValidationError(f"tables are limited to {MAX_TABLE_COLUMNS} columns")
        if len(self.rows) > MAX_TABLE_ROWS:
            raise OfficeValidationError(f"tables are limited to {MAX_TABLE_ROWS} rows")
        for row in self.rows:
            if len(row) != len(self.headers):
                raise OfficeValidationError("table row width must match header count")


@dataclass(frozen=True, slots=True)
class ImageBlock:
    path: str
    caption: str = ""
    width_cm: float = 12.0

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise OfficeValidationError("image path must not be empty")
        if not 2.0 <= self.width_cm <= 17.0:
            raise OfficeValidationError("image width must be between 2 and 17 cm")


@dataclass(frozen=True, slots=True)
class QuoteBlock:
    rich: RichText
    attribution: str = ""


@dataclass(frozen=True, slots=True)
class CalloutBlock:
    title: str
    body: str
    variant: str = "info"

    def __post_init__(self) -> None:
        if self.variant not in {"info", "warning", "success", "risk"}:
            raise OfficeValidationError("callout variant must be info, warning, success, or risk")


@dataclass(frozen=True, slots=True)
class TimelineItem:
    label: str
    title: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class TimelineBlock:
    items: tuple[TimelineItem, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise OfficeValidationError("timeline must contain at least one item")
        if len(self.items) > 24:
            raise OfficeValidationError("timeline is limited to 24 items")


@dataclass(frozen=True, slots=True)
class TwoColumnBlock:
    left: tuple[DocumentBlock, ...]
    right: tuple[DocumentBlock, ...]


@dataclass(frozen=True, slots=True)
class PageBreakBlock:
    pass


@dataclass(frozen=True, slots=True)
class BibliographyBlock:
    entries: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.entries:
            raise OfficeValidationError("bibliography must contain at least one entry")


DocumentBlock = (
    HeadingBlock
    | ParagraphBlock
    | ListItemBlock
    | TableBlock
    | ImageBlock
    | QuoteBlock
    | CalloutBlock
    | TimelineBlock
    | TwoColumnBlock
    | PageBreakBlock
    | BibliographyBlock
)


@dataclass(frozen=True, slots=True)
class HeaderFooterConfig:
    header_text: str = ""
    footer_text: str = ""
    page_numbers: bool = True


@dataclass(frozen=True, slots=True)
class PageSetup:
    margin_cm: tuple[float, float, float, float] = (2.2, 2.0, 2.2, 2.0)


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    title: str
    subtitle: str = ""
    author: str = "Eurus"
    subject: str = ""
    keywords: tuple[str, ...] = ()
    date_text: str = ""
    language: str = "en"


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    metadata: DocumentMetadata
    style: str = "business"
    blocks: tuple[DocumentBlock, ...] = ()
    header_footer: HeaderFooterConfig = field(default_factory=HeaderFooterConfig)
    page: PageSetup = field(default_factory=PageSetup)
    include_toc: bool = False

    def __post_init__(self) -> None:
        if not self.metadata.title.strip():
            raise OfficeValidationError("document title must not be empty")
        if self.style not in DOCUMENT_STYLES:
            raise OfficeValidationError(
                f"document style must be one of: {', '.join(DOCUMENT_STYLES)}"
            )
        if len(self.blocks) > MAX_BLOCKS:
            raise OfficeValidationError(f"documents are limited to {MAX_BLOCKS} blocks")
        total = sum(_block_chars(block) for block in self.blocks)
        if total > MAX_TEXT_CHARS:
            raise OfficeValidationError("document content exceeds the text limit")

    def has_images(self) -> bool:
        return any(isinstance(block, ImageBlock) for block in self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "title": self.metadata.title,
                "subtitle": self.metadata.subtitle,
                "author": self.metadata.author,
                "subject": self.metadata.subject,
                "keywords": list(self.metadata.keywords),
                "date_text": self.metadata.date_text,
                "language": self.metadata.language,
            },
            "style": self.style,
            "include_toc": self.include_toc,
            "header_footer": {
                "header_text": self.header_footer.header_text,
                "footer_text": self.header_footer.footer_text,
                "page_numbers": self.header_footer.page_numbers,
            },
            "blocks": [block_to_dict(block) for block in self.blocks],
        }


def _block_chars(block: DocumentBlock) -> int:
    if isinstance(block, TwoColumnBlock):
        return sum(_block_chars(item) for item in (*block.left, *block.right))
    if isinstance(block, PageBreakBlock):
        return 0
    if isinstance(block, HeadingBlock):
        return len(block.text)
    if isinstance(block, ParagraphBlock):
        return len(block.rich.plain)
    if isinstance(block, (ListItemBlock, CalloutBlock)):
        return len(getattr(block, "text", "") or "") + len(getattr(block, "body", "") or "")
    if isinstance(block, TableBlock):
        return sum(len(cell) for row in block.rows for cell in row) + sum(
            len(header) for header in block.headers
        )
    if isinstance(block, QuoteBlock):
        return len(block.rich.plain)
    if isinstance(block, TimelineBlock):
        return sum(len(item.title) + len(item.description) for item in block.items)
    if isinstance(block, BibliographyBlock):
        return sum(len(entry) for entry in block.entries)
    return 0


def block_to_dict(block: DocumentBlock) -> dict[str, Any]:
    if isinstance(block, HeadingBlock):
        return {"kind": "heading", "level": block.level, "text": block.text}
    if isinstance(block, ParagraphBlock):
        return {"kind": "paragraph", "text": block.rich.plain}
    if isinstance(block, ListItemBlock):
        return {"kind": "list", "ordered": block.ordered, "items": [block.text]}
    if isinstance(block, TableBlock):
        return {
            "kind": "table",
            "headers": list(block.headers),
            "rows": [list(row) for row in block.rows],
            "caption": block.caption,
        }
    if isinstance(block, ImageBlock):
        return {
            "kind": "image",
            "path": block.path,
            "caption": block.caption,
            "width_cm": block.width_cm,
        }
    if isinstance(block, QuoteBlock):
        return {"kind": "quote", "text": block.rich.plain, "attribution": block.attribution}
    if isinstance(block, CalloutBlock):
        return {
            "kind": "callout",
            "title": block.title,
            "body": block.body,
            "variant": block.variant,
        }
    if isinstance(block, TimelineBlock):
        return {
            "kind": "timeline",
            "items": [
                {"label": item.label, "title": item.title, "description": item.description}
                for item in block.items
            ],
        }
    if isinstance(block, TwoColumnBlock):
        return {
            "kind": "two_column",
            "left": [block_to_dict(item) for item in block.left],
            "right": [block_to_dict(item) for item in block.right],
        }
    if isinstance(block, PageBreakBlock):
        return {"kind": "page_break"}
    if isinstance(block, BibliographyBlock):
        return {"kind": "bibliography", "entries": list(block.entries)}
    raise OfficeValidationError("unsupported block type")


_INLINE_SPLIT = "**"


def parse_inline(text: str) -> list[TextRun]:
    runs: list[TextRun] = []
    bold_parts = text.split(_INLINE_SPLIT)
    for index, bold_part in enumerate(bold_parts):
        bold = index % 2 == 1
        for piece_index, piece in enumerate(bold_part.split("*")):
            italic = piece_index % 2 == 1
            for code_index, code_piece in enumerate(piece.split("`")):
                if not code_piece:
                    continue
                runs.append(
                    TextRun(
                        text=code_piece,
                        bold=bold,
                        italic=italic,
                        code=code_index % 2 == 1,
                    )
                )
    return runs or [TextRun(text=text)]
