from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from eurus.office.design.themes import DocumentTheme, theme_for_style
from eurus.office.errors import OfficeRenderError
from eurus.office.models.document import (
    BibliographyBlock,
    CalloutBlock,
    DocumentSpec,
    HeadingBlock,
    ImageBlock,
    ListItemBlock,
    PageBreakBlock,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    TableBlock,
    TimelineBlock,
    TwoColumnBlock,
)

CALLOUT_TITLES = {"info": "Note", "warning": "Warning", "success": "Key result", "risk": "Risk"}


def _docx_module() -> Any:
    try:
        return import_module("docx")
    except ImportError as exc:
        raise OfficeRenderError("python-docx is required for Word rendering") from exc


def _shared() -> Any:
    return import_module("docx.shared")


def _qn() -> Any:
    return import_module("docx.oxml.ns").qn


def _OxmlElement() -> Any:
    return import_module("docx.oxml").OxmlElement


def _Pt(value: float) -> Any:
    return _shared().Pt(value)


def _Cm(value: float) -> Any:
    return _shared().Cm(value)


def _Inches(value: float) -> Any:
    return _shared().Inches(value)


def _RGBColor(hex_text: str) -> Any:
    return _shared().RGBColor.from_string(hex_text)


def _configure_styles(document: Any, theme: DocumentTheme) -> None:
    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = theme.body_font
    normal.font.size = _Pt(theme.base_size_pt)
    normal.font.color.rgb = _RGBColor(theme.body_color)
    paragraph_format = normal.paragraph_format
    paragraph_format.space_after = _Pt(7)
    paragraph_format.line_spacing = theme.line_spacing
    heading_colors = {1: 21.0, 2: 16.0, 3: 13.0, 4: 11.5}
    for level, size in heading_colors.items():
        style = styles[f"Heading {level}"]
        style.font.name = theme.heading_font
        style.font.size = _Pt(size)
        style.font.bold = True
        style.font.color.rgb = _RGBColor(theme.heading_color)
        style.paragraph_format.space_before = _Pt(16 if level == 1 else 12)
        style.paragraph_format.space_after = _Pt(6)
        style.paragraph_format.keep_with_next = True
    for name in ("List Bullet", "List Number"):
        list_style = styles[name]
        list_style.font.name = theme.body_font
        list_style.font.size = _Pt(theme.base_size_pt)
        list_style.font.color.rgb = _RGBColor(theme.body_color)
    caption = styles["Caption"]
    caption.font.name = theme.body_font
    caption.font.size = _Pt(9)
    caption.font.italic = True
    caption.font.color.rgb = _RGBColor(theme.muted_color)


