from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from eurus.coding.paths import resolve_workspace_path
from eurus.office.config import OfficeConfig
from eurus.office.errors import OfficeError
from eurus.office.models.review import FinalArtifactPackage
from eurus.office.models.task import OfficeFormat, OfficeTask, utc_now
from eurus.office.qa.scoring import AcceptanceThresholds
from eurus.office.renderers.base import verify_artifact_file
from eurus.office.specialist.budget import BudgetLimits
from eurus.office.specialist.manager import OfficeSpecialist
from eurus.office.storage import JobWorkspace, brief_from_payload, create_job_id
from eurus.providers.router import Router
from eurus.tools.base import MutationVerification, Tool, ToolContext, ToolRegistry

MAX_CONSTRAINT_CHARS = 500
MAX_OUTPUT_PATH_CHARS = 400
OFFICE_DEADLINE_MARGIN_SECONDS = 5.0
MIN_SPECIALIST_DEADLINE_SECONDS = 30.0

TOOL_NAME = "office_specialist"

TOOL_DESCRIPTION = (
    "Delegate office document production to the Eurus Office Specialist. The specialist runs a "
    "bounded multi-agent department (client analysis, research, design, production, structural "
    "QA, visual QA, critic review, client acceptance) and returns a professionally rendered, "
    "QA-verified Word, Excel, or PowerPoint artifact. Use this tool for any request to create "
    "or substantially rebuild a .docx, .xlsx, or .pptx file. Requires approval."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": ["word", "excel", "powerpoint"]},
        "objective": {"type": "string", "minLength": 1, "maxLength": 12000},
        "output_path": {"type": "string", "maxLength": 400},
        "audience": {"type": "string", "maxLength": 500},
        "tone": {"type": "string", "maxLength": 500},
        "language": {"type": "string", "maxLength": 12},
        "style": {"type": "string", "maxLength": 200},
        "constraints": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 24,
        },
        "source_paths": {
            "type": "array",
            "items": {"type": "string", "maxLength": 400},
            "maxItems": 20,
        },
        "include_toc": {"type": "boolean"},
        "external_research": {"type": "boolean"},
        "deadline_seconds": {"type": "number", "minimum": 30, "maximum": 7200},
    },
    "required": ["format", "objective"],
    "additionalProperties": False,
}

FORMAT_ALIASES = {
    "word": OfficeFormat.DOCX,
    "docx": OfficeFormat.DOCX,
    "excel": OfficeFormat.XLSX,
    "xlsx": OfficeFormat.XLSX,
    "powerpoint": OfficeFormat.PPTX,
    "pptx": OfficeFormat.PPTX,
}

log = logging.getLogger("eurus.office.specialist")


def specialist_deadline_seconds(
    arguments: dict[str, Any], context: ToolContext, config: OfficeConfig
) -> float:
    raw_deadline = arguments.get("deadline_seconds")
    requested = float(raw_deadline) if raw_deadline is not None else config.deadline_seconds
    remaining = context.remaining_seconds()
    if remaining is None:
        return requested
    allowance = remaining - OFFICE_DEADLINE_MARGIN_SECONDS
    if allowance < MIN_SPECIALIST_DEADLINE_SECONDS:
        raise OfficeError(
            f"only {remaining:.0f}s of the agent execution budget remain; the office "
            f"department requires at least {MIN_SPECIALIST_DEADLINE_SECONDS:.0f}s plus a "
            f"{OFFICE_DEADLINE_MARGIN_SECONDS:.0f}s completion margin"
        )
    return min(requested, allowance)


def resolve_delivered_path(output_path: str, fmt: OfficeFormat, workspace_root: Path) -> Path:
    relative = output_path.strip()
    if not relative:
        raise OfficeError("output path is empty")
    target = resolve_workspace_path(workspace_root, relative, label="output path")
    if target.suffix.lower() != fmt.extension:
        target = target.with_suffix(fmt.extension)
    return target


async def verify_office_mutation(
    arguments: dict[str, Any], context: ToolContext
) -> MutationVerification:
    raw_format = str(arguments.get("format", "")).strip().lower()
    fmt = FORMAT_ALIASES.get(raw_format)
    if fmt is None:
        return MutationVerification("unknown", "arguments do not identify a document format")
    raw_output = str(arguments.get("output_path", "")).strip()
    if not raw_output:
        return MutationVerification(
            "unknown", "no explicit output_path was provided; the delivered name is not predictable"
        )
    try:
        target = resolve_delivered_path(raw_output, fmt, context.workspace)
    except Exception as exc:
        return MutationVerification("unknown", f"output path could not be resolved: {exc}")
    verified, detail = verify_artifact_file(target, fmt.value)
    if verified:
        return MutationVerification("completed", f"{target}: {detail}")
    if not target.exists():
        return MutationVerification("absent", f"{target} was not created")
    return MutationVerification("unknown", f"{target}: {detail}")


