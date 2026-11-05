from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.models import Message
from eurus.office.errors import OfficeCapabilityError
from eurus.office.models.review import DefectReport
from eurus.office.models.task import AgentRole, DefectSeverity, ResolutionStatus
from eurus.office.renderers.base import convert_to_pdf, pdf_to_images
from eurus.providers.base import ChatResponse
from eurus.providers.capabilities import capabilities_for_protocol
from eurus.providers.router import Router


@dataclass(frozen=True, slots=True)
class VisualInspection:
    mode: str
    page_count: int
    defects: tuple[DefectReport, ...]
    score: float | None
    notes: tuple[str, ...]
    image_paths: tuple[Path, ...] = ()


def _counter(job_id: str) -> Any:
    state = {"n": 0}

    def make(
        severity: DefectSeverity, location: str, description: str, evidence: str, fix: str
    ) -> DefectReport:
        state["n"] += 1
        return DefectReport(
            defect_id=f"vis-{job_id[:8]}-{state['n']:03d}",
            severity=severity,
            artifact_location=location,
            description=description,
            evidence=evidence[:800],
            responsible_agent=AgentRole.VISUAL_QA,
            recommended_fix=fix[:800],
            resolution_status=ResolutionStatus.OPEN,
            layer="visual",
        )

    return make


async def render_artifact_images(
    path: Path, renders_dir: Path, *, dpi: int = 90, limit: int = 40
) -> list[Path]:
    pdf_path = await convert_to_pdf(path, renders_dir)
    return await pdf_to_images(pdf_path, renders_dir / "pages", dpi=dpi, limit=limit)