def _set_cell_shading(cell: Any, fill: str) -> None:
    shading = _OxmlElement()("w:shd")
    shading.set(_qn()("w:val"), "clear")
    shading.set(_qn()("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _repeat_header_row(row: Any) -> None:
    row_properties = row._tr.get_or_add_trPr()
    header = _OxmlElement()("w:tblHeader")
    header.set(_qn()("w:val"), "true")
    row_properties.append(header)


def _set_table_borders(table: Any, border_color: str) -> None:
    table_pr = table._tbl.tblPr
    borders = _OxmlElement()("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = _OxmlElement()(f"w:{edge}")
        element.set(_qn()("w:val"), "single")
        element.set(_qn()("w:sz"), "4")
        element.set(_qn()("w:color"), border_color)
        borders.append(element)
    table_pr.append(borders)


def _fill_runs(paragraph: Any, rich: RichText, theme: DocumentTheme) -> None:
    if not rich.runs:
        return
    for run in rich.runs:
        text = run.text
        if not text:
            continue
        added = paragraph.add_run(text)
        added.font.name = theme.body_font
        if run.bold:
            added.font.bold = True
        if run.italic:
            added.font.italic = True
        if run.code:
            added.font.name = "Consolas"
            added.font.size = _Pt(theme.base_size_pt - 1.0)


def _add_callout(document: Any, block: CalloutBlock, theme: DocumentTheme) -> None:
    table = document.add_table(rows=1, cols=1)
    table.alignment = 1
    cell = table.cell(0, 0)
    _set_cell_shading(cell, theme.callout_fill_for_variant.get(block.variant, theme.callout_fill))
    title = cell.paragraphs[0]
    title_run = title.add_run(f"{CALLOUT_TITLES.get(block.variant, 'Note')}: {block.title}")
    title_run.font.bold = True
    title_run.font.size = _Pt(theme.base_size_pt)
    title_run.font.color.rgb = _RGBColor(theme.heading_color)
    if block.body:
        body = cell.add_paragraph()
        body_run = body.add_run(block.body)
        body_run.font.size = _Pt(theme.base_size_pt)
    _set_cell_margins(cell)


def _set_cell_margins(cell: Any) -> None:
    margins = _OxmlElement()("w:tcMar")
    for side, value in (("top", 80), ("bottom", 80), ("start", 120), ("end", 120)):
        element = _OxmlElement()(f"w:{side}")
        element.set(_qn()("w:w"), str(value))
        element.set(_qn()("w:type"), "dxa")
        margins.append(element)
    cell._tc.get_or_add_tcPr().append(margins)


def _add_table(document: Any, block: TableBlock, theme: DocumentTheme) -> None:
    rows = len(block.rows) + 1
    table = document.add_table(rows=rows, cols=len(block.headers))
    table.style = document.styles["Table Grid"]
    table.autofit = True
    _set_table_borders(table, theme.muted_color)
    for index, header in enumerate(block.headers):
        cell = table.cell(0, index)
        _set_cell_shading(cell, theme.table_header_fill)
        paragraph = cell.paragraphs[0]
        run = paragraph.add_run(header)
        run.font.bold = True
        run.font.size = _Pt(theme.base_size_pt)
        run.font.color.rgb = _RGBColor(theme.table_header_color)
        paragraph.paragraph_format.space_after = _Pt(2)
    _repeat_header_row(table.rows[0])
    for row_index, row in enumerate(block.rows, 1):
        if row_index % 2 == 0:
            for cell in table.rows[row_index].cells:
                _set_cell_shading(cell, theme.table_band_fill)
        for column_index, value in enumerate(row):
            cell = table.cell(row_index, column_index)
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run(str(value))
            run.font.size = _Pt(theme.base_size_pt - 0.5)
            paragraph.paragraph_format.space_after = _Pt(2)
            _set_cell_margins(cell)
    if block.caption:
        caption = document.add_paragraph(block.caption, style="Caption")
        caption.alignment = 1


def _add_timeline(document: Any, block: TimelineBlock, theme: DocumentTheme) -> None:
    table = document.add_table(rows=len(block.items), cols=2)
    table.style = document.styles["Table Grid"]
    _set_table_borders(table, theme.muted_color)
    table.columns[0].width = _Cm(2.6)
    for index, item in enumerate(block.items):
        label_cell = table.cell(index, 0)
        _set_cell_shading(label_cell, theme.table_header_fill)
        label_paragraph = label_cell.paragraphs[0]
        label_run = label_paragraph.add_run(item.label)
        label_run.font.bold = True
        label_run.font.color.rgb = _RGBColor(theme.table_header_color)
        label_run.font.size = _Pt(theme.base_size_pt)
        value_cell = table.cell(index, 1)
        title_paragraph = value_cell.paragraphs[0]
        title_run = title_paragraph.add_run(item.title)
        title_run.font.bold = True
        title_run.font.size = _Pt(theme.base_size_pt)
        if item.description:
            description = value_cell.add_paragraph()
            description_run = description.add_run(item.description)
            description_run.font.size = _Pt(theme.base_size_pt - 0.5)
            description_run.font.color.rgb = _RGBColor(theme.muted_color)
        _set_cell_margins(value_cell)
        _set_cell_margins(label_cell)


def _add_two_column(document: Any, block: TwoColumnBlock, theme: DocumentTheme) -> None:
    table = document.add_table(rows=1, cols=2)
    _set_table_borders(table, "FFFFFF")
    for column_index, blocks in ((0, block.left), (1, block.right)):
        cell = table.cell(0, column_index)
        _set_cell_margins(cell)
        first = True
        for inner in blocks:
            target = cell.paragraphs[0] if first else cell.add_paragraph()
            first = False
            _render_block_into_paragraph(target, inner, theme, document)


def _render_block_into_paragraph(
    paragraph: Any, block: Any, theme: DocumentTheme, document: Any
) -> None:
    if isinstance(block, ParagraphBlock):
        _fill_runs(paragraph, block.rich, theme)
    elif isinstance(block, ListItemBlock):
        prefix = "• " if not block.ordered else ""
        run = paragraph.add_run(f"{prefix}{block.text}")
        run.font.size = _Pt(theme.base_size_pt)
    elif isinstance(block, HeadingBlock):
        run = paragraph.add_run(block.text)
        run.font.bold = True
        run.font.size = _Pt(theme.base_size_pt + 1.5)
        run.font.color.rgb = _RGBColor(theme.heading_color)
    else:
        run = paragraph.add_run("")
        run.font.size = _Pt(theme.base_size_pt)


def _add_page_number_field(paragraph: Any) -> None:
    run = paragraph.add_run()
    field_begin = _OxmlElement()("w:fldChar")
    field_begin.set(_qn()("w:fldCharType"), "begin")
    instruction = _OxmlElement()("w:instrText")
    instruction.set(_qn()("xml:space"), "preserve")
    instruction.text = " PAGE "
    field_separate = _OxmlElement()("w:fldChar")
    field_separate.set(_qn()("w:fldCharType"), "separate")
    field_end = _OxmlElement()("w:fldChar")
    field_end.set(_qn()("w:fldCharType"), "end")
    run._r.append(field_begin)
    run._r.append(instruction)
    run._r.append(field_separate)
    run._r.append(field_end)


def _add_toc_field(document: Any) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    field_begin = _OxmlElement()("w:fldChar")
    field_begin.set(_qn()("w:fldCharType"), "begin")
    instruction = _OxmlElement()("w:instrText")
    instruction.set(_qn()("xml:space"), "preserve")
    instruction.text = ' TOC \\o "1-3" \\h \\z \\u '
    field_separate = _OxmlElement()("w:fldChar")
    field_separate.set(_qn()("w:fldCharType"), "separate")
    placeholder = _OxmlElement()("w:t")
    placeholder.text = "Table of contents will update when opened in Word (F9)."
    field_end = _OxmlElement()("w:fldChar")
    field_end.set(_qn()("w:fldCharType"), "end")
    run._r.append(field_begin)
    run._r.append(instruction)
    run._r.append(field_separate)
    run._r.append(placeholder)
    run._r.append(field_end)


def _add_cover(document: Any, spec: DocumentSpec, theme: DocumentTheme) -> None:
    cover_table = document.add_table(rows=1, cols=1)
    cell = cover_table.cell(0, 0)
    _set_cell_shading(cell, theme.cover_fill)
    cell.width = _Inches(6.9)
    spacer = cell.paragraphs[0]
    spacer.paragraph_format.space_after = _Pt(60)
    title = cell.add_paragraph()
    title.alignment = 1
    title_run = title.add_run(spec.metadata.title)
    title_run.font.name = theme.heading_font
    title_run.font.size = _Pt(30)
    title_run.font.bold = True
    title_run.font.color.rgb = _RGBColor(theme.cover_text)
    if spec.metadata.subtitle:
        subtitle = cell.add_paragraph()
        subtitle.alignment = 1
        subtitle_run = subtitle.add_run(spec.metadata.subtitle)
        subtitle_run.font.size = _Pt(14)
        subtitle_run.font.color.rgb = _RGBColor(theme.cover_muted)
    meta_line = "  ·  ".join(
        part for part in (spec.metadata.author, spec.metadata.date_text) if part
    )
    if meta_line:
        meta = cell.add_paragraph()
        meta.alignment = 1
        meta.paragraph_format.space_before = _Pt(24)
        meta_run = meta.add_run(meta_line)
        meta_run.font.size = _Pt(11)
        meta_run.font.color.rgb = _RGBColor(theme.cover_muted)
    closing = cell.add_paragraph()
    closing.paragraph_format.space_after = _Pt(60)
    document.add_page_break()


def _configure_section(document: Any, spec: DocumentSpec, theme: DocumentTheme) -> None:
    section = document.sections[0]
    section.top_margin = _Cm(spec.page.margin_cm[0])
    section.bottom_margin = _Cm(spec.page.margin_cm[1])
    section.left_margin = _Cm(spec.page.margin_cm[2])
    section.right_margin = _Cm(spec.page.margin_cm[3])
    if spec.header_footer.header_text:
        header = section.header
        header.paragraphs[0].text = spec.header_footer.header_text
        for run in header.paragraphs[0].runs:
            run.font.size = _Pt(9)
            run.font.color.rgb = _RGBColor(theme.muted_color)
    if spec.header_footer.footer_text or spec.header_footer.page_numbers:
        footer = section.footer
        footer_paragraph = footer.paragraphs[0]
        footer_paragraph.alignment = 1
        if spec.header_footer.footer_text:
            text_run = footer_paragraph.add_run(spec.header_footer.footer_text + "    ·    ")
            text_run.font.size = _Pt(9)
            text_run.font.color.rgb = _RGBColor(theme.muted_color)
        if spec.header_footer.page_numbers:
            _add_page_number_field(footer_paragraph)
            for run in footer_paragraph.runs:
                run.font.size = _Pt(9)
                run.font.color.rgb = _RGBColor(theme.muted_color)


def _render_block(document: Any, block: Any, theme: DocumentTheme) -> None:
    if isinstance(block, HeadingBlock):
        document.add_heading(block.text, level=block.level)
    elif isinstance(block, ParagraphBlock):
        paragraph = document.add_paragraph()
        _fill_runs(paragraph, block.rich, theme)
    elif isinstance(block, ListItemBlock):
        style = "List Number" if block.ordered else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        run = paragraph.add_run(block.text)
        run.font.name = theme.body_font
    elif isinstance(block, TableBlock):
        _add_table(document, block, theme)
    elif isinstance(block, ImageBlock):
        _add_image(document, block, theme)
    elif isinstance(block, QuoteBlock):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = _Cm(1.0)
        paragraph.paragraph_format.space_before = _Pt(8)
        _fill_runs(paragraph, block.rich, theme)
        for run in paragraph.runs:
            run.font.italic = True
        if block.attribution:
            attribution = document.add_paragraph()
            attribution.paragraph_format.left_indent = _Cm(1.0)
            attribution_run = attribution.add_run(f"— {block.attribution}")
            attribution_run.font.size = _Pt(theme.base_size_pt - 1)
            attribution_run.font.color.rgb = _RGBColor(theme.muted_color)
    elif isinstance(block, CalloutBlock):
        _add_callout(document, block, theme)
    elif isinstance(block, TimelineBlock):
        _add_timeline(document, block, theme)
    elif isinstance(block, TwoColumnBlock):
        _add_two_column(document, block, theme)
    elif isinstance(block, PageBreakBlock):
        document.add_page_break()
    elif isinstance(block, BibliographyBlock):
        document.add_heading("References", level=1)
        for entry in block.entries:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = _Cm(0.75)
            paragraph.paragraph_format.first_line_indent = _Cm(-0.75)
            run = paragraph.add_run(entry)
            run.font.size = _Pt(theme.base_size_pt - 0.5)
    else:
        raise OfficeRenderError(f"unsupported document block: {type(block).__name__}")


def _add_image(document: Any, block: ImageBlock, theme: DocumentTheme) -> None:
    image_path = Path(block.path)
    if not image_path.is_file():
        paragraph = document.add_paragraph()
        run = paragraph.add_run(f"[missing image: {block.path}]")
        run.font.color.rgb = _RGBColor("B3261E")
        run.font.italic = True
        return
    try:
        document.add_picture(str(image_path), width=_Cm(block.width_cm))
    except Exception as exc:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(f"[unreadable image: {block.path}]")
        run.font.color.rgb = _RGBColor("B3261E")
        run.font.italic = True
        raise OfficeRenderError(f"cannot embed image {block.path}: {exc}") from exc
    last_paragraph = document.paragraphs[-1]
    last_paragraph.alignment = 1
    if block.caption:
        caption = document.add_paragraph(block.caption, style="Caption")
        caption.alignment = 1


def render_document(spec: DocumentSpec, output_path: Path) -> Path:
    docx = _docx_module()
    document = docx.Document()
    theme = theme_for_style(spec.style)
    _configure_styles(document, theme)
    core = document.core_properties
    core.title = spec.metadata.title
    core.subject = spec.metadata.subject
    core.author = spec.metadata.author
    core.keywords = ", ".join(spec.metadata.keywords)
    if spec.metadata.language:
        core.language = spec.metadata.language
    _configure_section(document, spec, theme)
    _add_cover(document, spec, theme)
    if spec.include_toc:
        document.add_heading("Contents", level=1)
        _add_toc_field(document)
    for block in spec.blocks:
        _render_block(document, block, theme)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))
    return output_path
