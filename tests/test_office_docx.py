from __future__ import annotations

from pathlib import Path

import pytest

from eurus.office.errors import OfficeValidationError
from eurus.office.markdown import build_document_spec
from eurus.office.models.document import (
    BibliographyBlock,
    CalloutBlock,
    DocumentMetadata,
    DocumentSpec,
    HeaderFooterConfig,
    HeadingBlock,
    ListItemBlock,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    TableBlock,
    TimelineBlock,
    TimelineItem,
)
from eurus.office.qa.structural import validate_structure
from eurus.office.renderers.word import render_document

docx = pytest.importorskip("docx")


def _data_tables(document):
    return [table for table in document.tables if len(table.columns) > 1]


def _table_with_headers(document, headers):
    for table in document.tables:
        cells = tuple(cell.text.strip() for cell in table.rows[0].cells)
        if cells == tuple(headers):
            return table
    return None


def build_spec(tmp_path: Path, **overrides) -> DocumentSpec:
    payload = dict(
        metadata=DocumentMetadata(
            title="Laporan Kerajaan Mataram Kuno",
            subtitle="Ringkasan sejarah",
            author="Eurus",
            date_text="2026",
            subject="Sejarah",
            keywords=("sejarah", "jawa"),
        ),
        style="editorial",
        include_toc=True,
        header_footer=HeaderFooterConfig(
            header_text="Mataram Kuno", footer_text="Eurus Office", page_numbers=True
        ),
        blocks=(
            HeadingBlock(1, "Pendahuluan"),
            ParagraphBlock(
                RichText.from_inline("Mataram Kuno adalah **kerajaan** besar di Jawa Tengah.")
            ),
            HeadingBlock(2, "Dinasti Sanjaya"),
            TableBlock(
                headers=("Raja", "Masa", "Ibu Kota"),
                rows=(
                    ("Sanjaya", "732-760", "Mataram"),
                    ("Rakai Panangkaran", "760-780", "Mataram"),
                    ("Pikatan", "830s", "Mataram"),
                ),
                caption="Tabel 1. Raja-raja Sanjaya",
            ),
            HeadingBlock(2, "Kronologi"),
            TimelineBlock(
                (
                    TimelineItem("732", "Sanjaya berkuasa", "Prasasti Canggal"),
                    TimelineItem("856", "Pembagian kerajaan", ""),
                    TimelineItem("929", "Pindah ke Jawa Timur", "Mpu Sindok"),
                )
            ),
            CalloutBlock("Prasasti Canggal", "Bukti tertulis tertua.", "info"),
            QuoteBlock(RichText.from_text("Sebuah kutipan sejarah."), "Kitab Kuno"),
            ListItemBlock("Item penting", False),
            ListItemBlock("Langkah pertama", True),
            BibliographyBlock(("Van Naerssen. 1947. Canggal.",)),
        ),
    )
    payload.update(overrides)
    return DocumentSpec(**payload)


def render_to(tmp_path: Path, spec: DocumentSpec) -> Path:
    return render_document(spec, tmp_path / "report.docx")


def test_docx_contains_native_tables_with_header_row(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    dynasty_table = _table_with_headers(document, ("Raja", "Masa", "Ibu Kota"))
    assert dynasty_table is not None
    assert dynasty_table.rows[1].cells[0].text == "Sanjaya"
    header_repeat = dynasty_table.rows[0]._tr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}trPr/"
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblHeader"
    )
    assert header_repeat is not None


