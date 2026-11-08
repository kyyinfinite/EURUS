from __future__ import annotations

import contextlib
import re
from importlib import import_module
from pathlib import Path
from typing import Any

from eurus.office.errors import OfficeValidationError
from eurus.office.models.review import DefectReport
from eurus.office.models.task import AgentRole, DefectSeverity, ResolutionStatus

MD_TABLE_PATTERN = re.compile(r"(\|.+\|)")
MD_BOLD_PATTERN = re.compile(r"\*\*[^*]+\*\*")
MD_HEADING_PATTERN = re.compile(r"(^|\n)#{1,4}\s+\S")
MD_LIST_PATTERN = re.compile(r"(^|\n)\s*[-*+]\s+\S")
MD_DIVIDER_PATTERN = re.compile(r"\|?\s*-{3,}\s*\|")
UNICODE_REPLACEMENT = "\ufffd"
_SHEET_REF_PATTERN = re.compile(
    r"(?:'([^']+)'!|([A-Za-z_][A-Za-z0-9_]*)!)?\$?[A-Z]{1,3}\$?[0-9]{1,7}"
)


def _counter(job_id: str) -> Any:
    state = {"n": 0}

    def make(
        severity: DefectSeverity,
        location: str,
        description: str,
        evidence: str,
        fix: str,
        agent: AgentRole,
    ) -> DefectReport:
        state["n"] += 1
        return DefectReport(
            defect_id=f"str-{job_id[:8]}-{state['n']:03d}",
            severity=severity,
            artifact_location=location,
            description=description,
            evidence=evidence[:800],
            responsible_agent=agent,
            recommended_fix=fix[:800],
            resolution_status=ResolutionStatus.OPEN,
            layer="structural",
        )

    return make


def _docx_problems(path: Path) -> list[str]:
    try:
        Document = import_module("docx").Document
    except ImportError as exc:
        raise OfficeValidationError("python-docx is required for DOCX structural QA") from exc
    document = Document(str(path))
    problems: list[str] = []
    headings: list[int] = []
    for paragraph in document.paragraphs:
        style_name = (paragraph.style.name or "").lower()
        if style_name.startswith("heading"):
            with contextlib.suppress(ValueError, IndexError):
                headings.append(int(style_name.split()[-1]))
        text = paragraph.text
        if not text:
            continue
        if MD_BOLD_PATTERN.search(text) or MD_HEADING_PATTERN.search(text):
            problems.append(f"markdown leakage in paragraph: {text[:120]}")
    for level_index in range(1, len(headings)):
        if headings[level_index] - headings[level_index - 1] > 1:
            problems.append(
                f"heading hierarchy skips from h{headings[level_index - 1]} "
                f"to h{headings[level_index]}"
            )
            break
    if document.paragraphs and not any(paragraph.text.strip() for paragraph in document.paragraphs):
        problems.append("document body has no text content")
    return problems


def _docx_table_stats(path: Path) -> dict[str, Any]:
    Document = import_module("docx").Document
    document = Document(str(path))
    table_count = len(document.tables)
    header_rows = 0
    for table in document.tables:
        if not table.rows:
            continue
        first_row_cells = [cell.text.strip() for cell in table.rows[0].cells]
        if any(first_row_cells):
            header_rows += 1
    return {"tables": table_count, "header_rows": header_rows}


def _pptx_problems(path: Path) -> list[str]:
    try:
        pptx_module = import_module("pptx")
    except ImportError as exc:
        raise OfficeValidationError("python-pptx is required for PPTX structural QA") from exc
    presentation = pptx_module.Presentation(str(path))
    problems: list[str] = []
    slide_count = len(presentation.slides)
    if slide_count == 0:
        problems.append("presentation has no slides")
        return problems
    width = presentation.slide_width
    height = presentation.slide_height
    ratio = width / height if height else 0
    if not 1.6 <= ratio <= 1.9:
        problems.append(f"unexpected slide aspect ratio: {ratio:.2f}")
    for index, slide in enumerate(presentation.slides, 1):
        text_parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text_parts.append(shape.text_frame.text)
                if MD_BOLD_PATTERN.search(shape.text_frame.text) or MD_HEADING_PATTERN.search(
                    shape.text_frame.text
                ):
                    problems.append(f"markdown leakage on slide {index}")
            try:
                left = shape.left or 0
                top = shape.top or 0
                right = left + (shape.width or 0)
                bottom = top + (shape.height or 0)
                if (
                    left < -10_000
                    or top < -10_000
                    or right > width + 10_000
                    or bottom > height + 10_000
                ):
                    problems.append(
                        f"shape out of bounds on slide {index}: {getattr(shape, 'name', 'shape')}"
                    )
            except (TypeError, AttributeError):
                continue
        if not any(part.strip() for part in text_parts):
            problems.append(f"slide {index} has no text content")
    return problems


