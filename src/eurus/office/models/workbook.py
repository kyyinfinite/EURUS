from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from eurus.office.errors import OfficeValidationError

MAX_SHEETS = 20
MAX_ROWS = 10_000
MAX_COLUMNS = 60
MAX_CELLS = 200_000
MAX_NAME_LENGTH = 31

SHEET_ROLES = (
    "overview",
    "data",
    "input",
    "constants",
    "calculations",
    "results",
    "dashboard",
    "summary",
)

CHART_KINDS = ("bar", "column", "line", "pie", "area", "scatter")
CONDITIONAL_KINDS = ("color_scale", "data_bar", "cell_is")
VALIDATION_KINDS = ("list", "decimal", "whole")
CELL_REF = r"\$?[A-Z]{1,3}\$?[0-9]{1,7}"


class SheetRole(StrEnum):
    OVERVIEW = "overview"
    DATA = "data"
    INPUT = "input"
    CONSTANTS = "constants"
    CALCULATIONS = "calculations"
    RESULTS = "results"
    DASHBOARD = "dashboard"
    SUMMARY = "summary"


@dataclass(frozen=True, slots=True)
class FormulaColumn:
    header: str
    pattern: str
    number_format: str = ""

    def __post_init__(self) -> None:
        if not self.header.strip():
            raise OfficeValidationError("formula column header must not be empty")
        if not self.pattern.strip().startswith("="):
            raise OfficeValidationError("formula column pattern must start with =")
        if "{row}" not in self.pattern:
            raise OfficeValidationError("formula column pattern must contain {row}")


@dataclass(frozen=True, slots=True)
class KpiCard:
    label: str
    formula: str
    number_format: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.formula.strip().startswith("="):
            raise OfficeValidationError("kpi formula must start with =")
        if not self.label.strip():
            raise OfficeValidationError("kpi label must not be empty")


@dataclass(frozen=True, slots=True)
class ChartSpec:
    kind: str
    title: str
    categories_ref: str
    series: tuple[tuple[str, str], ...]
    anchor: str = "A1"
    width_cm: float = 16.0
    height_cm: float = 9.0

    def __post_init__(self) -> None:
        if self.kind not in CHART_KINDS:
            raise OfficeValidationError(f"chart kind must be one of: {', '.join(CHART_KINDS)}")
        if not self.title.strip():
            raise OfficeValidationError("chart title must not be empty")
        if not self.categories_ref.strip():
            raise OfficeValidationError("chart categories_ref must not be empty")
        if not self.series:
            raise OfficeValidationError("chart needs at least one series")
        if not 4.0 <= self.width_cm <= 24.0 or not 3.0 <= self.height_cm <= 18.0:
            raise OfficeValidationError("chart size must be within workbook bounds")


@dataclass(frozen=True, slots=True)
class ConditionalSpec:
    kind: str
    range: str
    operator: str = ""
    formula: str = ""
    low_color: str = "F8696B"
    mid_color: str = "FFEB84"
    high_color: str = "63BE7B"

    def __post_init__(self) -> None:
        if self.kind not in CONDITIONAL_KINDS:
            raise OfficeValidationError(
                f"conditional kind must be one of: {', '.join(CONDITIONAL_KINDS)}"
            )
        if not self.range.strip():
            raise OfficeValidationError("conditional range must not be empty")


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    kind: str
    range: str
    formula1: str = ""
    formula2: str = ""
    allow_blank: bool = True

    def __post_init__(self) -> None:
        if self.kind not in VALIDATION_KINDS:
            raise OfficeValidationError(
                f"validation kind must be one of: {', '.join(VALIDATION_KINDS)}"
            )
        if not self.range.strip():
            raise OfficeValidationError("validation range must not be empty")


@dataclass(frozen=True, slots=True)
class NamedRangeSpec:
    name: str
    reference: str

    def __post_init__(self) -> None:
        if not self.name.replace("_", "").isalnum() or not self.name[0].isalpha():
            raise OfficeValidationError(
                "named range name must be alphanumeric and start with a letter"
            )
        if not self.reference.strip():
            raise OfficeValidationError("named range reference must not be empty")


@dataclass(frozen=True, slots=True)
class SheetTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    start_cell: str = "A1"
    style: str = "accent"
    allow_formulas: bool = False

    def __post_init__(self) -> None:
        if not self.headers:
            raise OfficeValidationError("sheet table headers must not be empty")
        if len(self.headers) > MAX_COLUMNS:
            raise OfficeValidationError(f"sheet tables are limited to {MAX_COLUMNS} columns")
        if len(self.rows) > MAX_ROWS:
            raise OfficeValidationError(f"sheet tables are limited to {MAX_ROWS} rows")
        for row in self.rows:
            if len(row) > len(self.headers):
                raise OfficeValidationError("sheet table row must not exceed header count")


