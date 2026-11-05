from __future__ import annotations

import html as html_module
from dataclasses import dataclass

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
from eurus.office.models.presentation import ChartData, PresentationSpec

PX_PER_INCH = 96.0
PT_TO_PX = 96.0 / 72.0


def inches_to_px(value: float) -> int:
    return round(value * PX_PER_INCH)


def pt_to_px(value: float) -> int:
    return round(value * PT_TO_PX)


def escape(text: str) -> str:
    return html_module.escape(str(text), quote=True)


@dataclass(frozen=True, slots=True)
class PreviewBuild:
    document: str
    slide_count: int
    title: str


def _text_element_html(element: TextElement, tokens: DesignTokens) -> str:
    color = color_for_role(tokens, element.color_role)
    font = tokens.display_font if element.font_role == "display" else tokens.body_font
    weight = "font-weight:700;" if element.bold else "font-weight:400;"
    italic = "font-style:italic;" if element.italic else ""
    align = {"left": "left", "center": "center", "right": "right"}.get(element.align, "left")
    spacing = f"letter-spacing:{element.spacing_pt}px;" if element.spacing_pt else ""
    return (
        f'<div class="el text" style="left:{inches_to_px(element.x)}px;'
        f"top:{inches_to_px(element.y)}px;"
        f"width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;"
        f"font-size:{pt_to_px(element.size_pt)}px;color:{color};{weight}{italic}"
        f'text-align:{align};font-family:{font};{spacing}">{escape(element.text)}</div>'
    )


def _bullets_element_html(element: BulletsElement, tokens: DesignTokens) -> str:
    color = color_for_role(tokens, element.color_role)
    items = []
    for index, item in enumerate(element.items, 1):
        marker = f"{index}." if element.numbered else "•"
        items.append(
            f'<li style="margin-bottom:{element.gap_pt}px;"><span class="marker">{marker}</span>'
            f"{escape(item)}</li>"
        )
    return (
        f'<div class="el bullets" style="left:{inches_to_px(element.x)}px;'
        f"top:{inches_to_px(element.y)}px;"
        f"width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;"
        f'font-size:{pt_to_px(element.size_pt)}px;color:{color};">'
        f'<ul style="list-style:none;padding:0;margin:0;">{"".join(items)}</ul></div>'
    )


def _shape_element_html(element: ShapeElement, tokens: DesignTokens) -> str:
    fill = color_for_role(tokens, element.fill_role)
    radius = (
        "50%" if element.kind == "circle" else f"{max(2, round(element.radius * PX_PER_INCH))}px"
    )
    shadow = "box-shadow:0 10px 24px rgba(15,23,42,.12);" if element.shadow else ""
    return (
        f'<div class="el shape" style="left:{inches_to_px(element.x)}px;'
        f"top:{inches_to_px(element.y)}px;"
        f"width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;"
        f'background:{fill};border-radius:{radius};{shadow}"></div>'
    )


def _chart_svg(chart: ChartData, tokens: DesignTokens, width_px: int, height_px: int) -> str:
    muted = tokens.muted
    accent = tokens.accent
    soft = tokens.accent_soft
    border = tokens.border
    series_colors = (accent, muted, soft, border)
    padding = 42
    plot_w = max(width_px - padding * 2 - 10, 40)
    plot_h = max(height_px - padding * 2 - 30, 30)
    if chart.kind == "pie":
        total = sum(sum(series.values) for series in chart.series) or 1.0
        slices = list(zip(chart.categories, chart.series[0].values, strict=True))
        cx, cy, radius = width_px / 2, height_px / 2, min(plot_w, plot_h) / 2
        angle = -90.0
        parts = []
        for slice_index, (_label, value) in enumerate(slices):
            share = (value / total) * 360.0
            end = angle + share
            large = 1 if share > 180 else 0
            import math

            x1 = cx + radius * math.cos(math.radians(angle))
            y1 = cy + radius * math.sin(math.radians(angle))
            x2 = cx + radius * math.cos(math.radians(end))
            y2 = cy + radius * math.sin(math.radians(end))
            color = series_colors[slice_index % len(series_colors)]
            parts.append(
                f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                f'A{radius:.1f},{radius:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>'
                f'{y2:.1f} Z" fill="{color}"/>'
            )
            angle = end
        return (
            f'<svg width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
            f"{''.join(parts)}</svg>"
        )
    categories = chart.categories
    count = len(categories)
    max_value = (
        max((value for series in chart.series for value in series.values), default=1.0) or 1.0
    )
    lines = [
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" '
        f'y2="{padding + plot_h}" stroke="{border}" stroke-width="1"/>',
        f'<line x1="{padding}" y1="{padding + plot_h}" '
        f'x2="{padding + plot_w}" y2="{padding + plot_h}" stroke="{border}" stroke-width="1"/>',
    ]
    if chart.kind == "line":
        for series_index, series in enumerate(chart.series):
            color = series_colors[series_index % len(series_colors)]
            step = plot_w / max(count - 1, 1)
            points = []
            for index, value in enumerate(series.values):
                x = padding + index * step
                y = padding + plot_h - (value / max_value) * plot_h
                points.append(f"{x:.1f},{y:.1f}")
            lines.append(
                f'<polyline points="{" ".join(points)}" fill="none" '
                f'stroke="{color}" stroke-width="3"/>'
            )
    else:
        group_w = plot_w / max(count, 1)
        bar_w = group_w / max(len(chart.series), 1) * 0.7
        for series_index, series in enumerate(chart.series):
            color = series_colors[series_index % len(series_colors)]
            for index, value in enumerate(series.values):
                bar_height = (value / max_value) * plot_h
                x = (
                    padding
                    + index * group_w
                    + series_index * (group_w / max(len(chart.series), 1))
                    + bar_w * 0.15
                )
                y = padding + plot_h - bar_height
                lines.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_height:.1f}"'
                    f'fill="{color}" rx="3"/>'
                )
    for index, label in enumerate(categories):
        if count > 14 and index % 2 == 1:
            continue
        x = padding + (index + 0.5) * (plot_w / max(count, 1))
        lines.append(
            f'<text x="{x:.1f}" y="{padding + plot_h + 16}" font-size="10"'
            f'fill="{muted}" text-anchor="middle">{escape(label)}</text>'
        )
    return (
        f'<svg width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}">'
        f"{''.join(lines)}</svg>"
    )


