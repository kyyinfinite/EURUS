from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.office.errors import (
    OfficeBudgetError,
    OfficeValidationError,
    OfficeWorkflowError,
)
from eurus.office.models.document import (
    DocumentMetadata,
    DocumentSpec,
    HeadingBlock,
    ParagraphBlock,
    RichText,
    TableBlock,
)
from eurus.office.models.presentation import PresentationSpec, SlideSpec
from eurus.office.models.review import DefectReport, QAReport
from eurus.office.models.task import (
    AgentRole,
    DefectSeverity,
    JobStage,
    OfficeBrief,
    OfficeFormat,
    OfficeJobState,
    QualityScores,
    StageStatus,
)
from eurus.office.models.workbook import KpiCard, SheetSpec, SheetTable, WorkbookSpec
from eurus.office.specialist.budget import BudgetLimits, BudgetTracker
from eurus.office.specialist.workflow import validate_transition
from eurus.office.storage import JobWorkspace, brief_from_payload, create_job_id


def test_office_brief_validates_objective_and_format() -> None:
    with pytest.raises(OfficeValidationError):
        OfficeBrief(objective="   ", format=OfficeFormat.DOCX)
    with pytest.raises(OfficeValidationError):
        OfficeBrief(objective="x", format="pdf")
    with pytest.raises(OfficeValidationError):
        OfficeBrief(objective="Make a report", format=OfficeFormat.DOCX, deadline_seconds=-1)
    brief = OfficeBrief(objective="Make a report", format="word")
    assert brief.format is OfficeFormat.DOCX


def test_office_brief_round_trip() -> None:
    brief = OfficeBrief(
        objective="Objective",
        format=OfficeFormat.PPTX,
        audience="students",
        constraints=("one", "two"),
        deadline_seconds=120.0,
    )
    restored = OfficeBrief.from_dict(json.loads(json.dumps(brief.to_dict())))
    assert restored == brief


def test_document_spec_requires_title_and_style() -> None:
    with pytest.raises(OfficeValidationError):
        DocumentSpec(metadata=DocumentMetadata(title="  "))
    with pytest.raises(OfficeValidationError):
        DocumentSpec(metadata=DocumentMetadata(title="T"), style="mystery")
    spec = DocumentSpec(
        metadata=DocumentMetadata(title="T"),
        blocks=(HeadingBlock(1, "H"), ParagraphBlock(RichText.from_text("Body"))),
    )
    assert spec.blocks[0].level == 1


def test_table_block_requires_matching_widths() -> None:
    with pytest.raises(OfficeValidationError):
        TableBlock(headers=("A", "B"), rows=(("1",),))
    table = TableBlock(headers=("A", "B"), rows=(("1", "2"), ("3", "4")))
    assert table.caption == ""


def test_presentation_must_start_with_cover() -> None:
    with pytest.raises(OfficeValidationError):
        PresentationSpec(title="Deck", slides=(SlideSpec(layout="content", title="X"),))
    spec = PresentationSpec(
        title="Deck",
        slides=(
            SlideSpec(layout="cover", title="Cover"),
            SlideSpec(layout="content", title="Body", bullets=("a",)),
        ),
    )
    assert len(spec.slides) == 2


def test_workbook_rejects_duplicate_sheet_names_and_bad_kpi() -> None:
    base = SheetTable(headers=("A",), rows=(("1",),))
    with pytest.raises(OfficeValidationError):
        WorkbookSpec(
            title="W",
            sheets=(
                SheetSpec(name="Data", role="data", tables=(base,)),
                SheetSpec(name="data", role="data", tables=(base,)),
            ),
        )
    with pytest.raises(OfficeValidationError):
        KpiCard(label="L", formula="SUM(A1:A2)")
    workbook = WorkbookSpec(
        title="W",
        sheets=(SheetSpec(name="Data", role="data", tables=(base,)),),
    )
    assert workbook.sheets[0].safe_name == "Data"