@dataclass(frozen=True, slots=True)
class SheetSpec:
    name: str
    role: str
    tables: tuple[SheetTable, ...] = ()
    formula_columns: tuple[FormulaColumn, ...] = ()
    kpis: tuple[KpiCard, ...] = ()
    charts: tuple[ChartSpec, ...] = ()
    conditional: tuple[ConditionalSpec, ...] = ()
    validation: tuple[ValidationSpec, ...] = ()
    column_formats: tuple[tuple[str, str], ...] = ()
    freeze_panes: str = ""
    auto_filter: bool = False
    notes: tuple[str, ...] = ()
    print_area: str = ""

    def __post_init__(self) -> None:
        name = clean_sheet_name(self.name)
        if not name:
            raise OfficeValidationError("sheet name must not be empty")
        if self.role not in SHEET_ROLES:
            raise OfficeValidationError(f"sheet role must be one of: {', '.join(SHEET_ROLES)}")
        if len(self.charts) > 8:
            raise OfficeValidationError("sheets are limited to 8 charts")
        if self.role == "dashboard" and not self.kpis and not self.charts:
            raise OfficeValidationError("dashboard sheets need kpis or charts")

    @property
    def safe_name(self) -> str:
        return clean_sheet_name(self.name)


@dataclass(frozen=True, slots=True)
class WorkbookSpec:
    title: str
    subject: str = ""
    sheets: tuple[SheetSpec, ...] = ()
    named_ranges: tuple[NamedRangeSpec, ...] = ()
    recalc_on_load: bool = True
    language: str = "en"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise OfficeValidationError("workbook title must not be empty")
        if not self.sheets:
            raise OfficeValidationError("workbook needs at least one sheet")
        if len(self.sheets) > MAX_SHEETS:
            raise OfficeValidationError(f"workbooks are limited to {MAX_SHEETS} sheets")
        names = [sheet.safe_name.casefold() for sheet in self.sheets]
        if len(names) != len(set(names)):
            raise OfficeValidationError("sheet names must be unique")
        total_cells = 0
        for sheet in self.sheets:
            for table in sheet.tables:
                total_cells += len(table.headers) * (len(table.rows) + 1)
        if total_cells > MAX_CELLS:
            raise OfficeValidationError(f"workbooks are limited to {MAX_CELLS} cells")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subject": self.subject,
            "recalc_on_load": self.recalc_on_load,
            "named_ranges": [
                {"name": item.name, "reference": item.reference} for item in self.named_ranges
            ],
            "sheets": [sheet_to_dict(sheet) for sheet in self.sheets],
        }


def clean_sheet_name(raw: str) -> str:
    cleaned = ""
    for char in raw.strip():
        if char in "\\/*?:[]":
            cleaned += "_"
        else:
            cleaned += char
    return cleaned[:MAX_NAME_LENGTH]


def sheet_to_dict(sheet: SheetSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": sheet.name, "role": sheet.role}
    if sheet.tables:
        payload["tables"] = [
            {
                "headers": list(table.headers),
                "rows": [list(row) for row in table.rows],
                "start_cell": table.start_cell,
                "style": table.style,
            }
            for table in sheet.tables
        ]
    if sheet.formula_columns:
        payload["formula_columns"] = [
            {
                "header": item.header,
                "pattern": item.pattern,
                "number_format": item.number_format,
            }
            for item in sheet.formula_columns
        ]
    if sheet.kpis:
        payload["kpis"] = [
            {
                "label": item.label,
                "formula": item.formula,
                "number_format": item.number_format,
                "detail": item.detail,
            }
            for item in sheet.kpis
        ]
    if sheet.charts:
        payload["charts"] = [
            {
                "kind": chart.kind,
                "title": chart.title,
                "categories_ref": chart.categories_ref,
                "series": [{"name": name, "ref": ref} for name, ref in chart.series],
                "anchor": chart.anchor,
                "width_cm": chart.width_cm,
                "height_cm": chart.height_cm,
            }
            for chart in sheet.charts
        ]
    if sheet.conditional:
        payload["conditional"] = [
            {
                "kind": rule.kind,
                "range": rule.range,
                "operator": rule.operator,
                "formula": rule.formula,
            }
            for rule in sheet.conditional
        ]
    if sheet.validation:
        payload["validation"] = [
            {
                "kind": rule.kind,
                "range": rule.range,
                "formula1": rule.formula1,
                "formula2": rule.formula2,
            }
            for rule in sheet.validation
        ]
    if sheet.column_formats:
        payload["column_formats"] = {column: fmt for column, fmt in sheet.column_formats}
    for name in ("freeze_panes", "print_area"):
        value = getattr(sheet, name)
        if value:
            payload[name] = value
    if sheet.auto_filter:
        payload["auto_filter"] = True
    if sheet.notes:
        payload["notes"] = list(sheet.notes)
    return payload


@dataclass(frozen=True, slots=True)
class ArtifactAttachment:
    filename: str
    description: str = ""
