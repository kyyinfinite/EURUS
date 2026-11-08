from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from eurus.coding.paths import resolve_workspace_path
from eurus.office.design.layouts import SlideLayoutModel, plan_presentation
from eurus.office.errors import OfficeError, OfficeValidationError
from eurus.office.models.document import (
    BibliographyBlock,
    CalloutBlock,
    DocumentMetadata,
    DocumentSpec,
    HeadingBlock,
    ListItemBlock,
    ParagraphBlock,
    QuoteBlock,
    TableBlock,
    TimelineBlock,
    TwoColumnBlock,
)
from eurus.office.models.presentation import DESIGN_DIRECTIONS, PresentationSpec
from eurus.office.models.review import (
    AcceptanceReport,
    DefectReport,
    FinalArtifactPackage,
    QAReport,
)
from eurus.office.models.task import (
    AgentAssignment,
    AgentRole,
    DesignSpecification,
    JobStage,
    OfficeBrief,
    OfficeFormat,
    OfficeJobState,
    OfficeTask,
    QualityScores,
    ResolutionStatus,
    StageRecord,
    StageStatus,
)
from eurus.office.models.workbook import WorkbookSpec
from eurus.office.qa.scoring import (
    AcceptanceDecision,
    AcceptanceThresholds,
    evaluate_acceptance,
    merge_defects,
)
from eurus.office.qa.structural import validate_structure
from eurus.office.qa.visual import inspect_visual
from eurus.office.renderers.base import (
    PDF_CONVERSION_TIMEOUT_SECONDS,
    convert_to_pdf,
    detect_capabilities,
    verify_artifact_file,
)
from eurus.office.renderers.excel import render_workbook
from eurus.office.renderers.html import build_preview
from eurus.office.renderers.word import render_document
from eurus.office.specialist.agents import (
    AcceptanceOutcome,
    ArtifactSpecification,
    parse_acceptance,
    parse_artifact,
    parse_client_analysis,
    parse_defects,
    parse_design,
    parse_model_json,
    parse_research,
)
from eurus.office.specialist.budget import BudgetLimits, BudgetTracker
from eurus.office.specialist.dispatcher import RoleDispatcher
from eurus.office.specialist.prompts import (
    ROLE_PROMPTS,
    client_acceptance_prompt,
    client_brief_prompt,
    critic_prompt,
    design_prompt,
    production_prompt,
    research_prompt,
    schema_for_format,
)
from eurus.office.specialist.workflow import is_terminal, validate_transition
from eurus.office.storage import JobWorkspace
from eurus.providers.router import Router
from eurus.tools.base import ToolContext, ToolRegistry

MAX_SOURCE_CHARS = 14_000
MAX_SOURCE_FILE_CHARS = 6_000
DEFAULT_MAX_REVISIONS = 3

log = logging.getLogger("eurus.office.specialist")


