from __future__ import annotations

import pytest

from eurus.office.errors import OfficeValidationError
from eurus.office.markdown import build_document_spec, parse_markdown_text
from eurus.office.models.document import (
    HeadingBlock,
    ListItemBlock,
    PageBreakBlock,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
)

MARKDOWN_SAMPLE = """# Laporan Kinerja

Ringkasan **eksekutif** dengan *penekanan* dan `kode`.

## Poin Utama

- Pertama
- Kedua

1. Satu
2. Dua

| Indikator | Nilai | Target |
| --- | --- | --- |
| Pendapatan | 1.2M | 1.0M |
| NPS | 62 | 60 |

> Kutipan penting

### Lampiran
"""


def test_markdown_parses_all_block_types() -> None:
    parsed = parse_markdown_text(MARKDOWN_SAMPLE)
    kinds = [type(block) for block in parsed.blocks]
    assert HeadingBlock in kinds
    assert ParagraphBlock in kinds
    assert ListItemBlock in kinds
    assert TableBlock in kinds
    assert QuoteBlock in kinds
    assert parsed.title == "Laporan Kinerja"


def test_markdown_table_becomes_real_table_block() -> None:
    parsed = parse_markdown_text(MARKDOWN_SAMPLE)
    tables = [block for block in parsed.blocks if isinstance(block, TableBlock)]
    assert len(tables) == 1
    assert tables[0].headers == ("Indikator", "Nilai", "Target")
    assert tables[0].rows[0] == ("Pendapatan", "1.2M", "1.0M")
    assert tables[0].rows[1] == ("NPS", "62", "60")


def test_markdown_inline_formatting_becomes_runs() -> None:
    parsed = parse_markdown_text(MARKDOWN_SAMPLE)
    paragraphs = [block for block in parsed.blocks if isinstance(block, ParagraphBlock)]
    runs = paragraphs[0].rich.runs
    bold_runs = [run for run in runs if run.bold]
    italic_runs = [run for run in runs if run.italic]
    code_runs = [run for run in runs if run.code]
    assert any(run.text == "eksekutif" for run in bold_runs)
    assert any(run.text == "penekanan" for run in italic_runs)
    assert "kode" in run_texts(code_runs)


def run_texts(runs) -> list[str]:
    return [run.text for run in runs]


def test_ordered_and_unordered_lists() -> None:
    parsed = parse_markdown_text(MARKDOWN_SAMPLE)
    items = [block for block in parsed.blocks if isinstance(block, ListItemBlock)]
    ordered = [item for item in items if item.ordered]
    unordered = [item for item in items if not item.ordered]
    assert [item.text for item in unordered] == ["Pertama", "Kedua"]
    assert [item.text for item in ordered] == ["Satu", "Dua"]


def test_separator_becomes_page_break() -> None:
    parsed = parse_markdown_text("First\n\n---\n\nSecond")
    assert PageBreakBlock in [type(block) for block in parsed.blocks]


def test_build_document_spec_rejects_empty() -> None:
    with pytest.raises(OfficeValidationError):
        build_document_spec("   ")
    title, blocks = build_document_spec("Just one paragraph")
    assert title == "Just one paragraph"
    assert any(isinstance(block, HeadingBlock) for block in blocks)


def test_markdown_table_never_leaks_into_paragraphs() -> None:
    parsed = parse_markdown_text(MARKDOWN_SAMPLE)
    paragraphs = [block.rich.plain for block in parsed.blocks if isinstance(block, ParagraphBlock)]
    for text in paragraphs:
        assert "|" not in text
        assert "---" not in text
        assert "**" not in text
        assert not text.startswith("#")
