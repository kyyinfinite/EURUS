from __future__ import annotations

import re
from importlib import import_module
from pathlib import Path
from typing import Any

from eurus.office.errors import OfficeRenderError
from eurus.office.models.workbook import (
    ChartSpec,
    ConditionalSpec,
    FormulaColumn,
    SheetSpec,
    SheetTable,
    ValidationSpec,
    WorkbookSpec,
)

HEADER_FILL = "17324D"
HEADER_COLOR = "FFFFFF"
BAND_FILL = "EEF3FA"
ACCENT_FILL = "DCE9F9"
PANEL_FILL = "F4F6FA"
DARK_INK = "0F2540"
MUTED_INK = "5A6B84"
ACCENT = "1D5FAD"

FORMULA_SAFE = re.compile(r"^[A-Z]+[0-9]+$")
CHART_BUILDERS = {
    "bar": "BarChart",
    "column": "BarChart",
    "line": "LineChart",
    "pie": "PieChart",
    "area": "AreaChart",
    "scatter": "ScatterChart",
}


def _openpyxl() -> Any:
    try:
        return import_module("openpyxl")
    except ImportError as exc:
        raise OfficeRenderError("openpyxl is required for Excel rendering") from exc


def _safe_cell(value: Any, *, allow_formulas: bool = False) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        if allow_formulas and text.startswith("=") and len(text) > 1:
            return text
        return "'" + text
    return text[:32_000]


def _styles_module() -> Any:
    return import_module("openpyxl.styles")


def _anchor_cell(table: SheetTable, offset_rows: int = 0) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([0-9]{1,6})", table.start_cell.strip().upper())
    if not match:
        return (1 + offset_rows, 1)
    column_letters, row_text = match.groups()
    column = 0
    for char in column_letters:
        column = column * 26 + (ord(char) - 64)
    return (int(row_text) + offset_rows, column)


def _column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _style_header_row(worksheet: Any, row_index: int, column_count: int) -> None:
    styles = _styles_module()
    for cell in worksheet[row_index]:
        if cell.column > column_count:
            break
        cell.font = styles.Font(bold=True, color=HEADER_COLOR, name="Aptos", size=11)
        cell.fill = styles.PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = styles.Alignment(vertical="center", horizontal="left", wrap_text=True)
    worksheet.row_dimensions[row_index].height = 26


def _render_table(worksheet: Any, table: SheetTable) -> tuple[int, int]:
    start_row, start_column = _anchor_cell(table)
    headers = list(table.headers)
    worksheet.cell(row=start_row, column=start_column, value=headers[0] if headers else "")
    for index, header in enumerate(headers, 1):
        worksheet.cell(row=start_row, column=start_column + index - 1, value=header)
    _style_header_row(worksheet, start_row, len(headers))
    for row_offset, row in enumerate(table.rows, 1):
        for column_offset in range(len(headers)):
            value = row[column_offset] if column_offset < len(row) else None
            cell = worksheet.cell(
                row=start_row + row_offset,
                column=start_column + column_offset,
                value=_safe_cell(value, allow_formulas=table.allow_formulas),
            )
            cell.alignment = styles_alignment()
            if row_offset % 2 == 0 and table.style != "plain":
                cell.fill = _styles_module().PatternFill("solid", fgColor=BAND_FILL)
    for index in range(len(headers)):
        letter = _column_letter(start_column + index)
        best = len(str(headers[index])) + 2
        for row_offset in range(1, len(table.rows) + 1):
            value = worksheet.cell(row=start_row + row_offset, column=start_column + index).value
            best = max(best, min(48, len(str(value or "")) + 2))
        worksheet.column_dimensions[letter].width = max(11, min(46, best))
    return (start_row, start_column)


def styles_alignment() -> Any:
    styles = _styles_module()
    return styles.Alignment(vertical="top", wrap_text=False)