class OfficeSpecialist:
    def __init__(
        self,
        router: Router,
        context: ToolContext,
        task: OfficeTask,
        *,
        parent_registry: ToolRegistry | None = None,
        workspace: JobWorkspace | None = None,
        limits: BudgetLimits | None = None,
        thresholds: AcceptanceThresholds | None = None,
        allow_research_delegation: bool = True,
    ) -> None:
        self.router = router
        self.context = context
        self.task = task
        self.brief = task.brief
        self.workspace = workspace or JobWorkspace.create(task.job_id)
        self.limits = limits or BudgetLimits(deadline_seconds=task.brief.deadline_seconds)
        self.budget = BudgetTracker(self.limits)
        self.thresholds = thresholds or AcceptanceThresholds()
        self.allow_research_delegation = allow_research_delegation
        self.dispatcher = RoleDispatcher(router, parent_registry, context, self.budget)
        self.state = OfficeJobState(job_id=task.job_id, task_id=task.task_id, brief=self.brief)
        self.analysis: dict[str, Any] = {}
        self.research: Any = None
        self.design: Any = None
        self.artifact: ArtifactSpecification | None = None
        self._pdf_path: Path | None = None
        self.capabilities = detect_capabilities()

    async def run(self) -> FinalArtifactPackage:
        self.workspace.save_task(self.task)
        self._save_state()
        try:
            await self._stage_received()
            await self._stage_brief_analysis()
            await self._stage_team_planning()
            await self._stage_research()
            await self._stage_design()
            await self._produce_cycle(revision_instructions=())
            while not await self._decision():
                await self._stage_revision()
                await self._produce_cycle(revision_instructions=self._revision_instructions())
            await self._stage_finalization()
            return self._package()
        except asyncio.CancelledError:
            self._transition(JobStage.CANCELLED, "job cancelled")
            self._save_state()
            raise
        except _QualityLimit as exc:
            if self.state.stage is not JobStage.QUALITY_LIMIT_REACHED:
                self._transition(JobStage.QUALITY_LIMIT_REACHED, str(exc)[:800])
            self._save_state()
            if self.artifact is not None:
                try:
                    await self._finalize(
                        quality_status="rejected",
                        status="quality_limit_reached",
                        summary=str(exc)[:4000],
                    )
                    return self._package()
                except Exception as finalize_exc:
                    raise OfficeError(
                        f"quality limit reached and finalization failed: {finalize_exc}"
                    ) from finalize_exc
            raise OfficeError(str(exc)) from exc
        except OfficeError as exc:
            if not is_terminal(self.state.stage):
                self._transition(JobStage.FAILED, str(exc)[:800])
            self._save_state()
            raise OfficeError(str(exc)) from exc
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if not is_terminal(self.state.stage):
                self._transition(JobStage.FAILED, detail[:800])
            self._save_state()
            raise OfficeError(f"office job failed: {detail}") from exc

    async def _stage_received(self) -> None:
        self.state = replace(
            self.state,
            stage_history=(
                *self.state.stage_history,
                StageRecord(
                    stage=JobStage.RECEIVED, status=StageStatus.RUNNING, detail="task accepted"
                ),
            ),
        )
        self.workspace.append_event(
            "stage.entered", {"stage": "received", "detail": "task accepted"}
        )
        self._save_state()
        self._finish_stage(StageStatus.PASSED, "task persisted")

    async def _stage_brief_analysis(self) -> None:
        self._transition(JobStage.BRIEF_ANALYSIS)
        try:
            self.analysis = await self.dispatcher.ask_role(
                AgentRole.CLIENT,
                ROLE_PROMPTS["client"],
                client_brief_prompt(self.brief),
                parser=_client_analysis_parser,
                max_tokens=1600,
                temperature=0.2,
            )
        except OfficeError:
            self._finish_stage(
                StageStatus.FAILED,
                "client agent could not produce a usable brief analysis",
            )
            self.state = self.state.with_warnings(
                ("client brief analysis failed; the specialist continues with the raw brief",)
            )
            self._save_state()
            self._finish_stage(StageStatus.PASSED, "continued with the raw brief")
            return
        language = str(self.analysis.get("language", self.brief.language))[:12]
        self.brief = replace(
            self.brief,
            audience=str(self.analysis.get("audience", ""))[:500] or self.brief.audience,
            tone=str(self.analysis.get("tone", ""))[:500] or self.brief.tone,
            style_hint=str(self.analysis.get("style", ""))[:200] or self.brief.style_hint,
            language=language or self.brief.language,
        )
        self.task = replace(self.task, brief=self.brief)
        self.workspace.save_task(self.task)
        self._finish_stage(
            StageStatus.PASSED,
            f"requirements: {len(self.analysis.get('requirements', ()))} recorded",
        )

    async def _stage_team_planning(self) -> None:
        self._transition(JobStage.TEAM_PLANNING)
        assignments = self._build_assignments()
        plan = {
            "job_id": self.task.job_id,
            "strategy": (
                "research from supplied materials only"
                if not self._research_delegation_planned()
                else "research delegation with read-only tools enabled"
            ),
            "assignments": [assignment.to_dict() for assignment in assignments],
            "revision_budget": self.limits.max_revision_cycles,
            "thresholds": self.thresholds.to_dict(),
            "capabilities": {
                "office_to_pdf": self.capabilities.office_to_pdf,
                "pdf_to_images": self.capabilities.pdf_to_images,
                "html_raster": self.capabilities.html_raster,
                "missing": list(self.capabilities.missing()),
            },
        }
        self.workspace.save_plan(plan)
        self._finish_stage(StageStatus.PASSED, f"{len(assignments)} workers assigned")

    def _build_assignments(self) -> tuple[AgentAssignment, ...]:
        return (
            AgentAssignment(
                role=AgentRole.CLIENT,
                objective="Interpret the request and define acceptance criteria",
                budget_model_calls=self.limits.role_budgets[AgentRole.CLIENT],
            ),
            AgentAssignment(
                role=AgentRole.RESEARCHER,
                objective="Build the content package from materials or approved research",
                budget_model_calls=self.limits.role_budgets[AgentRole.RESEARCHER],
            ),
            AgentAssignment(
                role=AgentRole.DESIGNER,
                objective="Define the visual direction and layout plan",
                budget_model_calls=self.limits.role_budgets[AgentRole.DESIGNER],
            ),
            AgentAssignment(
                role=AgentRole.PRODUCER,
                objective="Emit the artifact specification and render the file",
                budget_model_calls=self.limits.role_budgets[AgentRole.PRODUCER],
            ),
            AgentAssignment(
                role=AgentRole.CRITIC,
                objective="Independent content review and defect reporting",
                budget_model_calls=self.limits.role_budgets[AgentRole.CRITIC],
            ),
            AgentAssignment(
                role=AgentRole.VISUAL_QA,
                objective="Inspect rendered pages for visual defects",
                budget_model_calls=self.limits.role_budgets[AgentRole.VISUAL_QA],
            ),
        )

    def _research_delegation_planned(self) -> bool:
        return self.allow_research_delegation and self.brief.external_research

    async def _stage_research(self) -> None:
        self._transition(JobStage.RESEARCH)
        materials = self._collect_materials()
        questions = tuple(self.analysis.get("research_questions", ()))
        needed = bool(self.analysis.get("research_needed", False)) or self.brief.external_research
        research_payload: dict[str, Any] | None = None
        if self._research_delegation_planned() and needed:
            research_payload = await self._delegate_research(materials, questions)
        if research_payload is None:
            research_payload = await self._llm_research(materials, questions)
        self.research = parse_research(self.brief.objective, research_payload)
        self.workspace.write_text(
            self.workspace.reviews_dir / "research-package.json",
            json_dump(self.research.to_dict()),
        )
        self._finish_stage(
            StageStatus.PASSED,
            f"{len(self.research.facts)} facts, {len(self.research.outline)} outline points",
        )

    async def _delegate_research(
        self, materials: str, questions: tuple[str, ...]
    ) -> dict[str, Any] | None:
        questions_text = "\n".join(f"- {question}" for question in questions) or "- none"
        task_text = (
            f"Document objective: {self.brief.objective}\n"
            f"Language: {self.brief.language}\n\nResearch questions:\n{questions_text}\n\n"
            f"Supplied materials (read-only):\n{materials[:8000]}\n\n"
            "Use the read-only tools to verify or extend the facts where possible, then reply "
            "with the JSON PACKAGE as instructed."
        )
        try:
            text = await self.dispatcher.run_research_subagent(
                task_text,
                deadline_seconds=self.budget.remaining_seconds(),
            )
        except OfficeError as exc:
            self.state = self.state.with_warnings((f"research delegation failed: {exc}",))
            return None
        payload = self.dispatcher.extract_json_package(text)
        if payload is None:
            self.state = self.state.with_warnings(
                ("research subagent returned no parsable JSON package",)
            )
            return None
        return payload

    async def _llm_research(self, materials: str, questions: tuple[str, ...]) -> dict[str, Any]:
        payload = await self.dispatcher.ask_role(
            AgentRole.RESEARCHER,
            ROLE_PROMPTS["researcher"],
            research_prompt(self.brief, materials, questions),
            parser=parse_model_json,
            max_tokens=3000,
            temperature=0.2,
        )
        research: dict[str, Any] = payload
        return research

    def _collect_materials(self) -> str:
        parts: list[str] = []
        total = 0
        for source in sorted(self.workspace.source_dir.iterdir()):
            if not source.is_file() or total >= MAX_SOURCE_CHARS:
                break
            text = _read_text_file(source)[:MAX_SOURCE_FILE_CHARS]
            if not text:
                continue
            parts.append(f"--- {source.name} ---\n{text}")
            total += len(text)
        return "\n\n".join(parts)[:MAX_SOURCE_CHARS]

    async def _stage_design(self) -> None:
        self._transition(JobStage.DESIGN)
        outline = tuple(self.research.outline) if self.research is not None else ()
        slide_hint = 0
        if self.brief.format is OfficeFormat.PPTX:
            slide_hint = max(6, min(16, len(outline) + 5))
        self.design = await self.dispatcher.ask_role(
            AgentRole.DESIGNER,
            ROLE_PROMPTS["designer"],
            design_prompt(self.brief, outline, str(self.analysis.get("style", "")), slide_hint),
            parser=_design_parser,
            max_tokens=1600,
            temperature=0.3,
        )
        self._finish_stage(
            StageStatus.PASSED,
            f"direction={self.design.direction} style={self.design.document_style}",
        )

    async def _produce_cycle(self, *, revision_instructions: tuple[str, ...]) -> None:
        await self._stage_production(revision_instructions)
        await self._stage_structural_qa()
        await self._stage_rendering()
        await self._stage_visual_qa()
        await self._stage_critic_review()
        await self._stage_client_acceptance()

    async def _stage_production(self, revision_instructions: tuple[str, ...]) -> None:
        self._transition(JobStage.PRODUCTION)
        research_json = json_dump(self.research.to_dict()) if self.research is not None else "{}"
        design_json = json_dump(self.design.to_dict()) if self.design is not None else "{}"
        payload = await self.dispatcher.ask_role(
            AgentRole.PRODUCER,
            ROLE_PROMPTS["producer"],
            production_prompt(
                self.brief,
                research_json,
                design_json,
                schema_for_format(self.brief.format),
                revision_instructions,
            ),
            parser=parse_model_json,
            max_tokens=8000,
            temperature=0.35,
        )

        def build(current_payload: dict[str, Any]) -> ArtifactSpecification:
            return parse_artifact(
                self.brief, current_payload, image_roots=(self.workspace.source_dir,)
            )

        try:
            self.artifact = build(payload)
        except OfficeValidationError as exc:
            payload = await self.dispatcher.ask_role(
                AgentRole.PRODUCER,
                ROLE_PROMPTS["producer"],
                (
                    production_prompt(
                        self.brief,
                        research_json,
                        design_json,
                        schema_for_format(self.brief.format),
                        revision_instructions,
                    )
                    + f"\n\nYour previous specification was rejected by the validator: {exc}. "
                    "Return the corrected specification as one JSON object only."
                ),
                parser=parse_model_json,
                max_tokens=8000,
                temperature=0.15,
            )
            try:
                self.artifact = build(payload)
            except OfficeValidationError as second_exc:
                self._finish_stage(
                    StageStatus.FAILED, f"invalid artifact specification: {second_exc}"
                )
                raise OfficeError(
                    f"producer output failed validation twice: {second_exc}"
                ) from second_exc
        if (
            self.design is not None
            and self.brief.format is OfficeFormat.PPTX
            and self.artifact is not None
            and self.artifact.presentation is not None
            and self.design.direction in DESIGN_DIRECTIONS
            and self.artifact.presentation.direction != self.design.direction
        ):
            self.artifact = ArtifactSpecification(
                format=OfficeFormat.PPTX,
                presentation=replace(self.artifact.presentation, direction=self.design.direction),
            )
        self._finish_stage(
            StageStatus.PASSED,
            _artifact_summary(self.artifact),
        )

    async def _stage_structural_qa(self) -> None:
        self._transition(JobStage.STRUCTURAL_QA)
        artifact_path = await self._render_to_staging()
        defects, score = validate_structure(
            self.brief.format.value, artifact_path, self.task.job_id
        )
        self._structural_defects = defects
        self._structural_score = score
        critical = [defect for defect in defects if defect.severity.value == "critical"]
        self._finish_stage(
            StageStatus.PASSED if not critical else StageStatus.FAILED,
            f"structural score {score}; {len(defects)} defect(s)",
        )

    async def _stage_rendering(self) -> None:
        self._transition(JobStage.RENDERING)
        artifact_path = self._staged_artifact_path()
        try:
            pdf_path = await convert_to_pdf(
                artifact_path,
                self.workspace.renders_dir,
                timeout=self._pdf_timeout_seconds(),
            )
            self._pdf_path = pdf_path
            self._finish_stage(StageStatus.PASSED, f"pdf rendered: {pdf_path.name}")
        except (RuntimeError, OSError) as exc:
            self._pdf_path = None
            self.state = self.state.with_warnings((f"pdf rendering unavailable: {exc}",))
            self._save_state()
            self._finish_stage(StageStatus.FAILED, f"pdf rendering unavailable: {exc}")

    def _pdf_timeout_seconds(self) -> float:
        remaining = self.budget.remaining_seconds()
        if remaining is None:
            return PDF_CONVERSION_TIMEOUT_SECONDS
        return max(1.0, min(PDF_CONVERSION_TIMEOUT_SECONDS, remaining))

    async def _stage_visual_qa(self) -> None:
        self._transition(JobStage.VISUAL_QA)
        artifact_path = self._staged_artifact_path()
        inspection = await inspect_visual(
            self.brief.format.value,
            artifact_path,
            self.workspace.renders_dir,
            self.task.job_id,
            router=self.router,
            use_vision=True,
            pdf_path=getattr(self, "_pdf_path", None),
        )
        self._visual_defects = inspection.defects
        self._visual_score = inspection.score
        self._visual_mode = inspection.mode
        self._visual_notes = inspection.notes
        warnings: list[str] = []
        if inspection.mode == "unavailable":
            warnings.append(
                f"visual QA could not render images:"
                f"{inspection.notes[0] if inspection.notes else 'unknown reason'}"
            )
        elif inspection.mode == "deterministic":
            warnings.append(
                "visual QA ran in deterministic mode; no vision-capable model is configured"
            )
        if warnings:
            self.state = self.state.with_warnings(tuple(warnings))
        self._save_state()
        self._finish_stage(
            StageStatus.PASSED,
            f"visual mode={inspection.mode} score={inspection.score} pages={inspection.page_count}",
        )

    async def _stage_critic_review(self) -> None:
        self._transition(JobStage.CRITIC_REVIEW)
        artifact_text = self._artifact_text()
        qa_findings = "\n".join(
            [
                f"- [{defect.severity.value}] {defect.layer}: {defect.description}"
                for defect in self._all_defects()
            ]
            or ["- no automated findings"]
        )
        try:
            payload = await self.dispatcher.ask_role(
                AgentRole.CRITIC,
                ROLE_PROMPTS["critic"],
                critic_prompt(
                    self.brief,
                    artifact_text,
                    qa_findings,
                    tuple(self.analysis.get("requirements", ())),
                ),
                parser=parse_model_json,
                max_tokens=2400,
                temperature=0.2,
            )
            defects, content_score, summary = parse_defects(payload)
            self._critic_defects = defects
            if content_score is not None:
                self._content_score = content_score
            self._critic_summary = summary
        except OfficeError as exc:
            self._critic_defects = ()
            self._critic_summary = f"critic review failed: {exc}"
            self.state = self.state.with_warnings((self._critic_summary,))
        self._save_state()
        self._finish_stage(
            StageStatus.PASSED,
            f"content score={getattr(self, '_content_score', None)};"
            f"{len(getattr(self, '_critic_defects', ()))} defect(s)",
        )

    async def _stage_client_acceptance(self) -> None:
        self._transition(JobStage.CLIENT_ACCEPTANCE)
        qa_summary = "\n".join(
            [
                f"structural score: {getattr(self, '_structural_score', None)}",
                f"visual score: {getattr(self, '_visual_score', None)} (mode"
                f"{getattr(self, '_visual_mode', 'unavailable')})",
                f"content score: {getattr(self, '_content_score', None)}",
                f"open defects: {len(self._open_defects())}",
            ]
        )
        try:
            outcome = await self.dispatcher.ask_role(
                AgentRole.CLIENT,
                ROLE_PROMPTS["client"],
                client_acceptance_prompt(
                    self.brief,
                    tuple(self.analysis.get("requirements", ())),
                    tuple(self.analysis.get("acceptance_criteria", ())),
                    self._artifact_text(),
                    qa_summary,
                ),
                parser=parse_client_outcome,
                max_tokens=1600,
                temperature=0.1,
            )
            self._acceptance = AcceptanceReport(
                job_id=self.task.job_id,
                task_id=self.task.task_id,
                accepted=outcome.accepted,
                content_score=outcome.content_score,
                reasons=outcome.reasons,
                missing_content=outcome.missing_content,
                client_summary=outcome.client_summary,
            )
            critic_score = getattr(self, "_content_score", None)
            self._content_score = (
                min(float(critic_score), outcome.content_score)
                if critic_score is not None
                else outcome.content_score
            )
        except OfficeError as exc:
            self._acceptance = AcceptanceReport(
                job_id=self.task.job_id,
                task_id=self.task.task_id,
                accepted=False,
                content_score=float(getattr(self, "_content_score", 0.0) or 0.0),
                reasons=(f"client acceptance failed: {exc}",),
                missing_content=(),
                client_summary="",
            )
        self._finish_stage(
            StageStatus.PASSED,
            f"accepted={self._acceptance.accepted}; score={self._acceptance.content_score}",
        )

    async def _decision(self) -> bool:
        qa_report = self._build_qa_report()
        decision = evaluate_acceptance(qa_report, self._acceptance, self.thresholds)
        self._last_decision = decision
        self._last_qa = qa_report
        self.state = self.state.with_quality(
            QualityScores(
                content=qa_report.content_score,
                visual=qa_report.visual_score,
                structural=qa_report.structural_score,
            )
        )
        self._write_qa_report(qa_report)
        if decision.accepted:
            return True
        if self.state.revision_count >= self.limits.max_revision_cycles:
            self._transition(JobStage.QUALITY_LIMIT_REACHED, "; ".join(decision.reasons[:3]))
            self._save_state()
            self.state = self.state.with_stage_finished(
                StageStatus.FAILED, "revision budget exhausted"
            )
            self._save_state()
            raise _QualityLimit("; ".join(decision.reasons[:4]) or "quality thresholds not met")
        self._pending_instructions = self._instructions_from(decision, qa_report)
        return False

    def _instructions_from(
        self, decision: AcceptanceDecision, qa_report: QAReport
    ) -> tuple[str, ...]:
        instructions = [f"ACCEPTANCE REASON: {reason}" for reason in decision.reasons[:8]]
        instructions.extend(
            f"MISSING CONTENT: {item}" for item in self._acceptance.missing_content[:8]
        )
        instructions.extend(
            f"DEFECT [{defect.severity.value}] {defect.artifact_location}: "
            f"{defect.description} Fix: {defect.recommended_fix}"
            f"{defect.recommended_fix}"
            for defect in qa_report.open_defects()[:16]
        )
        return tuple(instructions[:24])

    async def _stage_revision(self) -> None:
        self._transition(JobStage.REVISION)
        self.state = self.state.with_revision()
        self._save_state()
        self._finish_stage(
            StageStatus.PASSED,
            f"revision {self.state.revision_count}/{self.limits.max_revision_cycles} prepared with"
            f"{len(self._pending_instructions)} instructions",
        )

    async def _stage_finalization(self) -> None:
        self._transition(JobStage.FINALIZATION)
        decision: AcceptanceDecision = self._last_decision
        await self._finalize(
            quality_status=decision.quality_status,
            status="completed",
            summary=self._summary_text(),
        )
        self._transition(JobStage.COMPLETED, "artifact finalized")
        self._finish_stage(StageStatus.PASSED, "delivered")

    async def _finalize(self, *, quality_status: str, status: str, summary: str) -> None:
        artifact_path = self._staged_artifact_path()
        verified, detail = verify_artifact_file(artifact_path, self.brief.format.value)
        if not verified:
            self._finish_stage(StageStatus.FAILED, f"artifact verification failed: {detail}")
            raise OfficeError(f"staged artifact failed verification: {detail}")
        final_artifact = self.workspace.final_path(artifact_path.name)
        shutil.copyfile(artifact_path, final_artifact)
        preview_path = self._materialize_preview(final_artifact)
        qa_path = self.workspace.reviews_dir / "qa-report.json"
        if not qa_path.is_file() and getattr(self, "_last_qa", None) is not None:
            self._write_qa_report(self._last_qa)
        self.state = self.state.with_artifacts(
            artifact_path=str(final_artifact),
            preview_path=str(preview_path) if preview_path is not None else "",
            qa_report_path=str(qa_path),
        )
        if quality_status != "rejected":
            delivered = self._deliver_to_workspace(final_artifact)
            delivered_ok, delivered_detail = verify_artifact_file(
                delivered, self.brief.format.value
            )
            if not delivered_ok:
                raise OfficeError(f"delivered artifact failed verification: {delivered_detail}")
            self.state = self.state.with_artifacts(artifact_path=str(delivered))
            self.workspace.append_event(
                "output.verified",
                {
                    "artifact": delivered.name,
                    "bytes": delivered.stat().st_size,
                    "format": self.brief.format.value,
                },
            )
            log.info(
                "[office_specialist] output_verified=true correlation_id=%s artifact=%s",
                self.task.job_id,
                delivered,
            )
        self.state = self.state.with_warnings(tuple(self._pending_warnings()))
        self._save_state()
        self._result = FinalArtifactPackage(
            job_id=self.task.job_id,
            status=status,
            format=self.brief.format,
            quality_status=quality_status,
            artifact_path=self.state.artifact_path,
            preview_path=self.state.preview_path,
            qa_report_path=self.state.qa_report_path,
            revision_count=self.state.revision_count,
            summary=summary[:4000],
            warnings=self.state.warnings,
            scores=self.state.quality,
        )

    def _deliver_to_workspace(self, final_artifact: Path) -> Path:
        relative = self.brief.output_path.strip() or f"office-outputs/{final_artifact.name}"
        target = resolve_workspace_path(self.context.workspace, relative, label="output path")
        if target.suffix.lower() != self.brief.format.extension:
            target = target.with_suffix(self.brief.format.extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            stem, suffix = target.stem, target.suffix
            counter = 1
            while target.exists():
                target = target.with_name(f"{stem}-{counter}{suffix}")
                counter += 1
        temporary = target.with_name(f".{target.name}.tmp-{self.task.job_id}")
        try:
            shutil.copyfile(final_artifact, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _materialize_preview(self, final_artifact: Path) -> Path | None:
        try:
            if (
                self.brief.format is OfficeFormat.PPTX
                and self.artifact is not None
                and self.artifact.presentation is not None
            ):
                models = plan_presentation(self.artifact.presentation)
                preview = build_preview(self.artifact.presentation, models)
                preview_path = self.workspace.final_path(final_artifact.stem + ".preview.html")
                preview_path.write_text(preview.document, encoding="utf-8")
                return preview_path
            pages = sorted((self.workspace.renders_dir / "pages").glob("*.jpg"))
            if pages:
                preview_path = self.workspace.final_path(
                    f"{final_artifact.stem}-preview-page-1.jpg"
                )
                shutil.copyfile(pages[0], preview_path)
                return preview_path
        except Exception:
            return None
        return None

    def _pending_warnings(self) -> tuple[str, ...]:
        decision: AcceptanceDecision = self._last_decision
        return decision.warnings

    def _build_qa_report(self) -> QAReport:
        return QAReport(
            job_id=self.task.job_id,
            task_id=self.task.task_id,
            structural_score=getattr(self, "_structural_score", None),
            visual_score=getattr(self, "_visual_score", None),
            content_score=getattr(self, "_content_score", None),
            defects=self._all_defects(),
            visual_mode=getattr(self, "_visual_mode", "unavailable"),
            vision_model_used=getattr(self, "_visual_mode", "") == "model",
            passed=False,
            notes=tuple(getattr(self, "_visual_notes", ())),
        )

    def _all_defects(self) -> tuple[DefectReport, ...]:
        return merge_defects(
            getattr(self, "_structural_defects", ()),
            getattr(self, "_visual_defects", ()),
            getattr(self, "_critic_defects", ()),
        )

    def _open_defects(self) -> tuple[DefectReport, ...]:
        return tuple(
            defect
            for defect in self._all_defects()
            if defect.resolution_status not in {ResolutionStatus.FIXED, ResolutionStatus.VERIFIED}
        )

    def _revision_instructions(self) -> tuple[str, ...]:
        return getattr(self, "_pending_instructions", ())

    def _write_qa_report(self, qa_report: QAReport) -> None:
        acceptance_dict = (
            self._acceptance.to_dict() if getattr(self, "_acceptance", None) is not None else {}
        )
        decision_dict = (
            self._last_decision.to_dict()
            if getattr(self, "_last_decision", None) is not None
            else {}
        )
        payload = {
            "qa_report": qa_report.to_dict(),
            "client_acceptance": acceptance_dict,
            "decision": decision_dict,
            "budget": self.budget.snapshot(),
        }
        self.workspace.write_json(self.workspace.reviews_dir / "qa-report.json", payload)
        self.state = self.state.with_artifacts(
            qa_report_path=str(self.workspace.reviews_dir / "qa-report.json")
        )

    def _summary_text(self) -> str:
        decision: AcceptanceDecision = self._last_decision
        qa = self._last_qa
        parts = [
            f"{self.brief.format.label} artifact produced by the office department",
            f"revisions used: {self.state.revision_count}",
            f"scores: content={qa.content_score} visual={qa.visual_score} "
            f"structural={qa.structural_score}",
            f"visual inspection mode: {getattr(self, '_visual_mode', 'unavailable')}",
            f"open defects: {len(qa.open_defects())}",
            f"client summary: {self._acceptance.client_summary[:400]}"
            if self._acceptance.client_summary
            else "",
        ]
        if decision.warnings:
            parts.append("warnings: " + "; ".join(decision.warnings))
        return " | ".join(part for part in parts if part)

    def _package(self) -> FinalArtifactPackage:
        result: FinalArtifactPackage | None = getattr(self, "_result", None)
        if result is not None:
            return result
        raise OfficeError("office job ended without a result package")

    def _staged_artifact_path(self) -> Path:
        if self.artifact is None:
            raise OfficeError("no artifact has been produced")
        name = _artifact_filename(self.brief, self.artifact)
        return self.workspace.staging_path(name)

    async def _render_to_staging(self) -> Path:
        if self.artifact is None:
            raise OfficeError("no artifact specification to render")
        path = self._staged_artifact_path()
        try:
            if self.artifact.format is OfficeFormat.DOCX and self.artifact.document is not None:
                await asyncio.to_thread(render_document, self.artifact.document, path)
            elif self.artifact.format is OfficeFormat.XLSX and self.artifact.workbook is not None:
                await asyncio.to_thread(render_workbook, self.artifact.workbook, path)
            elif (
                self.artifact.format is OfficeFormat.PPTX and self.artifact.presentation is not None
            ):
                models = plan_presentation(self.artifact.presentation)
                preview = build_preview(self.artifact.presentation, models)
                preview_path = self.workspace.staging_path(path.stem + ".preview.html")
                preview_path.write_text(preview.document, encoding="utf-8")
                await render_presentation_async_safe(self.artifact.presentation, models, path)
            else:
                raise OfficeError("artifact specification does not match the requested format")
        except OfficeError:
            raise
        except Exception as exc:
            self._finish_stage(StageStatus.FAILED, f"rendering failed: {exc}")
            raise OfficeError(f"rendering failed: {type(exc).__name__}: {exc}") from exc
        return path

    def _artifact_text(self) -> str:
        if self.artifact is None:
            return ""
        return _extract_text(self.artifact)

    def _transition(self, stage: JobStage, detail: str = "") -> None:
        if not is_terminal(stage):
            self.budget.ensure_deadline()
        validate_transition(self.state.stage, stage)
        self.state = self.state.with_stage(stage, detail)
        self.workspace.append_event("stage.entered", {"stage": stage.value, "detail": detail[:400]})
        log.info(
            "[office_specialist] stage=%s correlation_id=%s remaining_budget=%s",
            stage.value,
            self.task.job_id,
            (
                f"{self.budget.remaining_seconds():.0f}s"
                if self.budget.remaining_seconds() is not None
                else "none"
            ),
        )
        self._save_state()

    def _finish_stage(self, status: StageStatus, detail: str) -> None:
        self.state = self.state.with_stage_finished(status, detail)
        self.workspace.append_event(
            "stage.finished", {"status": status.value, "detail": detail[:400]}
        )
        self._save_state()

    def _save_state(self) -> None:
        self.workspace.save_state(self.state)


class _QualityLimit(OfficeError):
    pass


def parse_client_outcome(text: str) -> AcceptanceOutcome:
    return parse_acceptance(parse_model_json(text))


def _client_analysis_parser(text: str) -> dict[str, Any]:
    return parse_client_analysis(parse_model_json(text))


def _design_parser(text: str) -> DesignSpecification:
    return parse_design(parse_model_json(text))


def json_dump(payload: dict[str, Any] | Any) -> str:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    return json.dumps(payload, ensure_ascii=False)


def _artifact_filename(brief: OfficeBrief, artifact: ArtifactSpecification) -> str:
    base = _slugify(_artifact_title(artifact)) or "eurus-office-artifact"
    return f"{base}{brief.format.extension}"


def _artifact_title(artifact: ArtifactSpecification) -> str:
    if artifact.document is not None:
        return artifact.document.metadata.title
    if artifact.presentation is not None:
        return artifact.presentation.title
    if artifact.workbook is not None:
        return artifact.workbook.title
    return "eurus-office-artifact"


def _slugify(text: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", " "} else "" for char in text.strip()
    )
    return "-".join(cleaned.split())[:80]


def _artifact_summary(artifact: ArtifactSpecification) -> str:
    if artifact.document is not None:
        return f"document: {len(artifact.document.blocks)} blocks, style={artifact.document.style}"
    if artifact.presentation is not None:
        return (
            f"presentation: {len(artifact.presentation.slides)} slides, "
            f"direction={artifact.presentation.direction}"
        )
    if artifact.workbook is not None:
        return f"workbook: {len(artifact.workbook.sheets)} sheets"
    return "unknown artifact"


def _read_text_file(path: Path) -> str:
    try:
        if path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            from eurus.coding.document_tools import _read_document

            return _read_document(path)
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _extract_text(artifact: ArtifactSpecification) -> str:
    if artifact.document is not None:
        return _document_text(artifact.document)
    if artifact.presentation is not None:
        return _presentation_text(artifact.presentation)
    if artifact.workbook is not None:
        return _workbook_text(artifact.workbook)
    return ""


def _document_text(spec: DocumentSpec) -> str:
    lines = [f"Title: {spec.metadata.title}", f"Subtitle: {spec.metadata.subtitle}"]
    for block in spec.blocks:
        if isinstance(block, HeadingBlock):
            lines.append(f"Heading: {block.text}")
        elif isinstance(block, ParagraphBlock):
            lines.append(f"Paragraph: {block.rich.plain}")
        elif isinstance(block, ListItemBlock):
            lines.append(f"Item: {block.text}")
        elif isinstance(block, TableBlock):
            lines.append("Table: " + " | ".join(block.headers))
            for row in block.rows[:20]:
                lines.append("  " + " | ".join(row))
        elif isinstance(block, TimelineBlock):
            for item in block.items:
                lines.append(f"Timeline {item.label}: {item.title} {item.description}")
        elif isinstance(block, CalloutBlock):
            lines.append(f"Callout: {block.title} {block.body}")
        elif isinstance(block, QuoteBlock):
            lines.append(f"Quote: {block.rich.plain} — {block.attribution}")
        elif isinstance(block, TwoColumnBlock):
            lines.append(
                "TwoColumn: "
                + _document_text(_spec_from_blocks(block.left))
                + " / "
                + _document_text(_spec_from_blocks(block.right))
            )
        elif isinstance(block, BibliographyBlock):
            lines.append("References: " + "; ".join(block.entries))
    return "\n".join(lines)


def _spec_from_blocks(blocks: tuple[Any, ...]) -> DocumentSpec:
    return DocumentSpec(
        metadata=DocumentMetadata(title="column"),
        blocks=tuple(blocks),
    )


def _presentation_text(spec: PresentationSpec) -> str:
    lines: list[str] = [f"Deck: {spec.title}", f"Subtitle: {spec.subtitle}"]
    for index, slide in enumerate(spec.slides, 1):
        lines.append(f"Slide {index} [{slide.layout}]: {slide.title}")
        if slide.subtitle:
            lines.append(f"  subtitle: {slide.subtitle}")
        for bullet in slide.bullets:
            lines.append(f"  - {bullet}")
        for body in slide.body:
            lines.append(f"  {body}")
        for metric in slide.metrics:
            lines.append(f"  metric {metric.label}: {metric.value} {metric.detail}")
        for entry in slide.timeline:
            lines.append(f"  timeline {entry.label}: {entry.title} {entry.description}")
        for step in slide.process:
            lines.append(f"  step: {step.title} {step.description}")
        for column in slide.comparison:
            lines.append(f"  column {column.title}: {'; '.join(column.points)}")
        if slide.chart is not None:
            lines.append(
                f"  chart {slide.chart.kind}: {slide.chart.title} "
                f"categories={list(slide.chart.categories)}"
            )
            for series in slide.chart.series:
                lines.append(f"    series {series.name}: {list(series.values)}")
        if slide.quote:
            lines.append(f"  quote: {slide.quote} — {slide.attribution}")
        if slide.callout:
            lines.append(f"  callout: {slide.callout}")
        for reference in slide.references:
            lines.append(f"  reference: {reference}")
        if slide.notes:
            lines.append(f"  notes: {slide.notes}")
    return "\n".join(lines)


def _workbook_text(spec: WorkbookSpec) -> str:
    lines = [f"Workbook: {spec.title}"]
    for sheet in spec.sheets:
        lines.append(f"Sheet {sheet.name} [{sheet.role}]")
        for table in sheet.tables:
            lines.append("  " + " | ".join(table.headers))
            for row in table.rows[:20]:
                lines.append("  " + " | ".join("" if cell is None else str(cell) for cell in row))
        for kpi in sheet.kpis:
            lines.append(f"  KPI {kpi.label}: {kpi.formula} {kpi.detail}")
        for chart in sheet.charts:
            lines.append(f"  chart {chart.kind}: {chart.title} {chart.categories_ref}")
        for formula_column in sheet.formula_columns:
            lines.append(f"  formula column {formula_column.header}: {formula_column.pattern}")
        for note in sheet.notes:
            lines.append(f"  note: {note}")
    return "\n".join(lines)


async def render_presentation_async_safe(
    spec: PresentationSpec, models: list[SlideLayoutModel], path: Path
) -> Path:
    from eurus.office.renderers.powerpoint import render_presentation_async

    return await render_presentation_async(spec, models, path)
