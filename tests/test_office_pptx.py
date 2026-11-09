from __future__ import annotations

from pathlib import Path

import pytest

from eurus.office.design.layouts import layout_variety, plan_presentation
from eurus.office.models.presentation import (
    ChartData,
    ChartSeries,
    ComparisonColumn,
    MetricItem,
    PresentationSpec,
    ProcessStep,
    SlideSpec,
    TimelineEntry,
)
from eurus.office.qa.structural import validate_structure
from eurus.office.renderers.html import build_preview
from eurus.office.renderers.powerpoint import render_presentation

pptx = pytest.importorskip("pptx")


def build_spec() -> PresentationSpec:
    return PresentationSpec(
        title="Energi Terbarukan",
        subtitle="Deck edukasi",
        author="Eurus",
        direction="educational",
        footer_text="Eurus Office",
        slides=(
            SlideSpec(
                layout="cover",
                title="Energi Terbarukan",
                subtitle="Untuk pelajar",
                kicker="edukasi",
            ),
            SlideSpec(layout="agenda", title="Agenda", bullets=("Konteks", "Teknologi", "Ekonomi")),
            SlideSpec(layout="section_divider", title="Teknologi", kicker="02"),
            SlideSpec(
                layout="content",
                title="Tiga keluarga energi",
                bullets=("Surya", "Angin", "Hidro"),
                callout="Setiap lokasi berbeda.",
            ),
            SlideSpec(
                layout="timeline",
                title="Milestone",
                timeline=(
                    TimelineEntry("1997", "Kyoto", "Target pertama"),
                    TimelineEntry("2015", "Paris", "Net-zero"),
                ),
            ),
            SlideSpec(
                layout="process",
                title="Membangun PLTS",
                process=(
                    ProcessStep("Survei", "Lokasi"),
                    ProcessStep("Izin", "AMDAL"),
                    ProcessStep("Bangun", "Panel"),
                ),
            ),
            SlideSpec(
                layout="metrics",
                title="Biaya turun",
                metrics=(
                    MetricItem("Solar LCOE", "-89%", "2010-2024"),
                    MetricItem("Wind", "-70%", "2010-2024"),
                    MetricItem("Baterai", "-93%", "2010-2024"),
                ),
            ),
            SlideSpec(
                layout="chart",
                title="Kapasitas",
                chart=ChartData(
                    kind="column",
                    title="GW",
                    categories=("2021", "2022", "2023"),
                    series=(
                        ChartSeries("Solar", (135.0, 192.0, 246.0)),
                        ChartSeries("Wind", (94.0, 101.0, 116.0)),
                    ),
                ),
                bullets=("Solar memimpin",),
            ),
            SlideSpec(
                layout="comparison",
                title="Atap vs PLTS",
                comparison=(
                    ComparisonColumn("Atap", ("Mandiri", "Tanpa lahan")),
                    ComparisonColumn("PLTS", ("Skala", "Buffer grid")),
                ),
            ),
            SlideSpec(
                layout="quote", title="Perspektif", quote="Pernyataan bijak.", attribution="Anonim"
            ),
            SlideSpec(
                layout="case_study",
                title="Studi kasus",
                bullets=("48 MW", "14.000 rumah"),
                metrics=(MetricItem("CO2", "31 kt", "per tahun"),),
            ),
            SlideSpec(
                layout="conclusion",
                title="Kesimpulan",
                bullets=("Biaya turun",),
                callout="Pilot 2027.",
            ),
            SlideSpec(
                layout="references", title="Referensi", references=("IEA 2025", "IRENA 2024")
            ),
        ),
    )


def test_layout_models_cover_all_slides_with_variety() -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    assert len(models) == len(spec.slides)
    assert layout_variety(spec) == 1.0
    assert all(model.elements for model in models)


def test_pptx_native_text_shapes_and_charts(tmp_path: Path) -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    path = render_presentation(spec, models, tmp_path / "deck.pptx")
    presentation = pptx.Presentation(str(path))
    assert len(presentation.slides) == 13
    assert presentation.slide_width == 12192000
    assert presentation.slide_height == 6858000
    charts = 0
    text_boxes = 0
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False):
                charts += 1
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                text_boxes += 1
    assert charts == 1
    assert text_boxes > 30


def test_pptx_text_is_editable_native_text(tmp_path: Path) -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    path = render_presentation(spec, models, tmp_path / "deck.pptx")
    presentation = pptx.Presentation(str(path))
    all_text = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                all_text.append(shape.text_frame.text)
    joined = "\n".join(all_text)
    assert "Energi Terbarukan" in joined
    assert "Solar" in joined or "Surya" in joined


def test_html_preview_matches_pptx_slide_count_and_title(tmp_path: Path) -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    render_presentation(spec, models, tmp_path / "deck.pptx")
    preview = build_preview(spec, models)
    preview_path = tmp_path / "deck.preview.html"
    preview_path.write_text(preview.document, encoding="utf-8")
    html_text = preview_path.read_text(encoding="utf-8")
    assert preview.slide_count == 13
    assert html_text.count('class="slide"') == 13
    assert "Energi Terbarukan" in html_text
    assert 'class="el text"' in html_text or "el text" in html_text


def test_pptx_structural_qa_passes(tmp_path: Path) -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    path = render_presentation(spec, models, tmp_path / "deck.pptx")
    defects, score = validate_structure("pptx", path, "job-test")
    critical = [d for d in defects if d.severity.value == "critical"]
    assert not critical
    assert score >= 95.0


def test_pptx_structural_qa_flags_out_of_bounds_shapes(tmp_path: Path) -> None:
    spec = build_spec()
    models = plan_presentation(spec)
    path = render_presentation(spec, models, tmp_path / "deck.pptx")
    presentation = pptx.Presentation(str(path))
    from pptx.util import Emu

    slide = presentation.slides[3]
    for shape in slide.shapes:
        if getattr(shape, "has_text_frame", False):
            shape.left = Emu(presentation.slide_width + 500_000)
            break
    presentation.save(str(path))
    defects, score = validate_structure("pptx", path, "job-test")
    assert any("out of bounds" in d.description for d in defects)
    assert score < 100.0


def test_pptx_rejects_deck_without_cover(tmp_path: Path) -> None:
    from eurus.office.errors import OfficeValidationError

    with pytest.raises(OfficeValidationError):
        PresentationSpec(title="X", slides=(SlideSpec(layout="content", title="A"),))