def test_docx_headings_hierarchy_and_styles(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    styles = [p.style.name for p in document.paragraphs if p.style.name.startswith("Heading")]
    assert "Heading 1" in styles
    assert "Heading 2" in styles


def test_docx_metadata_and_core_properties(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    assert document.core_properties.title == "Laporan Kerajaan Mataram Kuno"
    assert document.core_properties.author == "Eurus"
    assert "sejarah" in document.core_properties.keywords


def test_docx_header_footer_and_page_number_field(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    footer_text = document.sections[0].footer.paragraphs[0].text
    assert "Eurus Office" in footer_text
    xml = document.sections[0].footer.paragraphs[0]._p.xml
    assert "PAGE" in xml
    header_text = document.sections[0].header.paragraphs[0].text
    assert "Mataram Kuno" in header_text


def test_docx_toc_field_present(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    body_xml = "".join(paragraph._p.xml for paragraph in document.paragraphs)
    assert "TOC" in body_xml


def test_docx_images_and_captions(tmp_path: Path) -> None:
    from PIL import Image

    image_path = tmp_path / "candi.png"
    Image.new("RGB", (300, 200), (150, 90, 40)).save(image_path)
    spec = build_spec(tmp_path)
    from eurus.office.models.document import ImageBlock

    blocks = tuple(list(spec.blocks)[:-1]) + (
        ImageBlock(path=str(image_path), caption="Candi Prambanan", width_cm=10.0),
        BibliographyBlock(("Sumber 2026.",)),
    )
    spec = DocumentSpec(
        metadata=spec.metadata,
        style=spec.style,
        blocks=tuple(blocks),
        header_footer=spec.header_footer,
        include_toc=False,
    )
    path = render_to(tmp_path, spec)
    document = docx.Document(str(path))
    inline_shapes = document.inline_shapes
    assert len(inline_shapes) == 1
    captions = [p.text for p in document.paragraphs if p.style.name == "Caption"]
    assert "Candi Prambanan" in captions


def test_docx_markdown_regression_no_leakage(tmp_path: Path) -> None:
    markdown = """# Judul

Paragraf dengan **tebal** dan *miring*.

| Kolom A | Kolom B |
| --- | --- |
| 1 | 2 |

- butir satu
- butir dua
"""
    title, blocks = build_document_spec(markdown)
    spec = DocumentSpec(
        metadata=DocumentMetadata(title=title),
        blocks=blocks,
    )
    path = render_to(tmp_path, spec)
    document = docx.Document(str(path))
    for paragraph in document.paragraphs:
        text = paragraph.text
        assert "**" not in text
        assert not text.lstrip().startswith("#")
        assert not text.startswith("- ")
        assert "|" not in text
    data_tables = _data_tables(document)
    assert len(data_tables) == 1
    assert data_tables[0].rows[0].cells[0].text == "Kolom A"


def test_docx_structural_qa_passes_clean_document(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    defects, score = validate_structure("docx", path, "job-test")
    critical = [d for d in defects if d.severity.value == "critical"]
    assert not critical
    assert score >= 95.0


def test_docx_structural_qa_flags_markdown_leakage(tmp_path: Path) -> None:
    path = render_to(tmp_path, build_spec(tmp_path))
    document = docx.Document(str(path))
    document.paragraphs[-1].add_run(" leaked **markdown** text")
    document.save(str(path))
    defects, score = validate_structure("docx", path, "job-test")
    descriptions = " ".join(d.description for d in defects)
    assert "markdown leakage" in descriptions
    assert any(
        d.severity
        is __import__(
            "eurus.office.models.task", fromlist=["DefectSeverity"]
        ).DefectSeverity.CRITICAL
        for d in defects
    )
    assert score < 95.0


def test_docx_missing_image_is_reported_not_crash(tmp_path: Path) -> None:
    from eurus.office.models.document import ImageBlock

    spec = build_spec(tmp_path)
    blocks = tuple(list(spec.blocks)) + (
        ImageBlock(path=str(tmp_path / "missing.png"), caption="Hilang", width_cm=8.0),
    )
    spec = DocumentSpec(metadata=spec.metadata, style=spec.style, blocks=tuple(blocks))
    path = render_to(tmp_path, spec)
    assert path.is_file()
    with pytest.raises(OfficeValidationError):
        ImageBlock(path="   ")
