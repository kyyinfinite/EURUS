from __future__ import annotations

from pathlib import Path

import pytest

from eurus.office.models.workbook import (
    ChartSpec,
    ConditionalSpec,
    KpiCard,
    NamedRangeSpec,
    SheetSpec,
    SheetTable,
    ValidationSpec,
    WorkbookSpec,
)
from eurus.office.qa.structural import validate_structure
from eurus.office.renderers.excel import render_workbook

openpyxl = pytest.importorskip("openpyxl")


def build_spec() -> WorkbookSpec:
    transactions = SheetSpec(
        name="Transactions",
        role="data",
        tables=(
            SheetTable(
                headers=("Month", "Revenue", "Cost", "Profit"),
                rows=(
                    ("Jan", 120, 80, "=B2-C2"),
                    ("Feb", 135, 84, "=B3-C3"),
                    ("Mar", 150, 90, "=B4-C4"),
                ),
                allow_formulas=True,
            ),
        ),
        column_formats=(("B", "#,##0"), ("C", "#,##0")),
        freeze_panes="A2",
        auto_filter=True,
    )
    dashboard = SheetSpec(
        name="Dashboard",
        role="dashboard",
        kpis=(
            KpiCard("Total revenue", "=SUM(Transactions!B2:B4)", "#,##0", "Quarter"),
            KpiCard("Average profit", "=AVERAGE(Transactions!D2:D4)", "#,##0", "Monthly"),
        ),
        charts=(
            ChartSpec(
                kind="column",
                title="Revenue vs cost",
                categories_ref="Transactions!A2:A4",
                series=(
                    ("Revenue", "Transactions!B2:B4"),
                    ("Cost", "Transactions!C2:C4"),
                ),
                anchor="F3",
            ),
        ),
        conditional=(ConditionalSpec(kind="data_bar", range="Transactions!B2:B4"),),
    )
    inputs = SheetSpec(
        name="Inputs",
        role="input",
        tables=(
            SheetTable(
                headers=("Parameter", "Value"),
                rows=(("Growth", 0.1), ("Scenario", "base")),
            ),
        ),
        validation=(
            ValidationSpec(kind="decimal", range="B2:B2", formula1="0", formula2="1"),
            ValidationSpec(kind="list", range="B3:B3", formula1='"base,stretch"'),
        ),
    )
    return WorkbookSpec(
        title="H1 Financial Analysis",
        subject="Finance",
        sheets=(transactions, dashboard, inputs),
        named_ranges=(NamedRangeSpec("RevenueColumn", "Transactions!$B$2:$B$4"),),
    )


def test_xlsx_formulas_stay_formulas(tmp_path: Path) -> None:
    path = render_workbook(build_spec(), tmp_path / "book.xlsx")
    workbook = openpyxl.load_workbook(path, data_only=False)
    worksheet = workbook["Transactions"]
    assert worksheet["D2"].value == "=B2-C2"
    assert worksheet["D3"].value == "=B3-C3"
    workbook.close()


def test_xlsx_formula_injection_protection_for_text(tmp_path: Path) -> None:
    spec = WorkbookSpec(
        title="Guarded",
        sheets=(
            SheetSpec(
                name="Data",
                role="data",
                tables=(
                    SheetTable(
                        headers=("Label", "Note"),
                        rows=(("=cmd|'/c calc'!A0", "safe"),),
                        allow_formulas=False,
                    ),
                ),
            ),
        ),
    )
    path = render_workbook(spec, tmp_path / "guarded.xlsx")
    workbook = openpyxl.load_workbook(path, data_only=False)
    assert workbook["Data"]["A2"].value == "'=cmd|'/c calc'!A0"
    workbook.close()


def test_xlsx_named_ranges_freeze_filter_formats(tmp_path: Path) -> None:
    path = render_workbook(build_spec(), tmp_path / "book.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert "RevenueColumn" in workbook.defined_names
    transactions = workbook["Transactions"]
    assert transactions.freeze_panes == "A2"
    assert transactions.auto_filter.ref is not None
    assert transactions["B2"].number_format == "#,##0"
    workbook.close()


def test_xlsx_charts_conditional_validation(tmp_path: Path) -> None:
    path = render_workbook(build_spec(), tmp_path / "book.xlsx")
    workbook = openpyxl.load_workbook(path)
    dashboard = workbook["Dashboard"]
    assert len(dashboard._charts) == 1
    chart = dashboard._charts[0]
    assert chart.title is not None
    assert len(chart.series) == 2
    conditional_count = len(list(workbook["Transactions"].conditional_formatting))
    assert conditional_count >= 1
    validations = workbook["Inputs"].data_validations.dataValidation
    assert len(validations) == 2
    workbook.close()


def test_xlsx_recalc_on_load_setting(tmp_path: Path) -> None:
    path = render_workbook(build_spec(), tmp_path / "book.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert workbook.calculation.fullCalcOnLoad is True
    workbook.close()


def test_xlsx_structural_qa_passes(tmp_path: Path) -> None:
    path = render_workbook(build_spec(), tmp_path / "book.xlsx")
    defects, score = validate_structure("xlsx", path, "job-test")
    assert score >= 95.0
    assert not defects


def test_xlsx_structural_qa_flags_unknown_sheet_reference(tmp_path: Path) -> None:
    spec = WorkbookSpec(
        title="Broken",
        sheets=(
            SheetSpec(
                name="Calc",
                role="calculations",
                tables=(
                    SheetTable(
                        headers=("Kpi", "Value"),
                        rows=(("Total", "=SUM(Missing!B2:B4)"),),
                        allow_formulas=True,
                    ),
                ),
            ),
        ),
    )
    path = render_workbook(spec, tmp_path / "broken.xlsx")
    defects, score = validate_structure("xlsx", path, "job-test")
    assert any("unknown sheet" in d.description for d in defects)
    assert score < 100.0
