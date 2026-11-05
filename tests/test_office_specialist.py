from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.models import Message, Usage
from eurus.office.errors import OfficeError
from eurus.office.models.task import JobStage, OfficeFormat, OfficeTask
from eurus.office.specialist.budget import BudgetLimits
from eurus.office.specialist.manager import OfficeSpecialist
from eurus.office.storage import JobWorkspace, brief_from_payload, create_job_id
from eurus.providers.base import ChatResponse
from eurus.tools.base import ToolContext

docx = pytest.importorskip("docx")

CLIENT_ANALYSIS = {
    "audience": "manajemen",
    "tone": "formal",
    "style": "business",
    "requirements": ["tabel", "referensi"],
    "acceptance_criteria": ["bersih"],
    "research_needed": False,
    "research_questions": [],
    "language": "id",
}

RESEARCH = {
    "facts": [{"statement": "Fakta satu", "kind": "verified", "source": "Sumber"}],
    "sources": ["Sumber"],
    "outline": ["Ringkasan"],
    "gaps": [],
}

DESIGN = {
    "direction": "corporate",
    "document_style": "business",
    "guidance": [],
    "palette_notes": "",
    "typography_notes": "",
    "layout_plan": [],
}

SPEC_V1 = {
    "kind": "document",
    "metadata": {"title": "Laporan", "author": "Eurus"},
    "style": "business",
    "include_toc": False,
    "blocks": [
        {"kind": "heading", "level": 1, "text": "Ringkasan"},
        {"kind": "paragraph", "text": "Paragraf pembuka laporan versi pertama."},
    ],
}

SPEC_V2 = {
    "kind": "document",
    "metadata": {"title": "Laporan", "author": "Eurus"},
    "style": "business",
    "include_toc": False,
    "blocks": [
        {"kind": "heading", "level": 1, "text": "Ringkasan"},
        {"kind": "paragraph", "text": "Paragraf revisi yang sudah lengkap."},
        {"kind": "table", "headers": ["A", "B"], "rows": [["1", "2"]], "caption": "Tabel"},
        {"kind": "bibliography", "entries": ["Sumber 2026."]},
    ],
}

CRITIC_STRICT = {
    "content_score": 60,
    "summary": "Belum lengkap.",
    "defects": [
        {
            "severity": "major",
            "artifact_location": "document",
            "description": "required table missing",
            "evidence": "no tables",
            "responsible_agent": "producer",
            "recommended_fix": "add the table",
            "layer": "content",
        }
    ],
}

CRITIC_FINAL = {"content_score": 93, "summary": "Lengkap.", "defects": []}

ACCEPT_NO = {
    "accepted": False,
    "content_score": 55,
    "missing_content": ["tabel"],
    "reasons": ["content incomplete"],
    "client_summary": "Perlu revisi.",
}

ACCEPT_YES = {
    "accepted": True,
    "content_score": 92,
    "missing_content": [],
    "reasons": [],
    "client_summary": "OK",
}


class ScriptedRouter:
    def __init__(self, *, production_specs, critic_payloads, accept_payloads) -> None:
        self.production_specs = list(production_specs)
        self.critic_payloads = list(critic_payloads)
        self.accept_payloads = list(accept_payloads)
        self.calls: list[str] = []
        self.production_calls = 0

    async def chat(self, messages, *, tools=(), temperature=None, max_tokens=None):
        system = messages[0].content
        user = messages[1].content
        if "evaluating the finished document" in user:
            payload = self.accept_payloads.pop(0) if self.accept_payloads else ACCEPT_YES
            role = "accept"
        elif "Critic Agent" in system:
            payload = self.critic_payloads.pop(0) if self.critic_payloads else CRITIC_FINAL
            role = "critic"
        elif "Client Agent" in system:
            payload = CLIENT_ANALYSIS
            role = "analysis"
        elif "Research Agent" in system:
            payload = RESEARCH
            role = "research"
        elif "Design Agent" in system:
            payload = DESIGN
            role = "design"
        elif "Production Agent" in system:
            self.production_calls += 1
            index = min(self.production_calls, len(self.production_specs)) - 1
            payload = self.production_specs[index]
            role = "production"
        else:
            raise AssertionError("unexpected role prompt")
        self.calls.append(role)
        return ChatResponse(
            message=Message.assistant(json.dumps(payload)), usage=Usage(), finish_reason="stop"
        )


def make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def make_task(tmp_path: Path, output_path: str) -> OfficeTask:
    brief = brief_from_payload(
        {
            "objective": "Laporan manajemen dengan tabel dan referensi.",
            "format": "docx",
            "language": "id",
            "output_path": output_path,
        }
    )
    return OfficeTask(task_id="task-1", job_id=create_job_id(), brief=brief)


@pytest.mark.asyncio
async def test_specialist_completes_full_lifecycle(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V2],
        critic_payloads=[CRITIC_FINAL],
        accept_payloads=[ACCEPT_YES],
    )
    context = make_context(tmp_path)
    task = make_task(tmp_path, "outputs/report.docx")
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=40, max_revision_cycles=2, deadline_seconds=600),
    )
    result = await specialist.run()
    assert result.status == "completed"
    assert result.quality_status in {"passed", "passed_with_warnings"}
    assert result.format is OfficeFormat.DOCX
    assert result.artifact_path.endswith(".docx")
    assert Path(result.artifact_path).is_file()
    assert Path(result.qa_report_path).is_file()
    stages = [record.stage for record in specialist.state.stage_history]
    for expected in (
        JobStage.RECEIVED,
        JobStage.BRIEF_ANALYSIS,
        JobStage.TEAM_PLANNING,
        JobStage.RESEARCH,
        JobStage.DESIGN,
        JobStage.PRODUCTION,
        JobStage.STRUCTURAL_QA,
        JobStage.RENDERING,
        JobStage.VISUAL_QA,
        JobStage.CRITIC_REVIEW,
        JobStage.CLIENT_ACCEPTANCE,
        JobStage.FINALIZATION,
        JobStage.COMPLETED,
    ):
        assert expected in stages
    assert "analysis" in router.calls and "critic" in router.calls


