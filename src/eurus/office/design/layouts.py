from __future__ import annotations

from dataclasses import dataclass

from eurus.office.design.tokens import DesignTokens, is_dark, tokens_for_direction
from eurus.office.models.presentation import (
    ChartData,
    ComparisonColumn,
    MetricItem,
    PresentationSpec,
    ProcessStep,
    SlideSpec,
    TimelineEntry,
)

SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5
MARGIN_IN = 0.68
CONTENT_TOP_IN = 1.82
CONTENT_BOTTOM_IN = 6.85
FOOTER_Y_IN = 7.02

TEXT_ROLES = (
    "kicker",
    "title",
    "subtitle",
    "body",
    "bullet",
    "metric_value",
    "metric_label",
    "footer",
    "page_number",
    "quote",
    "attribution",
    "reference",
    "chip",
    "big_number",
    "callout",
)


@dataclass(frozen=True, slots=True)
class TextElement:
    role: str
    x: float
    y: float
    w: float
    h: float
    text: str
    size_pt: float
    color_role: str = "ink"
    bold: bool = False
    italic: bool = False
    align: str = "left"
    font_role: str = "body"
    spacing_pt: float = 0.0


@dataclass(frozen=True, slots=True)
class BulletsElement:
    x: float
    y: float
    w: float
    h: float
    items: tuple[str, ...]
    size_pt: float = 16.0
    color_role: str = "ink"
    numbered: bool = False
    gap_pt: float = 10.0


@dataclass(frozen=True, slots=True)
class ShapeElement:
    kind: str
    x: float
    y: float
    w: float
    h: float
    fill_role: str = "accent"
    line_role: str = ""
    radius: float = 0.06
    shadow: bool = False


@dataclass(frozen=True, slots=True)
class ChartElement:
    x: float
    y: float
    w: float
    h: float
    chart: ChartData


@dataclass(frozen=True, slots=True)
class ImageElement:
    x: float
    y: float
    w: float
    h: float
    path: str
    caption: str = ""


@dataclass(frozen=True, slots=True)
class ChevronElement:
    x: float
    y: float
    w: float
    h: float
    text: str
    description: str = ""
    fill_role: str = "accent"


SlideElement = (
    TextElement | BulletsElement | ShapeElement | ChartElement | ImageElement | ChevronElement
)


@dataclass(frozen=True, slots=True)
class SlideLayoutModel:
    index: int
    layout: str
    direction: str
    background: str
    elements: tuple[SlideElement, ...]
    footer_text: str = ""
    fallback_note: str = ""


def color_for_role(tokens: DesignTokens, role: str) -> str:
    if role == "ink":
        return tokens.ink
    if role == "muted":
        return tokens.muted
    if role == "accent":
        return tokens.accent
    if role == "accent_contrast":
        return tokens.accent_contrast
    if role == "background":
        return tokens.background
    if role == "surface":
        return tokens.surface
    if role == "border":
        return tokens.border
    if role == "accent_soft":
        return tokens.accent_soft
    return tokens.ink


def _title_block(
    slide: SlideSpec, tokens: DesignTokens, *, y: float = 0.62, size: float = 0.0
) -> TextElement:
    return TextElement(
        role="title",
        x=MARGIN_IN,
        y=y,
        w=SLIDE_WIDTH_IN - 2 * MARGIN_IN,
        h=0.85,
        text=slide.title,
        size_pt=size or tokens.title_size_pt,
        color_role="ink",
        bold=True,
        font_role="display",
    )


def _kicker_block(slide: SlideSpec, tokens: DesignTokens) -> TextElement:
    text = slide.kicker or ""
    return TextElement(
        role="kicker",
        x=MARGIN_IN,
        y=0.38,
        w=SLIDE_WIDTH_IN - 2 * MARGIN_IN,
        h=0.32,
        text=text.upper(),
        size_pt=tokens.kicker_size_pt,
        color_role="accent",
        bold=True,
        spacing_pt=2.0,
    )


