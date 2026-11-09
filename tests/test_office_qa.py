from __future__ import annotations

from pathlib import Path

import pytest

from eurus.office.models.document import (
    DocumentMetadata,
    DocumentSpec,
    HeadingBlock,
    ParagraphBlock,
    RichText,
)
from eurus.office.models.review import QAReport
from eurus.office.models.task import DefectSeverity
from eurus.office.qa.scoring import (
    AcceptanceReport,
    AcceptanceThresholds,
    evaluate_acceptance,
    merge_defects,
)
from eurus.office.qa.visual import _deterministic_defects, _page_metrics, vision_supported
from eurus.office.renderers.base import detect_capabilities
from eurus.office.renderers.word import render_document

PIL = pytest.importorskip("PIL")


def make_image(path: Path, size: tuple[int, int], color, painter=None) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, color)
    if painter is not None:
        draw = ImageDraw.Draw(image)
        painter(draw, size)
    image.save(path)
    return path


def test_page_metrics_measures_ink_and_edges(tmp_path: Path) -> None:
    def paint(draw, size):
        width, height = size
        draw.rectangle([0, 0, width, 40], fill=(30, 30, 30))

    busy = make_image(tmp_path / "busy.jpg", (400, 300), (250, 250, 250), paint)
    metrics = _page_metrics(busy)
    assert metrics["width"] == 400
    assert 0.0 <= metrics["ink_ratio"] <= 1.0
    assert metrics["edge_ink_bands"] >= 0


def test_deterministic_defects_flag_near_empty_pages(tmp_path: Path) -> None:
    blank = make_image(tmp_path / "blank.jpg", (400, 300), (255, 255, 255))
    metrics = [_page_metrics(blank)]
    defects = _deterministic_defects("jobtest", "pptx", [blank], metrics)
    assert any("essentially empty" in d.description for d in defects)


def test_deterministic_defects_ignore_uniform_design_bands(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 300), (247, 245, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 400, 6], fill=(154, 52, 18))
    draw.rectangle([0, 294, 400, 300], fill=(154, 52, 18))
    path = tmp_path / "cover.jpg"
    image.save(path)
    metrics = [_page_metrics(path)]
    defects = _deterministic_defects("jobtest", "pptx", [path], metrics)
    edge_defects = [d for d in defects if "edges" in d.description]
    assert not edge_defects


def test_deterministic_defects_flag_busy_edge_content(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 300), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for x in range(0, 400, 8):
        color = (20, 40, 90) if (x // 8) % 2 else (240, 200, 60)
        draw.rectangle([x, 0, x + 3, 300], fill=color)
    path = tmp_path / "busy.jpg"
    image.save(path)
    metrics = [_page_metrics(path)]
    defects = _deterministic_defects("jobtest", "docx", [path], metrics)
    assert any("edges" in d.description for d in defects)


def test_vision_supported_false_for_text_only_protocols() -> None:
    class FakeSpec:
        protocol = "openai"

    class FakeProvider:
        spec = FakeSpec()

    class FakeRef:
        provider = "openai"

    class FakeRouter:
        chain = (FakeRef(),)
        providers = {"openai": FakeProvider()}

    assert vision_supported(FakeRouter()) is False
    assert vision_supported(None) is False


def test_acceptance_policy_thresholds_and_auto_reject() -> None:
    from eurus.office.models.review import DefectReport
    from eurus.office.models.task import ResolutionStatus

    qa = QAReport(
        job_id="j",
        task_id="t",
        structural_score=97.0,
        visual_score=90.0,
        content_score=88.0,
        visual_mode="deterministic",
    )
    acceptance = AcceptanceReport(job_id="j", task_id="t", accepted=True, content_score=88.0)
    decision = evaluate_acceptance(qa, acceptance, AcceptanceThresholds())
    assert decision.accepted
    assert decision.quality_status == "passed"

    clipped = DefectReport(
        defect_id="d1",
        severity=DefectSeverity.CRITICAL,
        artifact_location="page 1",
        description="text clipping detected on the title",
        layer="visual",
    )
    qa_bad = QAReport(
        job_id="j",
        task_id="t",
        structural_score=97.0,
        visual_score=90.0,
        content_score=88.0,
        defects=(clipped,),
    )
    decision_bad = evaluate_acceptance(qa_bad, acceptance, AcceptanceThresholds())
    assert not decision_bad.accepted
    assert decision_bad.quality_status == "rejected"
    assert any("auto-reject" in reason for reason in decision_bad.reasons)

    resolved = clipped.with_status(ResolutionStatus.VERIFIED)
    qa_fixed = QAReport(
        job_id="j",
        task_id="t",
        structural_score=97.0,
        visual_score=90.0,
        content_score=88.0,
        defects=(resolved,),
    )
    decision_fixed = evaluate_acceptance(qa_fixed, acceptance, AcceptanceThresholds())
    assert decision_fixed.accepted


def test_acceptance_requires_visual_inspection_note() -> None:
    qa = QAReport(
        job_id="j",
        task_id="t",
        structural_score=97.0,
        visual_score=None,
        content_score=90.0,
        visual_mode="unavailable",
    )
    acceptance = AcceptanceReport(job_id="j", task_id="t", accepted=True, content_score=90.0)
    decision = evaluate_acceptance(qa, acceptance, AcceptanceThresholds())
    assert decision.accepted
    assert decision.quality_status == "passed_with_warnings"
    assert any("visual" in warning for warning in decision.warnings)


def test_merge_defects_deduplicates() -> None:
    from eurus.office.models.review import DefectReport

    first = DefectReport(
        defect_id="a",
        severity=DefectSeverity.MAJOR,
        artifact_location="page 1",
        description="overlap detected",
        layer="visual",
    )
    second = DefectReport(
        defect_id="b",
        severity=DefectSeverity.MAJOR,
        artifact_location="page 1",
        description="overlap detected",
        layer="visual",
    )
    third = DefectReport(
        defect_id="c",
        severity=DefectSeverity.MINOR,
        artifact_location="page 1",
        description="different issue",
        layer="structural",
    )
    merged = merge_defects((first, second), (third,))
    assert len(merged) == 2


@pytest.mark.skipif(
    not detect_capabilities().pdf_to_images,
    reason="LibreOffice (soffice) and pdftoppm are required to render document pages",
)
def test_visual_pipeline_renders_real_docx_pages(tmp_path: Path) -> None:
    import asyncio

    from eurus.office.qa.visual import inspect_visual

    spec = DocumentSpec(
        metadata=DocumentMetadata(title="Visual QA Fixture"),
        blocks=(
            HeadingBlock(1, "One"),
            ParagraphBlock(RichText.from_text("Paragraph one with enough text to render a page.")),
            HeadingBlock(2, "Two"),
            ParagraphBlock(RichText.from_text("Paragraph two with more body text.")),
        ),
    )
    artifact = render_document(spec, tmp_path / "fixture.docx")
    inspection = asyncio.run(inspect_visual("docx", artifact, tmp_path / "renders", "jobtest"))
    assert inspection.mode in {"deterministic", "model"}
    assert inspection.page_count >= 1
    assert inspection.score is not None and inspection.score > 0
    assert all(image.is_file() for image in inspection.image_paths)