def office_specialist_tool(
    router: Router,
    parent_registry: ToolRegistry,
    config: OfficeConfig | None = None,
) -> Tool:
    office_config = config or OfficeConfig()

    async def handler(arguments: dict[str, Any], context: ToolContext) -> str:
        if not office_config.enabled:
            raise OfficeError(
                "the office department is disabled in configuration ([office] enabled)"
            )
        raw_format = str(arguments.get("format", "")).strip().lower()
        fmt = FORMAT_ALIASES.get(raw_format)
        if fmt is None:
            raise OfficeError("format must be one of: word, excel, powerpoint")
        payload = {
            "objective": str(arguments.get("objective", "")).strip(),
            "format": fmt.value,
            "audience": str(arguments.get("audience", "")).strip()[:500],
            "tone": str(arguments.get("tone", "")).strip()[:500],
            "language": str(arguments.get("language", "en")).strip()[:12] or "en",
            "style": str(arguments.get("style", "")).strip()[:200],
            "constraints": [
                str(item).strip()[:MAX_CONSTRAINT_CHARS]
                for item in arguments.get("constraints", [])
                if str(item).strip()
            ][:24],
            "source_paths": [
                str(item).strip()[:400]
                for item in arguments.get("source_paths", [])
                if str(item).strip()
            ][:20],
            "output_path": str(arguments.get("output_path", "")).strip()[:MAX_OUTPUT_PATH_CHARS],
            "include_toc": bool(arguments.get("include_toc", True)),
            "external_research": bool(arguments.get("external_research", False)),
            "deadline_seconds": arguments.get("deadline_seconds"),
        }
        if not payload["objective"]:
            raise OfficeError("objective must not be empty")
        brief = brief_from_payload(payload)
        deadline = specialist_deadline_seconds(arguments, context, office_config)
        brief = type(brief)(
            objective=brief.objective,
            format=brief.format,
            audience=brief.audience,
            tone=brief.tone,
            language=brief.language,
            style_hint=brief.style_hint,
            constraints=brief.constraints,
            source_paths=brief.source_paths,
            output_path=brief.output_path,
            include_toc=brief.include_toc,
            external_research=brief.external_research,
            deadline_seconds=deadline,
        )
        task = OfficeTask(
            task_id=f"task-{create_job_id()}",
            job_id=create_job_id(),
            brief=brief,
            created_at=utc_now(),
        )
        remaining = context.remaining_seconds()
        log.info(
            "[office_specialist] execution_started correlation_id=%s effective_timeout=%.0fs"
            " user_deadline=%s",
            task.job_id,
            deadline,
            f"{remaining:.0f}s" if remaining is not None else "none",
        )
        workspace = JobWorkspace.create(task.job_id)
        workspace.copy_source_files(brief.source_paths, context.workspace)
        limits = BudgetLimits(
            max_total_model_calls=office_config.max_model_calls,
            max_revision_cycles=office_config.max_revision_cycles,
            max_workers=office_config.max_workers,
            deadline_seconds=deadline,
        )
        thresholds = AcceptanceThresholds(
            content_min=office_config.content_score_min,
            visual_min=office_config.visual_score_min,
            structural_min=office_config.structural_score_min,
        )
        specialist = OfficeSpecialist(
            router,
            context,
            task,
            parent_registry=parent_registry,
            workspace=workspace,
            limits=limits,
            thresholds=thresholds,
            allow_research_delegation=office_config.allow_research_delegation,
        )
        try:
            result: FinalArtifactPackage = await specialist.run()
        except asyncio.CancelledError:
            log.warning(
                "[office_specialist] execution_cancelled correlation_id=%s stage=%s",
                task.job_id,
                specialist.state.stage.value,
            )
            raise
        except Exception as exc:
            log.error(
                "[office_specialist] execution_failed correlation_id=%s stage=%s error_type=%s",
                task.job_id,
                specialist.state.stage.value,
                type(exc).__name__,
            )
            raise
        log.info(
            "[office_specialist] execution_completed correlation_id=%s output_verified=true"
            " artifact=%s",
            task.job_id,
            result.artifact_path,
        )
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    return Tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        parameters=TOOL_PARAMETERS,
        handler=handler,
        mutates=True,
        requires_approval=True,
        parallel_safe=False,
        interactive=False,
        path_keys=("output_path",),
        timeout_seconds=office_config.tool_timeout_seconds,
        verify_mutation=verify_office_mutation,
    )


def office_specialist_tools(
    router: Router,
    parent_registry: ToolRegistry,
    config: OfficeConfig | None = None,
) -> list[Tool]:
    return [office_specialist_tool(router, parent_registry, config)]


def run_specialist_sync(specialist: OfficeSpecialist) -> FinalArtifactPackage:
    return asyncio.run(specialist.run())