def _chart_element_html(element: ChartElement, tokens: DesignTokens) -> str:
    width_px = inches_to_px(element.w)
    height_px = inches_to_px(element.h)
    return (
        f'<div class="el chart" style="left:{inches_to_px(element.x)}px;'
        f'top:{inches_to_px(element.y)}px;width:{width_px}px;height:{height_px}px;"'
        f'width:{width_px}px;height:{height_px}px;">'
        f'<div class="chart-title">{escape(element.chart.title)}</div>'
        f"{_chart_svg(element.chart, tokens, width_px, height_px - 34)}</div>"
    )


def _image_element_html(element: ImageElement) -> str:
    return (
        f'<div class="el image" style="left:{inches_to_px(element.x)}px;'
        f"top:{inches_to_px(element.y)}px;"
        f'width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;">'
        f'width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;">'
        f'<img src="file://{escape(element.path)}" '
        'style="width:100%;height:100%;object-fit:cover;border-radius:8px;" alt=""/></div>'
    )


def _chevron_element_html(element: ChevronElement, tokens: DesignTokens) -> str:
    fill = color_for_role(tokens, element.fill_role)
    contrast = tokens.accent_contrast
    return (
        f'<div class="el chevron" style="left:{inches_to_px(element.x)}px;'
        f"top:{inches_to_px(element.y)}px;"
        f"width:{inches_to_px(element.w)}px;height:{inches_to_px(element.h)}px;"
        f"background:{fill};"
        "clip-path:polygon(0 0, calc(100% - 18px) 0, 100% 50%, "
        'calc(100% - 18px) 100%, 0 100%, 18px 50%);">'
        f'<span style="color:{contrast};font-weight:700;font-size:{pt_to_px(14)}px;">'
        f"{escape(element.text)}</span></div>"
    )


def _slide_html(model: SlideLayoutModel, tokens: DesignTokens) -> str:
    parts: list[str] = []
    for element in model.elements:
        if isinstance(element, TextElement):
            if not element.text:
                continue
            parts.append(_text_element_html(element, tokens))
        elif isinstance(element, BulletsElement):
            parts.append(_bullets_element_html(element, tokens))
        elif isinstance(element, ShapeElement):
            parts.append(_shape_element_html(element, tokens))
        elif isinstance(element, ChartElement):
            parts.append(_chart_element_html(element, tokens))
        elif isinstance(element, ImageElement):
            parts.append(_image_element_html(element))
        elif isinstance(element, ChevronElement):
            parts.append(_chevron_element_html(element, tokens))
    return (
        f'<section class="slide" id="slide-{model.index}" '
        f'style="background:{model.background};">{"".join(parts)}</section>'
    )


def render_preview_html(spec: PresentationSpec, models: list[SlideLayoutModel]) -> str:
    tokens = tokens_for_direction(spec.direction)
    slides = "".join(_slide_html(model, tokens) for model in models)
    body_font = tokens.body_font
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(spec.title)} — Eurus deck preview</title>
<style>
:root{{--radius:{tokens.radius_px}px}}
*{{box-sizing:border-box}}
body{{margin:0;background:#20242B;color:{tokens.ink};font-family:{body_font};padding:28px 0 60px}}
main{{display:grid;gap:26px;justify-items:center}}
.slide{{position:relative;width:1280px;height:720px;overflow:hidden;border-radius:8px;
box-shadow:0 18px 44px rgba(0,0,0,.35)}}
.el{{position:absolute}}
.el.text{{line-height:1.25;white-space:pre-wrap;overflow:hidden}}
.el.bullets ul li{{line-height:1.3}}
.el.bullets .marker{{color:{tokens.accent};font-weight:700;margin-right:8px}}
.el.chart-title{{font-size:14px;font-weight:700;color:{tokens.ink};
margin:0 0 6px 4px;font-family:{tokens.display_font}}}
h1.deck-title{{font-family:{tokens.display_font};color:#F4F5F7;font-size:22px;
margin:18px 24px 0;max-width:1200px}}
p.deck-note{{color:#A7ADBA;margin:6px 24px 0;font-size:13px}}
</style>
</head>
<body>
<h1 class="deck-title">{escape(spec.title)}</h1>
            '<p class="deck-note">{escape(spec.subtitle)} · {len(spec.slides)} slides · '</
            'preview generated from the same layout model as the PPTX export</p>'
<main>{slides}</main>
</body>
</html>"""


def build_preview(spec: PresentationSpec, models: list[SlideLayoutModel]) -> PreviewBuild:
    document = render_preview_html(spec, models)
    return PreviewBuild(document=document, slide_count=len(spec.slides), title=spec.title)
