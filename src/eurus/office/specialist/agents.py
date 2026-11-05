from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.office.errors import OfficeValidationError
from eurus.office.markdown import build_document_spec
from eurus.office.models.document import (
    BibliographyBlock,
    CalloutBlock,
    DocumentBlock,
    DocumentMetadata,
    DocumentSpec,
    HeaderFooterConfig,
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
from eurus.office.models.presentation import (
    ChartData,
    ChartKind,
    ChartSeries,
    ComparisonColumn,
    MetricItem,
    PresentationSpec,
    ProcessStep,
    SlideSpec,
    TimelineEntry,
)
from eurus.office.models.review import DefectReport
from eurus.office.models.task import (
    DesignSpecification,
    OfficeBrief,
    OfficeFormat,
    ResearchPackage,
)
from eurus.office.models.workbook import (
    ChartSpec,
    ConditionalSpec,
    FormulaColumn,
    KpiCard,
    NamedRangeSpec,
    SheetSpec,
    SheetTable,
    ValidationSpec,
    WorkbookSpec,
)

FENCE_PATTERN = re.compile(r"^```[a-zA-Z0-9]*\n?|```$")

SLIDE_LAYOUT_NAMES = frozenset(
    {
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
    }
)

SHEET_ROLE_NAMES = frozenset(
    {"overview", "data", "input", "constants", "calculations", "results", "dashboard", "summary"}
)

DOCUMENT_STYLE_NAMES = frozenset(
    {"academic", "editorial", "business", "technical", "research", "educational", "proposal"}
)


@dataclass(frozen=True, slots=True)
class ArtifactSpecification:
    format: OfficeFormat
    document: DocumentSpec | None = None
    presentation: PresentationSpec | None = None
    workbook: WorkbookSpec | None = None

    @property
    def spec(self) -> DocumentSpec | PresentationSpec | WorkbookSpec:
        if self.document is not None:
            return self.document
        if self.presentation is not None:
            return self.presentation
        if self.workbook is not None:
            return self.workbook
        raise OfficeValidationError("artifact specification is empty")


@dataclass(frozen=True, slots=True)
class AcceptanceOutcome:
    accepted: bool
    content_score: float
    reasons: tuple[str, ...]
    missing_content: tuple[str, ...]
    client_summary: str


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = FENCE_PATTERN.sub("", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise OfficeValidationError("model reply contains no JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise OfficeValidationError(f"model reply is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise OfficeValidationError("model reply JSON must be an object")
    return payload


def _str_tuple(values: Any, limit: int = 200) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(str(item).strip()[:1000] for item in values if str(item).strip())[:limit]


def parse_research(objective: str, payload: dict[str, Any]) -> ResearchPackage:
    return ResearchPackage.from_payload(objective, payload)


def parse_design(payload: dict[str, Any]) -> DesignSpecification:
    direction = str(payload.get("direction", "editorial")).strip().lower() or "editorial"
    document_style = str(payload.get("document_style", "business")).strip().lower() or "business"
    return DesignSpecification(
        direction=direction,
        document_style=document_style,
        guidance=_str_tuple(payload.get("guidance"), 20),
        palette_notes=str(payload.get("palette_notes", ""))[:1000],
        typography_notes=str(payload.get("typography_notes", ""))[:1000],
        layout_plan=_str_tuple(payload.get("layout_plan"), 60),
    )


def parse_client_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    requirements = _str_tuple(payload.get("requirements"), 24)
    if not requirements:
        raise OfficeValidationError("client analysis produced no requirements")
    return {
        "audience": str(payload.get("audience", ""))[:500],
        "tone": str(payload.get("tone", ""))[:500],
        "style": str(payload.get("style", ""))[:200],
        "requirements": requirements,
        "acceptance_criteria": _str_tuple(payload.get("acceptance_criteria"), 24),
        "research_needed": bool(payload.get("research_needed", False)),
        "research_questions": _str_tuple(payload.get("research_questions"), 12),
        "language": str(payload.get("language", "en"))[:12] or "en",
    }


def parse_acceptance(payload: dict[str, Any]) -> AcceptanceOutcome:
    score_raw = payload.get("content_score", 0)
    try:
        content_score = max(0.0, min(100.0, float(score_raw)))
    except (TypeError, ValueError):
        content_score = 0.0
    return AcceptanceOutcome(
        accepted=bool(payload.get("accepted", False)),
        content_score=content_score,
        reasons=_str_tuple(payload.get("reasons"), 20),
        missing_content=_str_tuple(payload.get("missing_content"), 20),
        client_summary=str(payload.get("client_summary", ""))[:2000],
    )


def parse_defects(payload: dict[str, Any]) -> tuple[tuple[DefectReport, ...], float | None, str]:
    raw_defects = payload.get("defects", [])
    defects: list[DefectReport] = []
    for index, entry in enumerate(raw_defects[:40]):
        if not isinstance(entry, dict):
            continue
        try:
            defects.append(DefectReport.from_payload(index, entry))
        except OfficeValidationError:
            continue
    score_raw = payload.get("content_score")
    content_score: float | None = None
    if isinstance(score_raw, int | float):
        content_score = max(0.0, min(100.0, float(score_raw)))
    summary = str(payload.get("summary", ""))[:2000]
    return tuple(defects), content_score, summary


def _resolve_image(path: str, image_roots: tuple[Path, ...]) -> Path | None:
    candidate = Path(path.strip())
    if candidate.is_absolute():
        resolved = candidate.resolve()
        return resolved if resolved.is_file() else None
    for root in image_roots:
        option = (root / candidate).resolve()
        if option.is_file():
            return option
    return None


def _build_blocks(raw_blocks: Any, image_roots: tuple[Path, ...]) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    if not isinstance(raw_blocks, list):
        return blocks
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        blocks.extend(_build_block(raw, image_roots))
        if len(blocks) >= 400:
            break
    return blocks


def _build_block(raw: dict[str, Any], image_roots: tuple[Path, ...]) -> list[DocumentBlock]:
    kind = str(raw.get("kind", "")).strip().lower()
    if kind == "heading":
        text = str(raw.get("text", "")).strip()
        if not text:
            return []
        try:
            level = max(1, min(4, int(raw.get("level", 1))))
        except (TypeError, ValueError):
            level = 1
        return [HeadingBlock(level=level, text=text[:300])]
    if kind == "paragraph":
        text = str(raw.get("text", "")).strip()
        if not text:
            return []
        return [ParagraphBlock(rich=RichText.from_inline(text[:4000]))]
    if kind == "list":
        list_items = _str_tuple(raw.get("items"), 40)
        ordered = bool(raw.get("ordered", False))
        return [ListItemBlock(text=item, ordered=ordered) for item in list_items]
    if kind == "table":
        headers = _str_tuple(raw.get("headers"), 12)
        rows_raw = raw.get("rows", [])
        if not headers or not isinstance(rows_raw, list):
            return []
        rows: list[tuple[str, ...]] = []
        for row in rows_raw[:500]:
            if not isinstance(row, list):
                continue
            cells = tuple(str(cell)[:600] for cell in row[: len(headers)])
            cells = cells + ("",) * (len(headers) - len(cells))
            rows.append(cells)
        if not rows:
            return []
        return [
            TableBlock(headers=headers, rows=tuple(rows), caption=str(raw.get("caption", ""))[:300])
        ]
    if kind == "image":
        path = str(raw.get("path", "")).strip()
        if not path:
            return []
        resolved = _resolve_image(path, image_roots)
        if resolved is None:
            return []
        try:
            width = max(2.0, min(17.0, float(raw.get("width_cm", 12.0))))
        except (TypeError, ValueError):
            width = 12.0
        return [
            ImageBlock(
                path=str(resolved), caption=str(raw.get("caption", ""))[:300], width_cm=width
            )
        ]
    if kind == "quote":
        text = str(raw.get("text", "")).strip()
        if not text:
            return []
        return [
            QuoteBlock(
                rich=RichText.from_text(text[:2000]),
                attribution=str(raw.get("attribution", ""))[:200],
            )
        ]
    if kind == "callout":
        title = str(raw.get("title", "")).strip()
        if not title:
            return []
        variant = str(raw.get("variant", "info")).strip().lower()
        if variant not in {"info", "warning", "success", "risk"}:
            variant = "info"
        return [
            CalloutBlock(title=title[:200], body=str(raw.get("body", ""))[:1200], variant=variant)
        ]
    if kind == "timeline":
        items_raw = raw.get("items", [])
        if not isinstance(items_raw, list):
            return []
        items: list[TimelineItem] = []
        for entry in items_raw[:24]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            items.append(
                TimelineItem(
                    label=str(entry.get("label", ""))[:40],
                    title=title[:200],
                    description=str(entry.get("description", ""))[:400],
                )
            )
        if not items:
            return []
        return [TimelineBlock(items=tuple(items))]
    if kind == "two_column":
        left = tuple(_build_blocks(raw.get("left"), image_roots)[:12])
        right = tuple(_build_blocks(raw.get("right"), image_roots)[:12])
        if not left and not right:
            return []
        return [TwoColumnBlock(left=left, right=right)]
    if kind == "page_break":
        return [PageBreakBlock()]
    if kind == "bibliography":
        entries = _str_tuple(raw.get("entries"), 40)
        if not entries:
            return []
        return [BibliographyBlock(entries=entries)]
    return []


def _build_document(
    brief: OfficeBrief, payload: dict[str, Any], image_roots: tuple[Path, ...]
) -> DocumentSpec:
    metadata_raw = payload.get("metadata")
    if not isinstance(metadata_raw, dict):
        metadata_raw = {}
    title = str(metadata_raw.get("title", "")).strip() or str(payload.get("title", "")).strip()
    if not title:
        raise OfficeValidationError("document specification needs a title")
    blocks = _build_blocks(payload.get("blocks"), image_roots)
    if not blocks:
        raise OfficeValidationError("document specification has no content blocks")
    style = str(payload.get("style", brief.style_hint or "business")).strip().lower()
    if style not in DOCUMENT_STYLE_NAMES:
        style = "business"
    header_raw = payload.get("header_footer")
    if not isinstance(header_raw, dict):
        header_raw = {}
    return DocumentSpec(
        metadata=DocumentMetadata(
            title=title,
            subtitle=str(metadata_raw.get("subtitle", ""))[:500],
            author=str(metadata_raw.get("author", "Eurus"))[:200] or "Eurus",
            subject=str(metadata_raw.get("subject", ""))[:500],
            keywords=_str_tuple(metadata_raw.get("keywords"), 12),
            date_text=str(metadata_raw.get("date_text", ""))[:60],
            language=brief.language,
        ),
        style=style,
        blocks=tuple(blocks),
        header_footer=HeaderFooterConfig(
            header_text=str(header_raw.get("header_text", ""))[:300],
            footer_text=str(header_raw.get("footer_text", ""))[:300],
            page_numbers=bool(header_raw.get("page_numbers", True)),
        ),
        include_toc=bool(payload.get("include_toc", brief.include_toc)),
    )


def _build_presentation(
    brief: OfficeBrief, payload: dict[str, Any], image_roots: tuple[Path, ...]
) -> PresentationSpec:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise OfficeValidationError("presentation specification needs a title")
    slides: list[SlideSpec] = []
    raw_slides = payload.get("slides", [])
    if not isinstance(raw_slides, list):
        raw_slides = []
    for raw_slide in raw_slides:
        if not isinstance(raw_slide, dict):
            continue
        try:
            slide = _build_slide(raw_slide, image_roots)
        except OfficeValidationError:
            continue
        if slide is not None:
            slides.append(slide)
        if len(slides) >= 40:
            break
    if not slides or slides[0].layout != "cover":
        raise OfficeValidationError("presentation must begin with a cover slide")
    direction = str(payload.get("direction", "editorial")).strip().lower()
    return PresentationSpec(
        title=title,
        subtitle=str(payload.get("subtitle", ""))[:500],
        author=str(payload.get("author", "Eurus"))[:200] or "Eurus",
        date_text=str(payload.get("date_text", ""))[:60],
        direction=direction
        if direction
        in {
            "editorial",
            "corporate",
            "minimal",
            "educational",
            "scientific",
            "technical",
            "historical",
            "data_driven",
            "modern_keynote",
        }
        else "editorial",
        slides=tuple(slides),
        footer_text=str(payload.get("footer_text", brief.audience))[:200],
        language=brief.language,
    )


def _build_slide(raw: dict[str, Any], image_roots: tuple[Path, ...]) -> SlideSpec | None:
    layout = str(raw.get("layout", "")).strip().lower()
    if layout not in SLIDE_LAYOUT_NAMES:
        return None
    metrics: list[MetricItem] = []
    for entry in _as_list(raw.get("metrics"))[:6]:
        label = str(entry.get("label", "")).strip()
        if label:
            metrics.append(
                MetricItem(
                    label=label[:120],
                    value=str(entry.get("value", "—"))[:60] or "—",
                    detail=str(entry.get("detail", ""))[:200],
                )
            )
    timeline: list[TimelineEntry] = []
    for entry in _as_list(raw.get("timeline"))[:8]:
        entry_title = str(entry.get("title", "")).strip()
        if entry_title:
            timeline.append(
                TimelineEntry(
                    label=str(entry.get("label", ""))[:40],
                    title=entry_title[:200],
                    description=str(entry.get("description", ""))[:300],
                )
            )
    process: list[ProcessStep] = []
    for entry in _as_list(raw.get("process"))[:6]:
        title = str(entry.get("title", "")).strip()
        if title:
            process.append(
                ProcessStep(title=title[:200], description=str(entry.get("description", ""))[:300])
            )
    comparison: list[ComparisonColumn] = []
    for entry in _as_list(raw.get("comparison"))[:3]:
        title = str(entry.get("title", "")).strip()
        if title:
            comparison.append(
                ComparisonColumn(title=title[:200], points=_str_tuple(entry.get("points"), 8))
            )
    chart = _build_chart(raw)
    image_path = ""
    image_caption = str(raw.get("image_caption", ""))[:200]
    if layout == "image_lead":
        candidate = str(raw.get("image_path", "")).strip()
        resolved = _resolve_image(candidate, image_roots) if candidate else None
        if resolved is not None:
            image_path = str(resolved)
        else:
            layout = "content"
    return SlideSpec(
        layout=layout,
        title=str(raw.get("title", ""))[:250],
        subtitle=str(raw.get("subtitle", ""))[:400],
        kicker=str(raw.get("kicker", ""))[:80],
        bullets=_str_tuple(raw.get("bullets"), 8),
        body=_str_tuple(raw.get("body"), 4),
        callout=str(raw.get("callout", ""))[:300],
        metrics=tuple(metrics),
        timeline=tuple(timeline),
        process=tuple(process),
        comparison=tuple(comparison),
        chart=chart,
        quote=str(raw.get("quote", ""))[:600],
        attribution=str(raw.get("attribution", ""))[:200],
        image_path=image_path,
        image_caption=image_caption,
        references=_str_tuple(raw.get("references"), 16),
        notes=str(raw.get("notes", ""))[:1000],
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _build_chart(raw: dict[str, Any]) -> ChartData | None:
    chart_raw = raw.get("chart")
    if not isinstance(chart_raw, dict):
        return None
    kind_raw = str(chart_raw.get("kind", "column")).strip().lower()
    kind = (
        ChartKind(kind_raw) if kind_raw in {item.value for item in ChartKind} else ChartKind.COLUMN
    )
    categories = _str_tuple(chart_raw.get("categories"), 24)
    if not categories:
        return None
    series: list[ChartSeries] = []
    for series_raw in _as_list(chart_raw.get("series"))[:4]:
        name = str(series_raw.get("name", "")).strip()
        values_raw = series_raw.get("values", [])
        if not name or not isinstance(values_raw, list):
            continue
        values: list[float] = []
        for value in values_raw[:24]:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values.append(0.0)
        if values:
            series.append(ChartSeries(name=name[:120], values=tuple(values)))
    if not series:
        return None
    return ChartData(
        kind=kind,
        title=str(chart_raw.get("title", raw.get("title", "")))[:200],
        categories=tuple(categories),
        series=tuple(series),
    )


def _build_workbook(payload: dict[str, Any]) -> WorkbookSpec:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise OfficeValidationError("workbook specification needs a title")
    sheets: list[SheetSpec] = []
    for raw_sheet in _as_list(payload.get("sheets")):
        try:
            sheet = _build_sheet(raw_sheet)
        except OfficeValidationError:
            continue
        if sheet is not None:
            sheets.append(sheet)
        if len(sheets) >= 20:
            break
    if not sheets:
        raise OfficeValidationError("workbook specification has no sheets")
    named_ranges: list[NamedRangeSpec] = []
    for entry in _as_list(payload.get("named_ranges"))[:20]:
        name = str(entry.get("name", "")).strip()
        reference = str(entry.get("reference", "")).strip()
        if not name or not reference:
            continue
        try:
            named_ranges.append(NamedRangeSpec(name=name, reference=reference))
        except OfficeValidationError:
            continue
    return WorkbookSpec(
        title=title,
        subject=str(payload.get("subject", ""))[:300],
        sheets=tuple(sheets),
        named_ranges=tuple(named_ranges),
        recalc_on_load=bool(payload.get("recalc_on_load", True)),
    )


def _build_sheet(raw: dict[str, Any]) -> SheetSpec | None:
    name = str(raw.get("name", "")).strip()
    role = str(raw.get("role", "data")).strip().lower()
    if not name or role not in SHEET_ROLE_NAMES:
        return None
    tables: list[SheetTable] = []
    for raw_table in _as_list(raw.get("tables"))[:8]:
        headers = _str_tuple(raw_table.get("headers"), 60)
        rows_raw = raw_table.get("rows", [])
        if not headers or not isinstance(rows_raw, list):
            continue
        rows: list[tuple[Any, ...]] = []
        for row in rows_raw[:10_000]:
            if not isinstance(row, list):
                continue
            cells = list(row[: len(headers)])
            cells = cells + [None] * (len(headers) - len(cells))
            normalized: list[Any] = []
            for cell in cells:
                if isinstance(cell, bool | int | float):
                    normalized.append(cell)
                else:
                    text = str(cell)
                    normalized.append(text if text else None)
            rows.append(tuple(normalized))
        tables.append(
            SheetTable(
                headers=headers,
                rows=tuple(rows),
                start_cell=str(raw_table.get("start_cell", "A1"))[:8],
                style=str(raw_table.get("style", "accent"))[:20],
                allow_formulas=bool(raw_table.get("allow_formulas", False)),
            )
        )
    formula_columns: list[FormulaColumn] = []
    for entry in _as_list(raw.get("formula_columns"))[:12]:
        try:
            formula_columns.append(
                FormulaColumn(
                    header=str(entry.get("header", ""))[:60],
                    pattern=str(entry.get("pattern", ""))[:200],
                    number_format=str(entry.get("number_format", ""))[:40],
                )
            )
        except OfficeValidationError:
            continue
    kpis: list[KpiCard] = []
    for entry in _as_list(raw.get("kpis"))[:8]:
        try:
            kpis.append(
                KpiCard(
                    label=str(entry.get("label", ""))[:80],
                    formula=str(entry.get("formula", ""))[:200],
                    number_format=str(entry.get("number_format", ""))[:40],
                    detail=str(entry.get("detail", ""))[:200],
                )
            )
        except OfficeValidationError:
            continue
    charts: list[ChartSpec] = []
    for entry in _as_list(raw.get("charts"))[:8]:
        series: list[tuple[str, str]] = []
        for series_raw in _as_list(entry.get("series"))[:4]:
            series_name = str(series_raw.get("name", "")).strip()
            series_ref = str(series_raw.get("ref", "")).strip()
            if series_name and series_ref:
                series.append((series_name[:80], series_ref[:120]))
        try:
            charts.append(
                ChartSpec(
                    kind=str(entry.get("kind", "column"))[:12],
                    title=str(entry.get("title", ""))[:150],
                    categories_ref=str(entry.get("categories_ref", ""))[:120],
                    series=tuple(series),
                    anchor=str(entry.get("anchor", "A1"))[:8],
                    width_cm=float(entry.get("width_cm", 16.0)),
                    height_cm=float(entry.get("height_cm", 9.0)),
                )
            )
        except (OfficeValidationError, TypeError, ValueError):
            continue
    conditional: list[ConditionalSpec] = []
    for entry in _as_list(raw.get("conditional"))[:10]:
        try:
            conditional.append(
                ConditionalSpec(
                    kind=str(entry.get("kind", "color_scale"))[:12],
                    range=str(entry.get("range", ""))[:40],
                    operator=str(entry.get("operator", ""))[:16],
                    formula=str(entry.get("formula", ""))[:80],
                )
            )
        except OfficeValidationError:
            continue
    validation: list[ValidationSpec] = []
    for entry in _as_list(raw.get("validation"))[:10]:
        try:
            validation.append(
                ValidationSpec(
                    kind=str(entry.get("kind", "list"))[:10],
                    range=str(entry.get("range", ""))[:40],
                    formula1=str(entry.get("formula1", ""))[:120],
                    formula2=str(entry.get("formula2", ""))[:120],
                )
            )
        except OfficeValidationError:
            continue
    column_formats_raw = raw.get("column_formats")
    column_formats: tuple[tuple[str, str], ...] = ()
    if isinstance(column_formats_raw, dict):
        column_formats = tuple(
            (str(key)[:4], str(value)[:40]) for key, value in list(column_formats_raw.items())[:30]
        )
    return SheetSpec(
        name=name[:31],
        role=role,
        tables=tuple(tables),
        formula_columns=tuple(formula_columns),
        kpis=tuple(kpis),
        charts=tuple(charts),
        conditional=tuple(conditional),
        validation=tuple(validation),
        column_formats=column_formats,
        freeze_panes=str(raw.get("freeze_panes", ""))[:8],
        auto_filter=bool(raw.get("auto_filter", False)),
        notes=_str_tuple(raw.get("notes"), 6),
        print_area=str(raw.get("print_area", ""))[:60],
    )


def parse_artifact(
    brief: OfficeBrief,
    payload: dict[str, Any],
    image_roots: tuple[Path, ...] = (),
) -> ArtifactSpecification:
    kind = str(payload.get("kind", "")).strip().lower()
    expected_kinds = {
        OfficeFormat.DOCX: "document",
        OfficeFormat.PPTX: "presentation",
        OfficeFormat.XLSX: "workbook",
    }
    expected = expected_kinds[brief.format]
    if kind and kind != expected:
        raise OfficeValidationError(f"expected artifact kind {expected!r} but received {kind!r}")
    if brief.format is OfficeFormat.DOCX:
        return ArtifactSpecification(
            format=OfficeFormat.DOCX, document=_build_document(brief, payload, image_roots)
        )
    if brief.format is OfficeFormat.PPTX:
        return ArtifactSpecification(
            format=OfficeFormat.PPTX, presentation=_build_presentation(brief, payload, image_roots)
        )
    return ArtifactSpecification(format=OfficeFormat.XLSX, workbook=_build_workbook(payload))


def markdown_to_spec(
    brief: OfficeBrief,
    markdown_text: str,
    title: str | None = None,
    bibliography: tuple[str, ...] = (),
) -> ArtifactSpecification:
    style = brief.style_hint if brief.style_hint in DOCUMENT_STYLE_NAMES else "business"
    resolved_title, blocks = build_document_spec(
        markdown_text, style=style, include_toc=brief.include_toc, bibliography_entries=bibliography
    )
    spec = DocumentSpec(
        metadata=DocumentMetadata(
            title=title or resolved_title,
            author="Eurus",
            language=brief.language,
        ),
        style=style,
        blocks=blocks,
        include_toc=brief.include_toc,
    )
    return ArtifactSpecification(format=OfficeFormat.DOCX, document=spec)
