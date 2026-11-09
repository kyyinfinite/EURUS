from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from eurus.office.errors import OfficeValidationError

MAX_SLIDES = 40
MAX_BULLETS = 8
MAX_METRICS = 6
MAX_TIMELINE_ITEMS = 8
MAX_PROCESS_STEPS = 6
MAX_COMPARISON_COLUMNS = 3

SLIDE_LAYOUTS = (
    "cover",
    "agenda",
    "section_divider",
    "content",
    "two_column",
    "image_lead",
    "timeline",
    "process",
    "metrics",
    "chart",
    "comparison",
    "quote",
    "case_study",
    "conclusion",
    "references",
)

DESIGN_DIRECTIONS = (
    "editorial",
    "corporate",
    "minimal",
    "educational",
    "scientific",
    "technical",
    "historical",
    "data_driven",
    "modern_keynote",
)


class ChartKind(StrEnum):
    BAR = "bar"
    COLUMN = "column"
    LINE = "line"
    PIE = "pie"


@dataclass(frozen=True, slots=True)
class MetricItem:
    label: str
    value: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    label: str
    title: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ProcessStep:
    title: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ComparisonColumn:
    title: str
    points: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise OfficeValidationError("comparison column title must not be empty")


@dataclass(frozen=True, slots=True)
class ChartSeries:
    name: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise OfficeValidationError("chart series name must not be empty")


@dataclass(frozen=True, slots=True)
class ChartData:
    kind: ChartKind
    title: str
    categories: tuple[str, ...]
    series: tuple[ChartSeries, ...]

    def __post_init__(self) -> None:
        if not self.categories:
            raise OfficeValidationError("chart needs at least one category")
        if not self.series:
            raise OfficeValidationError("chart needs at least one series")
        if len(self.categories) > 24:
            raise OfficeValidationError("charts are limited to 24 categories")
        for series in self.series:
            if len(series.values) != len(self.categories):
                raise OfficeValidationError("chart series length must match category count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "title": self.title,
            "categories": list(self.categories),
            "series": [
                {"name": series.name, "values": [float(value) for value in series.values]}
                for series in self.series
            ],
        }


@dataclass(frozen=True, slots=True)
class SlideSpec:
    layout: str
    title: str = ""
    subtitle: str = ""
    kicker: str = ""
    bullets: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    callout: str = ""
    metrics: tuple[MetricItem, ...] = ()
    timeline: tuple[TimelineEntry, ...] = ()
    process: tuple[ProcessStep, ...] = ()
    comparison: tuple[ComparisonColumn, ...] = ()
    chart: ChartData | None = None
    quote: str = ""
    attribution: str = ""
    image_path: str = ""
    image_caption: str = ""
    references: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.layout not in SLIDE_LAYOUTS:
            raise OfficeValidationError(f"slide layout must be one of: {', '.join(SLIDE_LAYOUTS)}")
        if len(self.bullets) > MAX_BULLETS:
            raise OfficeValidationError(f"slides are limited to {MAX_BULLETS} bullets")
        if len(self.metrics) > MAX_METRICS:
            raise OfficeValidationError(f"slides are limited to {MAX_METRICS} metrics")
        if len(self.timeline) > MAX_TIMELINE_ITEMS:
            raise OfficeValidationError(f"timelines are limited to {MAX_TIMELINE_ITEMS} items")
        if len(self.process) > MAX_PROCESS_STEPS:
            raise OfficeValidationError(f"process slides are limited to {MAX_PROCESS_STEPS} steps")
        if len(self.comparison) > MAX_COMPARISON_COLUMNS:
            raise OfficeValidationError(
                f"comparisons are limited to {MAX_COMPARISON_COLUMNS} columns"
            )


@dataclass(frozen=True, slots=True)
class PresentationSpec:
    title: str
    subtitle: str = ""
    author: str = "Eurus"
    date_text: str = ""
    direction: str = "editorial"
    slides: tuple[SlideSpec, ...] = ()
    footer_text: str = ""
    language: str = "en"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise OfficeValidationError("presentation title must not be empty")
        if self.direction not in DESIGN_DIRECTIONS:
            raise OfficeValidationError(
                f"design direction must be one of: {', '.join(DESIGN_DIRECTIONS)}"
            )
        if not self.slides:
            raise OfficeValidationError("presentation needs at least one slide")
        if len(self.slides) > MAX_SLIDES:
            raise OfficeValidationError(f"presentations are limited to {MAX_SLIDES} slides")
        if not isinstance(self.slides[0], SlideSpec) or self.slides[0].layout != "cover":
            raise OfficeValidationError("presentations must start with a cover slide")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.author,
            "date_text": self.date_text,
            "direction": self.direction,
            "footer_text": self.footer_text,
            "language": self.language,
            "slides": [slide_to_dict(slide) for slide in self.slides],
        }


def slide_to_dict(slide: SlideSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {"layout": slide.layout, "title": slide.title}
    optional_fields = (
        "subtitle",
        "kicker",
        "callout",
        "quote",
        "attribution",
        "image_path",
        "image_caption",
        "notes",
    )
    for name in optional_fields:
        value = getattr(slide, name)
        if value:
            payload[name] = value
    for name in ("bullets", "body", "references"):
        value = getattr(slide, name)
        if value:
            payload[name] = list(value)
    if slide.metrics:
        payload["metrics"] = [
            {"label": item.label, "value": item.value, "detail": item.detail}
            for item in slide.metrics
        ]
    if slide.timeline:
        payload["timeline"] = [
            {"label": item.label, "title": item.title, "description": item.description}
            for item in slide.timeline
        ]
    if slide.process:
        payload["process"] = [
            {"title": item.title, "description": item.description} for item in slide.process
        ]
    if slide.comparison:
        payload["comparison"] = [
            {"title": column.title, "points": list(column.points)} for column in slide.comparison
        ]
    if slide.chart is not None:
        payload["chart"] = slide.chart.to_dict()
    return payload