def _footer_blocks(
    index: int, total: int, tokens: DesignTokens, footer_text: str, dark: bool
) -> tuple[TextElement, TextElement]:
    return (
        TextElement(
            role="footer",
            x=MARGIN_IN,
            y=FOOTER_Y_IN,
            w=SLIDE_WIDTH_IN - 2 * MARGIN_IN - 1.0,
            h=0.3,
            text=footer_text,
            size_pt=tokens.footer_size_pt,
            color_role="muted",
        ),
        TextElement(
            role="page_number",
            x=SLIDE_WIDTH_IN - MARGIN_IN - 1.0,
            y=FOOTER_Y_IN,
            w=1.0,
            h=0.3,
            text=f"{index:02d} / {total:02d}",
            size_pt=tokens.footer_size_pt,
            color_role="muted",
            align="right",
        ),
    )


def _accent_side_bar(tokens: DesignTokens) -> ShapeElement:
    return ShapeElement(
        kind="rect",
        x=0.0,
        y=0.0,
        w=0.14,
        h=SLIDE_HEIGHT_IN,
        fill_role="accent",
        radius=0.0,
    )


def _callout_bar(y: float, text: str, tokens: DesignTokens) -> tuple[ShapeElement, TextElement]:
    return (
        ShapeElement(
            kind="rect",
            x=MARGIN_IN,
            y=y,
            w=SLIDE_WIDTH_IN - 2 * MARGIN_IN,
            h=0.62,
            fill_role="accent",
            radius=0.08,
        ),
        TextElement(
            role="callout",
            x=MARGIN_IN + 0.25,
            y=y + 0.1,
            w=SLIDE_WIDTH_IN - 2 * MARGIN_IN - 0.5,
            h=0.42,
            text=text,
            size_pt=14.0,
            color_role="accent_contrast",
            bold=True,
        ),
    )


