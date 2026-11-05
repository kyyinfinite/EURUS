from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("docx")


def test_internal_renderers_remain_available_to_the_department(tmp_path: Path) -> None:
    from eurus.office.design.layouts import plan_presentation
    from eurus.office.models.document import (
        DocumentMetadata,
        DocumentSpec,
        HeadingBlock,
        ParagraphBlock,
        RichText,
    )
    from eurus.office.models.presentation import PresentationSpec, SlideSpec
    from eurus.office.models.workbook import SheetSpec, SheetTable, WorkbookSpec
    from eurus.office.renderers.excel import render_workbook
    from eurus.office.renderers.powerpoint import render_presentation
    from eurus.office.renderers.word import render_document

    document = render_document(
        DocumentSpec(
            metadata=DocumentMetadata(title="Internal renderer check"),
            blocks=(
                HeadingBlock(1, "H"),
                ParagraphBlock(RichText.from_text("Body")),
            ),
        ),
        tmp_path / "check.docx",
    )
    workbook = render_workbook(
        WorkbookSpec(
            title="Internal",
            sheets=(
                SheetSpec(
                    name="Data",
                    role="data",
                    tables=(SheetTable(headers=("A",), rows=(("1",),)),),
                ),
            ),
        ),
        tmp_path / "check.xlsx",
    )
    spec = PresentationSpec(
        title="Internal",
        slides=(
            SlideSpec(layout="cover", title="Cover"),
            SlideSpec(layout="content", title="Body", bullets=("one",)),
        ),
    )
    deck = render_presentation(spec, plan_presentation(spec), tmp_path / "check.pptx")
    for path in (document, workbook, deck):
        assert path.is_file()
        assert path.read_bytes()[:2] == b"PK"
