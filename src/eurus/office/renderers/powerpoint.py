from __future__ import annotations

import asyncio
import contextlib
from importlib import import_module
from pathlib import Path
from typing import Any

from eurus.office.design.layouts import (
    BulletsElement,
    ChartElement,
    ChevronElement,
    ImageElement,
    ShapeElement,
    SlideLayoutModel,
    TextElement,
    color_for_role,
)
from eurus.office.design.tokens import DesignTokens, tokens_for_direction
from eurus.office.errors import OfficeRenderError
from eurus.office.models.presentation import PresentationSpec

CHART_TYPES = {"bar": 57, "column": 51, "line": 65, "pie": 5}
FONT_SANS = "Aptos"
FONT_SERIF = "Georgia"


def _pptx_module() -> Any:
    try:
        return import_module("pptx")
    except ImportError as exc:
        raise OfficeRenderError("python-pptx is required for PowerPoint rendering") from exc


def _font_for(tokens: DesignTokens, role: str) -> str:
    stack = tokens.display_font if role == "display" else tokens.body_font
    first = stack.split(",")[0].strip().strip("'")
    return FONT_SERIF if "Georgia" in first else FONT_SANS


def _rgb(pptx: Any, hex_color: str) -> Any:
    return pptx.dml.color.RGBColor.from_string(hex_color.lstrip("#").upper())


ALIGNMENTS = {"left": 1, "center": 2, "right": 3}


def _add_text(
    slide: Any,
    pptx: Any,
    tokens: DesignTokens,
    element: TextElement,
    scale: float = 1.0,
) -> None:
    box = slide.shapes.add_textbox(
        _inches(element.x), _inches(element.y), _inches(element.w), _inches(element.h)
    )
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.vertical_anchor = 1
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    lines = element.text.split("\n") or [""]
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = ALIGNMENTS.get(element.align, 1)
        run = paragraph.add_run()
        run.text = line
        font = run.font
        font.name = _font_for(tokens, element.font_role)
        font.size = _points(element.size_pt)
        font.bold = element.bold
        font.italic = element.italic
        font.color.rgb = _rgb(pptx, color_for_role(tokens, element.color_role))
        if element.spacing_pt:
            paragraph.space_after = _points(element.spacing_pt)


def _add_bullets(slide: Any, pptx: Any, tokens: DesignTokens, element: BulletsElement) -> None:
    box = slide.shapes.add_textbox(
        _inches(element.x), _inches(element.y), _inches(element.w), _inches(element.h)
    )
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.margin_left = 0
    frame.margin_top = 0
    for index, item in enumerate(element.items):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        marker = f"{index + 1}. " if element.numbered else "•  "
        run = paragraph.add_run()
        run.text = f"{marker}{item}"
        font = run.font
        font.name = _font_for(tokens, "body")
        font.size = _points(element.size_pt)
        font.color.rgb = _rgb(pptx, color_for_role(tokens, element.color_role))
        paragraph.space_after = _points(element.gap_pt)


def _inches(value: float) -> Any:
    from pptx.util import Inches

    return Inches(value)


def _points(value: float) -> Any:
    from pptx.util import Pt

    return Pt(value)