def plan_cover(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [
        ShapeElement(
            kind="rect",
            x=0.0,
            y=0.0,
            w=SLIDE_WIDTH_IN,
            h=SLIDE_HEIGHT_IN,
            fill_role="background",
            radius=0.0,
        ),
        ShapeElement(
            kind="rect", x=0.0, y=0.0, w=SLIDE_WIDTH_IN, h=0.16, fill_role="accent", radius=0.0
        ),
        ShapeElement(
            kind="rect",
            x=0.0,
            y=SLIDE_HEIGHT_IN - 0.16,
            w=SLIDE_WIDTH_IN,
            h=0.16,
            fill_role="accent",
            radius=0.0,
        ),
        ShapeElement(kind="circle", x=10.9, y=1.15, w=1.7, h=1.7, fill_role="accent_soft"),
        ShapeElement(kind="circle", x=11.6, y=2.4, w=0.9, h=0.9, fill_role="accent"),
        ShapeElement(
            kind="rect",
            x=MARGIN_IN,
            y=2.05,
            w=0.9,
            h=0.075,
            fill_role="accent",
            radius=0.0,
        ),
    ]
    if slide.kicker:
        elements.append(
            TextElement(
                role="kicker",
                x=MARGIN_IN,
                y=1.55,
                w=9.0,
                h=0.4,
                text=slide.kicker.upper(),
                size_pt=tokens.kicker_size_pt,
                color_role="accent",
                bold=True,
                spacing_pt=3.0,
            )
        )
    elements.append(
        TextElement(
            role="title",
            x=MARGIN_IN,
            y=2.3,
            w=10.4,
            h=1.9,
            text=slide.title or "",
            size_pt=44.0,
            color_role="ink",
            bold=True,
            font_role="display",
        )
    )
    body_y = 4.3
    if slide.subtitle:
        elements.append(
            TextElement(
                role="subtitle",
                x=MARGIN_IN,
                y=body_y,
                w=9.6,
                h=0.9,
                text=slide.subtitle,
                size_pt=19.0,
                color_role="muted",
            )
        )
        body_y += 1.0
    byline = "  ·  ".join(part for part in (slide.body[0] if slide.body else "", footer) if part)
    if byline:
        elements.append(
            TextElement(
                role="body",
                x=MARGIN_IN,
                y=body_y,
                w=9.6,
                h=0.4,
                text=byline,
                size_pt=13.0,
                color_role="muted",
            )
        )
    return tuple(elements)


def plan_agenda(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [
        ShapeElement(
            kind="rect", x=0.0, y=0.0, w=4.4, h=SLIDE_HEIGHT_IN, fill_role="surface", radius=0.0
        ),
        ShapeElement(
            kind="rect", x=4.4, y=0.0, w=0.1, h=SLIDE_HEIGHT_IN, fill_role="accent", radius=0.0
        ),
        TextElement(
            role="title",
            x=MARGIN_IN,
            y=1.1,
            w=3.4,
            h=1.6,
            text=slide.title or "Agenda",
            size_pt=30.0,
            color_role="ink",
            bold=True,
            font_role="display",
        ),
    ]
    if slide.subtitle:
        elements.append(
            TextElement(
                role="subtitle",
                x=MARGIN_IN,
                y=2.7,
                w=3.4,
                h=1.2,
                text=slide.subtitle,
                size_pt=13.0,
                color_role="muted",
            )
        )
    items = slide.bullets or tuple(f"Part {number}" for number in range(1, 5))
    row_y = 0.95
    row_step = min(1.05, (CONTENT_BOTTOM_IN - row_y) / max(len(items), 1))
    for number, item in enumerate(items, 1):
        elements.append(
            ShapeElement(
                kind="chip", x=5.0, y=row_y, w=0.52, h=0.52, fill_role="accent", radius=0.26
            )
        )
        elements.append(
            TextElement(
                role="big_number",
                x=5.0,
                y=row_y + 0.055,
                w=0.52,
                h=0.42,
                text=str(number),
                size_pt=16.0,
                color_role="accent_contrast",
                bold=True,
                align="center",
            )
        )
        elements.append(
            TextElement(
                role="bullet",
                x=5.75,
                y=row_y + 0.05,
                w=6.7,
                h=0.5,
                text=item,
                size_pt=17.0,
                color_role="ink",
            )
        )
        row_y += row_step
    return tuple(elements)


def plan_section_divider(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [
        ShapeElement(
            kind="rect",
            x=0.0,
            y=0.0,
            w=SLIDE_WIDTH_IN,
            h=SLIDE_HEIGHT_IN,
            fill_role="background",
            radius=0.0,
        ),
        ShapeElement(
            kind="rect", x=0.0, y=0.0, w=0.6, h=SLIDE_HEIGHT_IN, fill_role="accent", radius=0.0
        ),
        ShapeElement(kind="circle", x=11.2, y=4.9, w=1.9, h=1.9, fill_role="accent_soft"),
    ]
    number_text = slide.kicker or f"{index:02d}"
    elements.append(
        TextElement(
            role="big_number",
            x=1.3,
            y=1.7,
            w=3.4,
            h=2.6,
            text=number_text.upper() if len(number_text) <= 4 else number_text,
            size_pt=110.0 if len(number_text) <= 4 else 40.0,
            color_role="accent_soft",
            bold=True,
            font_role="display",
        )
    )
    elements.append(
        TextElement(
            role="title",
            x=1.35,
            y=3.6,
            w=9.8,
            h=1.4,
            text=slide.title or "",
            size_pt=38.0,
            color_role="ink",
            bold=True,
            font_role="display",
        )
    )
    if slide.subtitle:
        elements.append(
            TextElement(
                role="subtitle",
                x=1.35,
                y=5.0,
                w=9.0,
                h=0.7,
                text=slide.subtitle,
                size_pt=16.0,
                color_role="muted",
            )
        )
    return tuple(elements)


def plan_content(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [
        _accent_side_bar(tokens),
        _kicker_block(slide, tokens)
        if slide.kicker
        else TextElement(role="kicker", x=MARGIN_IN, y=0.34, w=1, h=0.3, text="", size_pt=1),
        _title_block(slide, tokens),
    ]
    elements = [
        item
        for item in elements
        if not (item_text_empty(item) and isinstance(item, TextElement) and item.role == "kicker")
    ]
    body_top = CONTENT_TOP_IN + (0.12 if slide.kicker else 0.0)
    if slide.body:
        body_text = "\n".join(slide.body)
        elements.append(
            TextElement(
                role="body",
                x=MARGIN_IN,
                y=body_top,
                w=11.2,
                h=2.4,
                text=body_text,
                size_pt=15.0,
                color_role="ink",
            )
        )
        body_top += min(2.5, 0.42 * len(slide.body) + 0.2)
    if slide.bullets:
        elements.append(
            BulletsElement(
                x=MARGIN_IN + 0.05,
                y=body_top,
                w=11.1,
                h=CONTENT_BOTTOM_IN - body_top - (0.8 if slide.callout else 0.2),
                items=slide.bullets,
                size_pt=16.0,
            )
        )
    if slide.callout:
        bar, label = _callout_bar(6.05, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def item_text_empty(item: SlideElement) -> bool:
    if isinstance(item, TextElement):
        return not item.text
    return False


def plan_two_column(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    columns = slide.comparison or (
        _column_from("Overview", slide.bullets[:4]),
        _column_from("Details", slide.bullets[4:8]),
    )
    return _plan_columns(slide, tokens, index, total, footer, columns)


def _column_from(title: str, points: tuple[str, ...]) -> ComparisonColumn:
    return ComparisonColumn(title=title, points=points)


def _plan_columns(
    slide: SlideSpec,
    tokens: DesignTokens,
    index: int,
    total: int,
    footer: str,
    columns: tuple[ComparisonColumn, ...],
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    count = max(len(columns), 1)
    gap = 0.4
    panel_w = (SLIDE_WIDTH_IN - 2 * MARGIN_IN - gap * (count - 1)) / count
    x = MARGIN_IN
    panel_top = CONTENT_TOP_IN
    panel_h = CONTENT_BOTTOM_IN - panel_top - (0.8 if slide.callout else 0.0)
    for column in columns:
        elements.append(
            ShapeElement(
                kind="panel",
                x=x,
                y=panel_top,
                w=panel_w,
                h=panel_h,
                fill_role="surface",
                radius=0.1,
                shadow=True,
            )
        )
        elements.append(
            ShapeElement(
                kind="rect", x=x, y=panel_top, w=panel_w, h=0.09, fill_role="accent", radius=0.0
            )
        )
        elements.append(
            TextElement(
                role="subtitle",
                x=x + 0.28,
                y=panel_top + 0.24,
                w=panel_w - 0.56,
                h=0.5,
                text=column.title,
                size_pt=19.0,
                color_role="ink",
                bold=True,
            )
        )
        points = column.points
        if points:
            elements.append(
                BulletsElement(
                    x=x + 0.3,
                    y=panel_top + 0.95,
                    w=panel_w - 0.6,
                    h=panel_h - 1.15,
                    items=points,
                    size_pt=14.5,
                )
            )
        x += panel_w + gap
    if slide.callout:
        bar, label = _callout_bar(6.15, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def plan_comparison(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    columns = slide.comparison or (
        _column_from("Option A", ("Point one", "Point two")),
        _column_from("Option B", ("Point one", "Point two")),
    )
    return _plan_columns(slide, tokens, index, total, footer, columns)


def plan_metrics(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    metrics = slide.metrics[:6] or (_metric_from("Metric", "—"),)
    count = len(metrics)
    per_row = 3 if count > 2 else count
    rows = (count + per_row - 1) // per_row
    gap = 0.35
    card_w = (SLIDE_WIDTH_IN - 2 * MARGIN_IN - gap * (per_row - 1)) / per_row
    card_h = min(1.85, (CONTENT_BOTTOM_IN - CONTENT_TOP_IN - gap * (rows - 1)) / rows)
    for position, metric in enumerate(metrics):
        row, column_index = divmod(position, per_row)
        x = MARGIN_IN + column_index * (card_w + gap)
        y = CONTENT_TOP_IN + row * (card_h + gap)
        elements.append(
            ShapeElement(
                kind="panel",
                x=x,
                y=y,
                w=card_w,
                h=card_h,
                fill_role="surface",
                radius=0.12,
                shadow=True,
            )
        )
        elements.append(
            TextElement(
                role="metric_value",
                x=x + 0.25,
                y=y + 0.16,
                w=card_w - 0.5,
                h=0.78,
                text=metric.value,
                size_pt=30.0,
                color_role="accent",
                bold=True,
                font_role="display",
            )
        )
        elements.append(
            TextElement(
                role="metric_label",
                x=x + 0.25,
                y=y + 0.95,
                w=card_w - 0.5,
                h=0.4,
                text=metric.label,
                size_pt=13.5,
                color_role="ink",
                bold=True,
            )
        )
        if metric.detail:
            elements.append(
                TextElement(
                    role="body",
                    x=x + 0.25,
                    y=y + 1.32,
                    w=card_w - 0.5,
                    h=0.44,
                    text=metric.detail,
                    size_pt=11.0,
                    color_role="muted",
                )
            )
    if slide.callout:
        bar, label = _callout_bar(6.2, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def _metric_from(label: str, value: str) -> MetricItem:
    return MetricItem(label=label, value=value)


def plan_timeline(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    entries = slide.timeline
    if not entries:
        entries = (_entry_from("Now", slide.title or "Today"),)
    count = len(entries)
    line_y = 3.95
    start_x = MARGIN_IN + 0.35
    end_x = SLIDE_WIDTH_IN - MARGIN_IN - 0.35
    elements.append(
        ShapeElement(
            kind="rect",
            x=start_x,
            y=line_y,
            w=end_x - start_x,
            h=0.045,
            fill_role="border",
            radius=0.0,
        )
    )
    usable = end_x - start_x
    step = usable / max(count - 1, 1) if count > 1 else 0.0
    label_w = min(usable / max(count, 1) - 0.15, 3.4)
    for position, entry in enumerate(entries):
        cx = start_x + position * step if count > 1 else start_x + usable / 2
        above = position % 2 == 0
        node_y = line_y - 0.11
        elements.append(
            ShapeElement(kind="circle", x=cx - 0.13, y=node_y, w=0.26, h=0.26, fill_role="accent")
        )
        block_x = min(max(MARGIN_IN, cx - label_w / 2), SLIDE_WIDTH_IN - MARGIN_IN - label_w)
        block_y = line_y - 1.72 if above else line_y + 0.42
        elements.append(
            ShapeElement(
                kind="rect",
                x=block_x,
                y=block_y,
                w=label_w,
                h=1.52,
                fill_role="surface",
                radius=0.1,
                shadow=True,
            )
        )
        elements.append(
            TextElement(
                role="chip",
                x=block_x + 0.16,
                y=block_y + 0.12,
                w=label_w - 0.32,
                h=0.32,
                text=entry.label,
                size_pt=11.5,
                color_role="accent",
                bold=True,
            )
        )
        elements.append(
            TextElement(
                role="body",
                x=block_x + 0.16,
                y=block_y + 0.44,
                w=label_w - 0.32,
                h=0.98,
                text=entry.title + (f" — {entry.description}" if entry.description else ""),
                size_pt=12.0,
                color_role="ink",
            )
        )
    if slide.callout:
        bar, label = _callout_bar(6.35, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def _entry_from(label: str, title: str) -> TimelineEntry:
    return TimelineEntry(label=label, title=title)


def plan_process(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    steps = slide.process
    if not steps:
        steps = (_step_from(slide.title or "Step"),)
    count = len(steps)
    gap = 0.14
    step_w = (SLIDE_WIDTH_IN - 2 * MARGIN_IN - gap * (count - 1)) / max(count, 1)
    x = MARGIN_IN
    y = CONTENT_TOP_IN + 0.35
    for position, step in enumerate(steps):
        elements.append(
            ChevronElement(
                x=x, y=y, w=step_w, h=1.15, text=f"STEP {position + 1}", fill_role="accent"
            )
        )
        elements.append(
            TextElement(
                role="subtitle",
                x=x + 0.02,
                y=y + 1.35,
                w=step_w - 0.04,
                h=0.45,
                text=step.title,
                size_pt=14.5,
                color_role="ink",
                bold=True,
            )
        )
        elements.append(
            TextElement(
                role="body",
                x=x + 0.02,
                y=y + 1.82,
                w=step_w - 0.04,
                h=1.6,
                text=step.description,
                size_pt=11.5,
                color_role="muted",
            )
        )
        x += step_w + gap
    if slide.callout:
        bar, label = _callout_bar(6.25, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def _step_from(title: str) -> ProcessStep:
    return ProcessStep(title=title)


def plan_chart(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    if slide.chart is not None:
        elements.append(
            ShapeElement(
                kind="panel",
                x=MARGIN_IN,
                y=CONTENT_TOP_IN,
                w=8.6,
                h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN - (0.75 if slide.callout else 0.05),
                fill_role="surface",
                radius=0.1,
                shadow=True,
            )
        )
        elements.append(
            ChartElement(
                x=MARGIN_IN + 0.18,
                y=CONTENT_TOP_IN + 0.16,
                w=8.24,
                h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN - (1.05 if slide.callout else 0.35),
                chart=slide.chart,
            )
        )
        if slide.bullets:
            elements.append(
                BulletsElement(
                    x=9.55,
                    y=CONTENT_TOP_IN + 0.15,
                    w=3.1,
                    h=3.4,
                    items=slide.bullets,
                    size_pt=13.0,
                )
            )
    else:
        elements.append(
            BulletsElement(
                x=MARGIN_IN + 0.05,
                y=CONTENT_TOP_IN,
                w=11.1,
                h=3.6,
                items=slide.bullets or ("Chart data was not provided for this slide.",),
                size_pt=16.0,
            )
        )
    if slide.callout:
        bar, label = _callout_bar(6.2, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def plan_image_lead(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    if not slide.image_path:
        return plan_content(slide, tokens, index, total, footer)
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    image_w = 6.1
    elements.append(
        ShapeElement(
            kind="panel",
            x=SLIDE_WIDTH_IN - MARGIN_IN - image_w,
            y=CONTENT_TOP_IN - 0.15,
            w=image_w,
            h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN + 0.35,
            fill_role="surface",
            radius=0.12,
            shadow=True,
        )
    )
    elements.append(
        ImageElement(
            x=SLIDE_WIDTH_IN - MARGIN_IN - image_w + 0.18,
            y=CONTENT_TOP_IN + 0.03,
            w=image_w - 0.36,
            h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN - 0.35,
            path=slide.image_path,
        )
    )
    if slide.image_caption:
        elements.append(
            TextElement(
                role="body",
                x=SLIDE_WIDTH_IN - MARGIN_IN - image_w + 0.22,
                y=CONTENT_BOTTOM_IN - 0.28,
                w=image_w - 0.44,
                h=0.35,
                text=slide.image_caption,
                size_pt=10.5,
                color_role="muted",
                italic=True,
            )
        )
    body_top = CONTENT_TOP_IN
    if slide.body:
        elements.append(
            TextElement(
                role="body",
                x=MARGIN_IN,
                y=body_top,
                w=SLIDE_WIDTH_IN - 2 * MARGIN_IN - image_w - 0.5,
                h=1.6,
                text="\n".join(slide.body),
                size_pt=14.5,
                color_role="ink",
            )
        )
        body_top += 1.7
    if slide.bullets:
        elements.append(
            BulletsElement(
                x=MARGIN_IN + 0.05,
                y=body_top,
                w=SLIDE_WIDTH_IN - 2 * MARGIN_IN - image_w - 0.55,
                h=CONTENT_BOTTOM_IN - body_top - 0.2,
                items=slide.bullets,
                size_pt=14.5,
            )
        )
    return tuple(elements)


def plan_quote(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    quote_text = slide.quote or slide.body[0] if slide.body else slide.quote or ""
    elements: list[SlideElement] = [
        ShapeElement(
            kind="rect",
            x=0.0,
            y=0.0,
            w=SLIDE_WIDTH_IN,
            h=SLIDE_HEIGHT_IN,
            fill_role="background",
            radius=0.0,
        ),
        ShapeElement(kind="circle", x=1.05, y=1.05, w=1.15, h=1.15, fill_role="accent_soft"),
        TextElement(
            role="big_number",
            x=1.22,
            y=0.92,
            w=0.9,
            h=1.2,
            text="“",
            size_pt=64.0,
            color_role="accent",
            bold=True,
            font_role="display",
        ),
        TextElement(
            role="quote",
            x=1.7,
            y=2.45,
            w=9.9,
            h=2.9,
            text=quote_text,
            size_pt=26.0,
            color_role="ink",
            italic=True,
            font_role="display",
        ),
    ]
    if slide.attribution:
        elements.append(
            TextElement(
                role="attribution",
                x=1.75,
                y=5.6,
                w=8.0,
                h=0.45,
                text=f"— {slide.attribution}",
                size_pt=15.0,
                color_role="muted",
                bold=True,
            )
        )
    if slide.title:
        elements.append(
            TextElement(
                role="kicker",
                x=1.75,
                y=1.35,
                w=8.0,
                h=0.35,
                text=slide.title.upper(),
                size_pt=tokens.kicker_size_pt,
                color_role="accent",
                bold=True,
                spacing_pt=3.0,
            )
        )
    return tuple(elements)


def plan_case_study(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    elements.append(
        ShapeElement(
            kind="panel",
            x=MARGIN_IN,
            y=CONTENT_TOP_IN,
            w=7.6,
            h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN,
            fill_role="surface",
            radius=0.1,
            shadow=True,
        )
    )
    if slide.bullets:
        elements.append(
            BulletsElement(
                x=MARGIN_IN + 0.3,
                y=CONTENT_TOP_IN + 0.3,
                w=7.0,
                h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN - 0.6,
                items=slide.bullets,
                size_pt=14.5,
            )
        )
    metric_x = MARGIN_IN + 7.9
    metric_w = SLIDE_WIDTH_IN - MARGIN_IN - metric_x
    metric_y = CONTENT_TOP_IN
    for metric in slide.metrics[:3]:
        elements.append(
            ShapeElement(
                kind="panel",
                x=metric_x,
                y=metric_y,
                w=metric_w,
                h=1.32,
                fill_role="accent_soft",
                radius=0.12,
            )
        )
        elements.append(
            TextElement(
                role="metric_value",
                x=metric_x + 0.22,
                y=metric_y + 0.12,
                w=metric_w - 0.44,
                h=0.6,
                text=metric.value,
                size_pt=24.0,
                color_role="accent",
                bold=True,
                font_role="display",
            )
        )
        elements.append(
            TextElement(
                role="metric_label",
                x=metric_x + 0.22,
                y=metric_y + 0.75,
                w=metric_w - 0.44,
                h=0.45,
                text=metric.label,
                size_pt=12.0,
                color_role="ink",
            )
        )
        metric_y += 1.55
    if slide.callout:
        bar, label = _callout_bar(6.3, slide.callout, tokens)
        elements.extend((bar, label))
    return tuple(elements)


def plan_conclusion(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [
        ShapeElement(
            kind="rect",
            x=0.0,
            y=0.0,
            w=SLIDE_WIDTH_IN,
            h=SLIDE_HEIGHT_IN,
            fill_role="background",
            radius=0.0,
        ),
        ShapeElement(
            kind="rect",
            x=0.0,
            y=SLIDE_HEIGHT_IN - 1.5,
            w=SLIDE_WIDTH_IN,
            h=1.5,
            fill_role="accent_soft",
            radius=0.0,
        ),
        _kicker_block(slide, tokens)
        if slide.kicker
        else TextElement(role="kicker", x=MARGIN_IN, y=0.34, w=1, h=0.3, text="", size_pt=1),
        _title_block(slide, tokens, y=0.62, size=36.0),
    ]
    elements = [
        item
        for item in elements
        if not (isinstance(item, TextElement) and item.role == "kicker" and not item.text)
    ]
    if slide.bullets:
        elements.append(
            BulletsElement(
                x=MARGIN_IN + 0.05,
                y=2.0,
                w=11.1,
                h=3.3,
                items=slide.bullets,
                size_pt=16.5,
            )
        )
    if slide.callout:
        elements.append(
            TextElement(
                role="callout",
                x=MARGIN_IN,
                y=5.75,
                w=11.9,
                h=1.2,
                text=slide.callout,
                size_pt=16.0,
                color_role="accent",
                bold=True,
                font_role="display",
            )
        )
    return tuple(elements)


def plan_references(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer: str
) -> tuple[SlideElement, ...]:
    elements: list[SlideElement] = [_title_block(slide, tokens, y=0.55)]
    if slide.kicker:
        elements.insert(0, _kicker_block(slide, tokens))
    items = slide.references or slide.bullets or ("No references were supplied for this deck.",)
    elements.append(
        BulletsElement(
            x=MARGIN_IN + 0.05,
            y=CONTENT_TOP_IN,
            w=11.6,
            h=CONTENT_BOTTOM_IN - CONTENT_TOP_IN,
            items=items,
            size_pt=12.5,
            numbered=True,
            gap_pt=8.0,
        )
    )
    return tuple(elements)


PLANNERS = {
    "cover": plan_cover,
    "agenda": plan_agenda,
    "section_divider": plan_section_divider,
    "content": plan_content,
    "two_column": plan_two_column,
    "image_lead": plan_image_lead,
    "timeline": plan_timeline,
    "process": plan_process,
    "metrics": plan_metrics,
    "chart": plan_chart,
    "comparison": plan_comparison,
    "quote": plan_quote,
    "case_study": plan_case_study,
    "conclusion": plan_conclusion,
    "references": plan_references,
}


def plan_slide(
    slide: SlideSpec, tokens: DesignTokens, index: int, total: int, footer_text: str
) -> SlideLayoutModel:
    planner = PLANNERS.get(slide.layout, plan_content)
    elements = planner(slide, tokens, index, total, footer_text)
    footer, page_number = _footer_blocks(
        index, total, tokens, footer_text, is_dark(tokens.direction)
    )
    elements = (*elements, footer, page_number)
    return SlideLayoutModel(
        index=index,
        layout=slide.layout,
        direction=tokens.direction,
        background=tokens.background,
        elements=elements,
        footer_text=footer_text,
    )


def plan_presentation(spec: PresentationSpec) -> list[SlideLayoutModel]:
    tokens = tokens_for_direction(spec.direction)
    total = len(spec.slides)
    return [
        plan_slide(slide, tokens, position, total, spec.footer_text)
        for position, slide in enumerate(spec.slides, 1)
    ]


def layout_variety(spec: PresentationSpec) -> float:
    if not spec.slides:
        return 0.0
    distinct = len({slide.layout for slide in spec.slides})
    return round(distinct / len(spec.slides), 3)