def _page_metrics(image_path: Path) -> dict[str, float]:
    from PIL import Image, ImageStat

    with Image.open(image_path) as image:
        grayscale = image.convert("L")
        stat = ImageStat.Stat(grayscale)
        width, height = grayscale.size
        samples = list(grayscale.getdata())
        step = max(1, len(samples) // 120_000)
        sampled = samples[::step]
        dark = sum(1 for value in sampled if value < 200)
        ink_ratio = dark / max(len(sampled), 1)
        edge = 4
        busy_bands = 0
        band_total = 0
        for band_index in range(3):
            box = (
                (0, 0, width, edge)
                if band_index == 0
                else (
                    (0, height - edge, width, height) if band_index == 1 else (0, 0, edge, height)
                )
            )
            region = grayscale.crop(box)
            region_stat = ImageStat.Stat(region)
            region_stddev = float(region_stat.stddev[0])
            band_total += 1
            if region_stddev > 28.0:
                busy_bands += 1
        return {
            "mean": round(float(stat.mean[0]), 2),
            "stddev": round(float(stat.stddev[0]), 2),
            "ink_ratio": round(ink_ratio, 4),
            "edge_ink_bands": busy_bands,
            "edge_bands_total": band_total,
            "width": width,
            "height": height,
        }


def _deterministic_defects(
    job_id: str,
    fmt: str,
    images: list[Path],
    metrics: list[dict[str, float]],
) -> tuple[DefectReport, ...]:
    make = _counter(job_id)
    defects: list[DefectReport] = []
    if not images:
        return tuple(defects)
    expected_ink = {"docx": 0.015, "pptx": 0.02, "xlsx": 0.005}.get(fmt, 0.01)
    sparse_threshold = expected_ink / 3
    blank_threshold = expected_ink / 10
    for index, (image, page) in enumerate(zip(images, metrics, strict=True), 1):
        location = f"{Path(image).name}"
        if page["ink_ratio"] < blank_threshold:
            defects.append(
                make(
                    DefectSeverity.MAJOR,
                    location,
                    f"page {index} appears essentially empty (ink ratio {page['ink_ratio']:.4f})",
                    f"grayscale mean {page['mean']}, stddev {page['stddev']}",
                    "remove empty pages or add the missing content",
                )
            )
        elif page["ink_ratio"] < sparse_threshold:
            defects.append(
                make(
                    DefectSeverity.MINOR,
                    location,
                    f"page {index} is unusually sparse (ink ratio {page['ink_ratio']:.4f})",
                    f"grayscale mean {page['mean']}, stddev {page['stddev']}",
                    "check whether planned content is missing from this page",
                )
            )
        if page["edge_ink_bands"] >= 2:
            defects.append(
                make(
                    DefectSeverity.MAJOR,
                    location,
                    f"content on page {index} reaches the page edges on "
                    f"{page['edge_ink_bands']} sides and may be clipped",
                    f"edge ink bands {page['edge_ink_bands']}/{page['edge_bands_total']}",
                    "increase margins or move the content inside the safe area",
                )
            )
    if fmt == "pptx" and len(images) > 1:
        sizes = {(page["width"], page["height"]) for page in metrics}
        if len(sizes) > 1:
            defects.append(
                make(
                    DefectSeverity.MINOR,
                    "page images",
                    f"slide pages render with inconsistent pixel sizes: {sizes}",
                    "rendered page geometry",
                    "keep a single slide size for the whole deck",
                )
            )
    return tuple(defects)


def vision_supported(router: Router | None) -> bool:
    if router is None:
        return False
    try:
        chain = list(getattr(router, "chain", ()) or ())
        providers = getattr(router, "providers", {}) or {}
        for ref in chain:
            provider = providers.get(getattr(ref, "provider", ""))
            protocol = getattr(getattr(provider, "spec", None), "protocol", "")
            capabilities = capabilities_for_protocol(str(protocol))
            if capabilities.get("imageInput"):
                return True
    except Exception:
        return False
    return False


async def _model_inspection(
    router: Router,
    images: list[Path],
    *,
    artifact_name: str,
) -> tuple[tuple[DefectReport, ...], float]:
    make = _counter("model")
    defects: list[DefectReport] = []
    note_lines: list[str] = []
    scored: list[float] = []
    import base64

    for index, image in enumerate(images, 1):
        payload = base64.b64encode(image.read_bytes()).decode("ascii")
        response: ChatResponse = await router.chat(
            [
                Message.system(
                    "You are a visual quality inspector for rendered office documents. "
                    'Reply with STRICT JSON only: {"score": number, "defects": [{'
                    '"severity": "critical|major|minor|info", "description": "...", '
                    '"recommended_fix": "..."}]}. '
                    "Score the visual quality from 0 to 100. Inspect for text overflow, "
                    "clipping, overlap, crowding, and unreadable content. "
                    "Do not invent defects; if the page looks clean, say so with a high score."
                ),
                Message.user(
                    f"Inspect page {index} of {artifact_name}. "
                    "Rendered page data follows as base64 JPEG "
                    "(the configured transport may deliver it as text; "
                    "if you cannot see an image, set score to -1).\n\n"
                    f"{payload}"
                ),
            ],
            temperature=0.0,
            max_tokens=700,
        )
        text = response.message.content.strip()
        parsed = _parse_vision_reply(text)
        if parsed is None:
            note_lines.append(f"page {index}: model returned no parsable JSON")
            continue
        score, page_defects = parsed
        if score < 0:
            note_lines.append(f"page {index}: model could not read the image payload")
            continue
        scored.append(score)
        for page_defect in page_defects:
            severity_raw = str(page_defect.get("severity", "minor")).lower()
            severity = (
                DefectSeverity(severity_raw)
                if severity_raw in DefectSeverity.__members__
                else DefectSeverity.MINOR
            )
            defects.append(
                make(
                    severity,
                    f"page {index}",
                    str(page_defect.get("description", ""))[:1000],
                    "vision model inspection",
                    str(page_defect.get("recommended_fix", ""))[:800],
                )
            )
    average = round(sum(scored) / len(scored), 1) if scored else None
    return tuple(defects), average if average is not None else 0.0


def _parse_vision_reply(text: str) -> tuple[float, list[dict[str, Any]]] | None:
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    score = payload.get("score")
    if not isinstance(score, (int, float)):
        return None
    defects = payload.get("defects", [])
    return float(score), [item for item in defects if isinstance(item, dict)]


async def inspect_visual(
    fmt: str,
    artifact_path: Path,
    renders_dir: Path,
    job_id: str,
    *,
    router: Router | None = None,
    use_vision: bool = True,
    pdf_path: Path | None = None,
) -> VisualInspection:
    try:
        if pdf_path is not None and pdf_path.is_file():
            images = await pdf_to_images(pdf_path, renders_dir / "pages")
        else:
            images = await render_artifact_images(artifact_path, renders_dir)
    except (RuntimeError, OfficeCapabilityError) as exc:
        return VisualInspection(
            mode="unavailable",
            page_count=0,
            defects=(),
            score=None,
            notes=(f"visual rendering unavailable: {exc}",),
        )
    metrics = [_page_metrics(image) for image in images]
    defects = _deterministic_defects(job_id, fmt, images, metrics)
    notes: list[str] = []
    mode = "deterministic"
    score = _score_from_defects(defects, page_count=len(images))
    vision_usable = use_vision and vision_supported(router)
    if vision_usable and router is not None:
        model_defects, model_score = await _model_inspection(
            router, images, artifact_name=artifact_path.name
        )
        if model_score > 0:
            mode = "model"
            base_score = score if score is not None else model_score
            score = round((base_score + model_score) / 2, 1)
            defects = (*defects, *model_defects)
        else:
            notes.append(
                "vision model could not read rendered images; deterministic inspection only"
            )
    else:
        notes.append("no vision-capable model is configured; deterministic layout inspection only")
    return VisualInspection(
        mode=mode,
        page_count=len(images),
        defects=defects,
        score=score,
        notes=tuple(notes),
        image_paths=tuple(images),
    )


def _score_from_defects(defects: tuple[DefectReport, ...], *, page_count: int) -> float | None:
    if page_count == 0:
        return None
    penalty = 0.0
    for defect in defects:
        if defect.severity is DefectSeverity.CRITICAL:
            penalty += 25.0
        elif defect.severity is DefectSeverity.MAJOR:
            penalty += 10.0
        elif defect.severity is DefectSeverity.MINOR:
            penalty += 4.0
        else:
            penalty += 1.0
    return round(max(0.0, 100.0 - penalty), 1)