def _render_formula_columns(
    worksheet: Any,
    sheet: SheetSpec,
    table: SheetTable,
    table_origin: tuple[int, int],
    column_offset: int,
    formula_column: FormulaColumn,
) -> None:
    start_row, start_column = table_origin
    header_column = start_column + len(table.headers) + column_offset
    header_cell = worksheet.cell(row=start_row, column=header_column, value=formula_column.header)
    header_cell.font = _styles_module().Font(bold=True, color=HEADER_COLOR, name="Aptos")
    header_cell.fill = _styles_module().PatternFill("solid", fgColor=HEADER_FILL)
    for row_offset in range(1, len(table.rows) + 1):
        formula = formula_column.pattern.replace("{row}", str(start_row + row_offset))
        cell = worksheet.cell(row=start_row + row_offset, column=header_column, value=formula)
        cell.alignment = styles_alignment()
        if formula_column.number_format:
            cell.number_format = formula_column.number_format


def _render_kpis(worksheet: Any, sheet: SheetSpec, start_row: int) -> None:
    styles = _styles_module()
    row = start_row
    for kpi in sheet.kpis[:8]:
        label_cell = worksheet.cell(row=row, column=1, value=kpi.label)
        label_cell.font = styles.Font(bold=True, size=11, color=DARK_INK, name="Aptos")
        value_cell = worksheet.cell(row=row, column=2, value=kpi.formula)
        value_cell.font = styles.Font(bold=True, size=14, color=ACCENT, name="Aptos")
        if kpi.number_format:
            value_cell.number_format = kpi.number_format
        if kpi.detail:
            detail_cell = worksheet.cell(row=row, column=3, value=kpi.detail)
            detail_cell.font = styles.Font(size=10, color=MUTED_INK, italic=True, name="Aptos")
        row += 2
    return None


def _render_chart(worksheet: Any, chart_spec: ChartSpec) -> None:
    chart_module = import_module("openpyxl.chart")
    builder_name = CHART_BUILDERS.get(chart_spec.kind, "BarChart")
    chart = getattr(chart_module, builder_name)()
    chart.title = chart_spec.title
    chart.style = 10
    if chart_spec.kind == "bar":
        chart.type = "bar"
    elif chart_spec.kind == "column":
        chart.type = "col"
    for name, ref in chart_spec.series:
        values = chart_module.Reference(worksheet, range_string=ref)
        chart.series.append(chart_module.Series(values, title=name))
    categories = chart_module.Reference(worksheet, range_string=chart_spec.categories_ref)
    chart.set_categories(categories)
    chart.width = chart_spec.width_cm
    chart.height = chart_spec.height_cm
    worksheet.add_chart(chart, chart_spec.anchor)


def _render_conditional(worksheet: Any, rule: ConditionalSpec) -> None:
    formatting = import_module("openpyxl.formatting.rule")
    if rule.kind == "color_scale":
        worksheet.conditional_formatting.add(
            rule.range,
            formatting.ColorScaleRule(
                start_type="min",
                start_color=rule.low_color,
                mid_type="percentile",
                mid_value=50,
                mid_color=rule.mid_color,
                end_type="max",
                end_color=rule.high_color,
            ),
        )
    elif rule.kind == "data_bar":
        worksheet.conditional_formatting.add(
            rule.range, formatting.DataBarRule(start_type="min", end_type="max", color=ACCENT)
        )
    else:
        operator = rule.operator or "greaterThan"
        formula = rule.formula or "0"
        worksheet.conditional_formatting.add(
            rule.range,
            formatting.CellIsRule(
                operator=operator,
                formula=[formula],
                fill=_styles_module().PatternFill("solid", fgColor=rule.high_color),
            ),
        )


def _render_validation(worksheet: Any, rule: ValidationSpec) -> None:
    worksheet_module = import_module("openpyxl.worksheet.datavalidation")
    validation = worksheet_module.DataValidation(
        type=rule.kind,
        formula1=rule.formula1,
        formula2=rule.formula2 or None,
        allow_blank=rule.allow_blank,
        showDropDown=False if rule.kind == "list" else None,
    )
    worksheet.add_data_validation(validation)
    validation.add(rule.range)


def _auto_widths(worksheet: Any) -> None:
    for column_cells in worksheet.columns:
        letter = column_cells[0].column_letter
        width = 12
        for cell in column_cells[:200]:
            value = cell.value
            if value is None:
                continue
            text = str(value)
            if text.startswith("="):
                continue
            width = max(width, min(44, len(text) + 2))
        worksheet.column_dimensions[letter].width = width