def _pptx_stats(path: Path) -> dict[str, Any]:
    pptx_module = import_module("pptx")
    presentation = pptx_module.Presentation(str(path))
    charts = 0
    pictures = 0
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False):
                charts += 1
            if shape.shape_type == 13:
                pictures += 1
    return {
        "slides": len(presentation.slides),
        "charts": charts,
        "pictures": pictures,
        "width_emu": presentation.slide_width,
        "height_emu": presentation.slide_height,
    }


def _xlsx_problems(path: Path) -> list[str]:
    try:
        openpyxl = import_module("openpyxl")
    except ImportError as exc:
        raise OfficeValidationError("openpyxl is required for XLSX structural QA") from exc
    problems: list[str] = []
    workbook = openpyxl.load_workbook(path, data_only=False)
    try:
        if not workbook.sheetnames:
            problems.append("workbook has no sheets")
        sheet_names = list(workbook.sheetnames)
        upper_names = {name.upper() for name in sheet_names}
        for sheet_name in sheet_names:
            worksheet = workbook[sheet_name]
            for row in worksheet.iter_rows():
                for cell in row:
                    value = cell.value
                    if not isinstance(value, str) or not value:
                        continue
                    if UNICODE_REPLACEMENT in value:
                        problems.append(f"replacement character in {sheet_name}!{cell.coordinate}")
                    if not value.startswith("=") or len(value) < 2:
                        continue
                    for ref in _SHEET_REF_PATTERN.findall(value):
                        sheet_ref = ref[0] or ref[1]
                        if not sheet_ref:
                            continue
                        if sheet_ref in sheet_names or sheet_ref.upper() in upper_names:
                            continue
                        problems.append(
                            f"formula on {sheet_name}!{cell.coordinate} "
                            f"references unknown sheet {sheet_ref}"
                        )
        for name in workbook.defined_names:
            with contextlib.suppress(KeyError, TypeError):
                definition = str(workbook.defined_names[name].attr_text)
                match = re.match(r"^(?:'([^']+)'|([A-Za-z_][A-Za-z0-9 ]{0,30}))!?", definition)
                if not match:
                    continue
                sheet_ref = match.group(1) or match.group(2)
                if sheet_ref and sheet_ref not in sheet_names:
                    problems.append(f"named range {name} references unknown sheet {sheet_ref}")
    finally:
        workbook.close()
    return problems


def _xlsx_stats(path: Path) -> dict[str, Any]:
    openpyxl = import_module("openpyxl")
    workbook = openpyxl.load_workbook(path, data_only=False)
    try:
        charts = 0
        conditional = 0
        validations = 0
        for sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
            charts += len(getattr(worksheet, "_charts", []))
            conditional += len(list(getattr(worksheet, "conditional_formatting", ())))
            validations += len(getattr(worksheet.data_validations, "dataValidation", []))
        return {
            "sheets": len(workbook.sheetnames),
            "charts": charts,
            "conditional": conditional,
            "validations": validations,
            "named_ranges": len(list(workbook.defined_names)),
            "full_calc_on_load": bool(workbook.calculation.fullCalcOnLoad)
            if workbook.calculation is not None
            else False,
        }
    finally:
        workbook.close()


def validate_structure(fmt: str, path: Path, job_id: str) -> tuple[list[DefectReport], float]:
    make = _counter(job_id)
    if fmt == "docx":
        problems = _docx_problems(path)
        stats = _docx_table_stats(path)
        if stats["tables"] == 0:
            problems.append("no native Word tables were created")
    elif fmt == "pptx":
        problems = _pptx_problems(path)
        stats = _pptx_stats(path)
    elif fmt == "xlsx":
        problems = _xlsx_problems(path)
        stats = _xlsx_stats(path)
    else:
        raise OfficeValidationError(f"unsupported format for structural QA: {fmt}")
    defects: list[DefectReport] = []
    for problem in problems:
        severity = (
            DefectSeverity.CRITICAL
            if ("markdown" in problem or "no slides" in problem or "no text" in problem)
            else DefectSeverity.MAJOR
        )
        defects.append(
            make(
                severity,
                path.name,
                problem,
                "structural inspection of the generated file",
                "regenerate the artifact with the reported element corrected",
                AgentRole.PRODUCER,
            )
        )
    base = 100.0
    penalty = 0.0
    for defect in defects:
        if defect.severity is DefectSeverity.CRITICAL:
            penalty += 22.0
        elif defect.severity is DefectSeverity.MAJOR:
            penalty += 9.0
    score = max(0.0, min(100.0, base - penalty))
    return defects, round(score, 1)