@pytest.mark.asyncio
async def test_specialist_revision_loop_fixes_defects(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V1, SPEC_V2],
        critic_payloads=[CRITIC_STRICT, CRITIC_FINAL],
        accept_payloads=[ACCEPT_NO, ACCEPT_YES],
    )
    context = make_context(tmp_path)
    task = make_task(tmp_path, "outputs/revision.docx")
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=60, max_revision_cycles=3, deadline_seconds=900),
    )
    result = await specialist.run()
    assert result.status == "completed"
    assert result.revision_count == 1
    stages = [record.stage for record in specialist.state.stage_history]
    assert stages.count(JobStage.PRODUCTION) == 2
    assert JobStage.REVISION in stages
    document = docx.Document(result.artifact_path)
    assert len(document.tables) >= 1


@pytest.mark.asyncio
async def test_specialist_reports_quality_limit_after_budget(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V1],
        critic_payloads=[CRITIC_STRICT],
        accept_payloads=[ACCEPT_NO],
    )
    context = make_context(tmp_path)
    task = make_task(tmp_path, "outputs/limited.docx")
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=60, max_revision_cycles=0, deadline_seconds=900),
    )
    result = await specialist.run()
    assert result.status == "quality_limit_reached"
    assert result.quality_status == "rejected"
    assert specialist.state.stage is JobStage.QUALITY_LIMIT_REACHED
    assert Path(result.artifact_path).is_file()


@pytest.mark.asyncio
async def test_specialist_budget_enforcement_fails_job(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V1],
        critic_payloads=[CRITIC_STRICT],
        accept_payloads=[ACCEPT_NO],
    )
    context = make_context(tmp_path)
    task = make_task(tmp_path, "outputs/budget.docx")
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=2, max_revision_cycles=3, deadline_seconds=900),
    )
    with pytest.raises(OfficeError):
        await specialist.run()
    assert specialist.state.stage is JobStage.FAILED
    persisted = JobWorkspace.open(task.job_id).load_state()
    assert persisted.stage is JobStage.FAILED


@pytest.mark.asyncio
async def test_specialist_persists_recoverable_state(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V2],
        critic_payloads=[CRITIC_FINAL],
        accept_payloads=[ACCEPT_YES],
    )
    context = make_context(tmp_path)
    task = make_task(tmp_path, "outputs/recovery.docx")
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=40, max_revision_cycles=2, deadline_seconds=600),
    )
    await specialist.run()
    reloaded = JobWorkspace.open(task.job_id)
    state = reloaded.load_state()
    assert state.stage is JobStage.COMPLETED
    assert state.brief.format is OfficeFormat.DOCX
    events = reloaded.read_events()
    assert any(event["event"] == "stage.entered" for event in events)
    plan = reloaded.load_plan()
    assert plan["job_id"] == task.job_id
    assert len(plan["assignments"]) == 6


@pytest.mark.asyncio
async def test_specialist_jobs_are_isolated(tmp_path: Path) -> None:
    router_one = ScriptedRouter(
        production_specs=[SPEC_V2], critic_payloads=[CRITIC_FINAL], accept_payloads=[ACCEPT_YES]
    )
    router_two = ScriptedRouter(
        production_specs=[SPEC_V2], critic_payloads=[CRITIC_FINAL], accept_payloads=[ACCEPT_YES]
    )
    context = make_context(tmp_path)
    task_one = make_task(tmp_path, "outputs/one.docx")
    task_two = make_task(tmp_path, "outputs/two.docx")
    specialist_one = OfficeSpecialist(
        router_one,
        context,
        task_one,
        workspace=JobWorkspace.create(task_one.job_id),
        limits=BudgetLimits(max_total_model_calls=40, max_revision_cycles=1, deadline_seconds=600),
    )
    specialist_two = OfficeSpecialist(
        router_two,
        context,
        task_two,
        workspace=JobWorkspace.create(task_two.job_id),
        limits=BudgetLimits(max_total_model_calls=40, max_revision_cycles=1, deadline_seconds=600),
    )
    result_one = await specialist_one.run()
    result_two = await specialist_two.run()
    assert task_one.job_id != task_two.job_id
    assert str(task_one.job_id) not in result_two.artifact_path
    assert str(task_two.job_id) not in result_one.artifact_path
    assert Path(result_one.artifact_path).is_file()
    assert Path(result_two.artifact_path).is_file()


@pytest.mark.asyncio
async def test_specialist_delivers_without_overwriting(tmp_path: Path) -> None:
    router = ScriptedRouter(
        production_specs=[SPEC_V2], critic_payloads=[CRITIC_FINAL], accept_payloads=[ACCEPT_YES]
    )
    context = make_context(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "same.docx").write_bytes(b"existing artifact")
    task = make_task(tmp_path, "outputs/same.docx")
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=JobWorkspace.create(task.job_id),
        limits=BudgetLimits(max_total_model_calls=40, max_revision_cycles=1, deadline_seconds=600),
    )
    result = await specialist.run()
    assert Path(result.artifact_path).name != "same.docx"
    assert (tmp_path / "outputs" / "same.docx").read_bytes() == b"existing artifact"
    assert Path(result.artifact_path).is_file()