def render_workbook(spec: WorkbookSpec, output_path: Path) -> Path:
    openpyxl = _openpyxl()
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    used_names: set[str] = set()
    for sheet in spec.sheets:
        worksheet = workbook.create_sheet(sheet.safe_name)
        used_names.add(sheet.safe_name.casefold())
    worksheets = {
        workbook.sheetnames[index]: workbook.worksheets[index]
        for index in range(len(workbook.sheetnames))
    }
    for sheet in spec.sheets:
        worksheet = worksheets[sheet.safe_name]
        _render_sheet(worksheet, sheet, worksheets)
    for named_range in spec.named_ranges:
        try:
            workbook.defined_names.add(
                openpyxl.workbook.defined_name.DefinedName(
                    named_range.name, attr_text=named_range.reference
                )
            )
        except Exception as exc:
            raise OfficeRenderError(f"cannot add named range {named_range.name}: {exc}") from exc
    workbook.properties.title = spec.title
    workbook.properties.subject = spec.subject
    if spec.recalc_on_load:
        workbook.calculation.fullCalcOnLoad = True
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(output_path))
    return output_path


def _render_sheet(
    worksheet: Any, sheet: SheetSpec, worksheets: dict[str, Any] | None = None
) -> None:
    row_cursor = 1
    for table in sheet.tables:
        origin = _render_table(worksheet, table)
        for offset, formula_column in enumerate(sheet.formula_columns):
            _render_formula_columns(worksheet, sheet, table, origin, offset, formula_column)
        row_cursor = origin[0] + len(table.rows) + 2
    if sheet.kpis:
        _render_kpis(worksheet, sheet, row_cursor)
        row_cursor += min(len(sheet.kpis), 8) * 2
    for chart_spec in sheet.charts:
        _render_chart(worksheet, chart_spec)
    for raw_rule in sheet.conditional:
        rule = raw_rule
        target = worksheet
        if "!" in raw_rule.range and worksheets:
            sheet_name, _, cell_range = raw_rule.range.partition("!")
            cleaned = sheet_name.strip().strip("'")
            if cleaned in worksheets:
                target = worksheets[cleaned]
                rule = ConditionalSpec(
                    kind=raw_rule.kind,
                    range=cell_range,
                    operator=raw_rule.operator,
                    formula=raw_rule.formula,
                    low_color=raw_rule.low_color,
                    mid_color=raw_rule.mid_color,
                    high_color=raw_rule.high_color,
                )
        _render_conditional(target, rule)
    for validation_rule in sheet.validation:
        _render_validation(worksheet, validation_rule)
    for column_reference, number_format in sheet.column_formats:
        match = re.fullmatch(r"([A-Z]{1,3})", column_reference.strip().upper())
        if not match:
            continue
        letter = match.group(1)
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if cell.column_letter == letter and cell.value is not None:
                    cell.number_format = number_format
    if sheet.freeze_panes:
        worksheet.freeze_panes = sheet.freeze_panes
    elif sheet.tables:
        origin = _anchor_cell(sheet.tables[0])
        worksheet.freeze_panes = f"A{origin[0] + 1}"
    if sheet.auto_filter and sheet.tables:
        table = sheet.tables[0]
        origin = _anchor_cell(table)
        last_column = _column_letter(
            origin[1] + len(table.headers) - 1 + len(sheet.formula_columns)
        )
        last_row = origin[0] + len(table.rows)
        worksheet.auto_filter.ref = f"A{origin[0]}:{last_column}{last_row}"
    for note in sheet.notes[:6]:
        worksheet.cell(row=row_cursor, column=1, value=note).font = _styles_module().Font(
            size=9, italic=True, color=MUTED_INK, name="Aptos"
        )
        row_cursor += 1
    if sheet.print_area:
        worksheet.print_area = sheet.print_area
    worksheet.sheet_view.showGridLines = sheet.role != "dashboard"
    if not sheet.tables and not sheet.kpis:
        _auto_widths(worksheet)