def _add_shape(slide: Any, pptx: Any, tokens: DesignTokens, element: ShapeElement) -> None:
    from pptx.enum.shapes import MSO_SHAPE

    shape_type = MSO_SHAPE.OVAL if element.kind == "circle" else MSO_SHAPE.ROUNDED_RECTANGLE
    shape = slide.shapes.add_shape(
        shape_type, _inches(element.x), _inches(element.y), _inches(element.w), _inches(element.h)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(pptx, color_for_role(tokens, element.fill_role))
    if element.line_role:
        shape.line.color.rgb = _rgb(pptx, color_for_role(tokens, element.line_role))
    else:
        shape.line.fill.background()
    if element.kind in {"rect", "panel", "chip"}:
        with contextlib.suppress(Exception, IndexError):
            shape.adjustments[0] = min(0.5, element.radius / max(element.w, 0.01))
    shape.shadow.inherit = False


def _add_chart(slide: Any, pptx: Any, tokens: DesignTokens, element: ChartElement) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.util import Pt

    chart_type = CHART_TYPES.get(element.chart.kind, 51)
    data = CategoryChartData()
    data.categories = list(element.chart.categories)
    for series in element.chart.series:
        data.add_series(series.name, tuple(float(value) for value in series.values))
    frame = slide.shapes.add_chart(
        chart_type,
        _inches(element.x),
        _inches(element.y),
        _inches(element.w),
        _inches(element.h),
        data,
    )
    chart = frame.chart
    chart.has_title = True
    chart.chart_title.text_frame.text = element.chart.title
    for run in chart.chart_title.text_frame.paragraphs[0].runs:
        run.font.size = Pt(13)
        run.font.bold = True
        run.font.color.rgb = _rgb(pptx, tokens.ink)
        run.font.name = _font_for(tokens, "body")
    chart.has_legend = len(element.chart.series) > 1
    if chart.has_legend:
        from pptx.enum.chart import XL_LEGEND_POSITION

        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(10)
        chart.legend.font.color.rgb = _rgb(pptx, tokens.muted)
    series_colors = (tokens.accent, tokens.muted, tokens.accent_soft, tokens.border)
    for index, series in enumerate(chart.series):
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = _rgb(pptx, series_colors[index % len(series_colors)])
    with contextlib.suppress(Exception):
        _style_chart_axes(chart, pptx, tokens)


def _add_image(slide: Any, element: ImageElement) -> None:
    path = Path(element.path)
    if not path.is_file():
        _add_missing_image_placeholder(slide, element)
        return
    try:
        slide.shapes.add_picture(
            str(path),
            _inches(element.x),
            _inches(element.y),
            _inches(element.w),
            _inches(element.h),
        )
    except Exception:
        _add_missing_image_placeholder(slide, element)


def _add_missing_image_placeholder(slide: Any, element: ImageElement) -> None:
    box = slide.shapes.add_textbox(
        _inches(element.x), _inches(element.y), _inches(element.w), _inches(element.h)
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.paragraphs[0].text = f"[missing image: {Path(element.path).name}]"


def _add_chevron(slide: Any, pptx: Any, tokens: DesignTokens, element: ChevronElement) -> None:
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(
        MSO_SHAPE.CHEVRON,
        _inches(element.x),
        _inches(element.y),
        _inches(element.w),
        _inches(element.h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(pptx, color_for_role(tokens, element.fill_role))
    shape.line.fill.background()
    shape.shadow.inherit = False
    frame = shape.text_frame
    frame.clear()
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    paragraph.alignment = 2
    run = paragraph.add_run()
    run.text = element.text
    run.font.size = _points(13.0)
    run.font.bold = True
    run.font.name = _font_for(tokens, "body")
    run.font.color.rgb = _rgb(pptx, tokens.accent_contrast)


def _fill_slide(slide: Any, pptx: Any, tokens: DesignTokens, model: SlideLayoutModel) -> None:
    for element in model.elements:
        if isinstance(element, ShapeElement):
            _add_shape(slide, pptx, tokens, element)
        elif isinstance(element, ChevronElement):
            _add_chevron(slide, pptx, tokens, element)
        elif isinstance(element, ChartElement):
            _add_chart(slide, pptx, tokens, element)
        elif isinstance(element, ImageElement):
            _add_image(slide, element)
        elif isinstance(element, BulletsElement):
            _add_bullets(slide, pptx, tokens, element)
        elif isinstance(element, TextElement) and element.text:
            _add_text(slide, pptx, tokens, element)


def _style_chart_axes(chart: Any, pptx: Any, tokens: DesignTokens) -> None:
    from pptx.util import Pt

    category_axis = chart.category_axis
    category_axis.tick_labels.font.size = Pt(10)
    category_axis.tick_labels.font.color.rgb = _rgb(pptx, tokens.muted)
    value_axis = chart.value_axis
    value_axis.tick_labels.font.size = Pt(10)
    value_axis.tick_labels.font.color.rgb = _rgb(pptx, tokens.muted)


def render_presentation(
    spec: PresentationSpec, models: list[SlideLayoutModel], output_path: Path
) -> Path:
    pptx = _pptx_module()
    from pptx.util import Emu

    tokens = tokens_for_direction(spec.direction)
    presentation = pptx.Presentation()
    presentation.slide_width = Emu(12192000)
    presentation.slide_height = Emu(6858000)
    blank = presentation.slide_layouts[6]
    for model in models:
        slide = presentation.slides.add_slide(blank)
        background = slide.background.fill
        background.solid()
        background.fore_color.rgb = _rgb(pptx, model.background)
        _fill_slide(slide, pptx, tokens, model)
    core = presentation.core_properties
    core.title = spec.title
    core.subject = spec.subtitle
    core.author = spec.author
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(output_path))
    return output_path


async def render_presentation_async(
    spec: PresentationSpec, models: list[SlideLayoutModel], output_path: Path
) -> Path:
    return await asyncio.to_thread(render_presentation, spec, models, output_path)