def test_workflow_transitions_reject_illegal_paths() -> None:
    validate_transition(JobStage.RECEIVED, JobStage.BRIEF_ANALYSIS)
    with pytest.raises(OfficeWorkflowError):
        validate_transition(JobStage.RECEIVED, JobStage.COMPLETED)
    with pytest.raises(OfficeWorkflowError):
        validate_transition(JobStage.COMPLETED, JobStage.PRODUCTION)


def test_budget_tracker_enforces_total_and_role_caps() -> None:
    limits = BudgetLimits(max_total_model_calls=3, max_revision_cycles=1, role_budgets={})
    tracker = BudgetTracker(limits)
    for _ in range(3):
        tracker.start_role_call(AgentRole.PRODUCER)
    with pytest.raises(OfficeBudgetError):
        tracker.start_role_call(AgentRole.PRODUCER)


def test_budget_tracker_role_cap() -> None:
    limits = BudgetLimits(max_total_model_calls=50, max_revision_cycles=1)
    tracker = BudgetTracker(limits)
    for _ in range(limits.role_budgets[AgentRole.CRITIC]):
        tracker.start_role_call(AgentRole.CRITIC)
    with pytest.raises(OfficeBudgetError):
        tracker.start_role_call(AgentRole.CRITIC)
    tracker.start_role_call(AgentRole.CLIENT)
    assert tracker.snapshot()["total_model_calls"] == limits.role_budgets[AgentRole.CRITIC] + 1


def test_job_state_stage_history_round_trip(tmp_path: Path) -> None:
    brief = OfficeBrief(objective="Objective", format=OfficeFormat.XLSX)
    state = OfficeJobState(job_id="job-x", task_id="t", brief=brief)
    state = state.with_stage(JobStage.BRIEF_ANALYSIS, "starting")
    state = state.with_stage_finished(StageStatus.PASSED, "done")
    state = state.with_quality(QualityScores(content=90.0, structural=97.0))
    restored = OfficeJobState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.stage is JobStage.BRIEF_ANALYSIS
    assert restored.stage_history[-1].status is StageStatus.PASSED
    assert restored.quality.structural == 97.0


def test_job_workspace_isolation_and_traversal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path / "home"))
    workspace = JobWorkspace.create(create_job_id())
    inside = workspace.staging_path("artifact.docx")
    assert workspace.contains(inside)
    with pytest.raises(OfficeValidationError):
        workspace.staging_path("../outside.docx")
    with pytest.raises(OfficeValidationError):
        workspace.final_path("../../escape.docx")


def test_job_workspace_persists_task_state_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path / "home"))
    brief = brief_from_payload({"objective": "Do the thing", "format": "word"})
    job_id = create_job_id()
    workspace = JobWorkspace.create(job_id)
    task = __import__("eurus.office.models.task", fromlist=["OfficeTask"]).OfficeTask(
        task_id="task-1", job_id=job_id, brief=brief
    )
    workspace.save_task(task)
    workspace.save_state(OfficeJobState(job_id=job_id, task_id="task-1", brief=brief))
    workspace.append_event("test.event", {"detail": "value"})
    reloaded = JobWorkspace.open(job_id)
    assert reloaded.load_task().brief.objective == "Do the thing"
    assert reloaded.load_state().task_id == "task-1"
    assert reloaded.read_events()[0]["event"] == "test.event"


def test_defect_and_qa_models() -> None:
    defect = DefectReport(
        defect_id="d1",
        severity=DefectSeverity.CRITICAL,
        artifact_location="page 1",
        description="text clipped",
        layer="visual",
    )
    assert defect.responsible_agent is AgentRole.PRODUCER
    qa = QAReport(job_id="j", task_id="t", structural_score=99.0, defects=(defect,))
    assert qa.unresolved_critical
    assert len(qa.open_defects()) == 1
    payload = qa.to_dict()
    assert payload["defects"][0]["severity"] == "critical"
