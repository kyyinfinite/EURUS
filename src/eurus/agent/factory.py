from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from eurus import paths
from eurus.agent.coder_tools import coder_delegation_tools
from eurus.agent.context import ExtractiveContextEngine, LLMSummarizingContextEngine
from eurus.agent.delegation import delegation_tools
from eurus.agent.events import AgentEvent
from eurus.agent.loop import Agent, Approver, IterationBudget
from eurus.agent.planner import TaskPlanner
from eurus.agent.prompt import build_system_prompt_tiers
from eurus.agent.structured_planner import StructuredPlanner
from eurus.agent.team_tools import document_team_tools
from eurus.agent.verification import RuntimeVerificationService
from eurus.coding.project_index import render_project_index_cached
from eurus.config import Config
from eurus.execution.docker import resolve_seccomp_profile
from eurus.execution.envelope import (
    ExecutionEnvelope,
    IsolationBackend,
    NetworkProfile,
    ResourceBudget,
)
from eurus.execution.host import HostSandbox
from eurus.execution.sandbox import ContainerSandbox, Sandbox
from eurus.learning import LessonStore, Reflector
from eurus.learning.skills import SkillLearner
from eurus.memory.manager import MemoryManager, PromptMemory
from eurus.providers.factory import build_auxiliary_router
from eurus.providers.router import Router
from eurus.runtime.instrumentation import span
from eurus.security.journal import AuditJournal
from eurus.skills.tools import skill_tools
from eurus.tools.base import ToolContext, ToolRegistry
from eurus.tools.builtin import default_registry
from eurus.tools.shell import shell_tool
from eurus.workspace import WorkspaceManager

log = logging.getLogger(__name__)


def _execution_journal() -> AuditJournal:
    home = paths.home_manager()
    home.ensure_dir(home.security_dir)
    return AuditJournal(home.security_dir / "execution.jsonl")


def _build_envelope(config: Config, root: Path) -> ExecutionEnvelope:
    if config.sandbox.backend == "host":
        backend = IsolationBackend.HOST
        profile = NetworkProfile.OFFLINE
    else:
        backend = IsolationBackend.CONTAINER
        profile = NetworkProfile.WEB_READ if config.sandbox.network else NetworkProfile.OFFLINE
    budget = ResourceBudget(
        memory_mb=config.sandbox.memory_mb,
        cpu_limit=config.sandbox.cpu_limit,
        pids_limit=config.sandbox.pids_limit,
        timeout_seconds=config.tools.timeout_seconds,
        max_output_bytes=config.tools.max_output_chars,
    )
    return ExecutionEnvelope.authorize(
        workspace_root=str(root),
        backend=backend,
        network_profile=profile,
        budget=budget,
    )


def build_agent(
    config: Config,
    router: Router,
    *,
    registry: ToolRegistry | None = None,
    approver: Approver | None = None,
    workspace: Path | None = None,
    stream: bool = True,
    shell: bool = True,
    lessons: bool = False,
    learn: bool = False,
    learn_background: bool = False,
    skill_learning: bool | None = None,
    background_event_sink: Callable[[AgentEvent], object] | None = None,
    event_recorder: Callable[[AgentEvent], object] | None = None,
    verify: bool | None = None,
    plan: bool | None = None,
) -> Agent:
    root = (workspace or Path(config.permissions.workspace)).expanduser().resolve()
    workspaces = WorkspaceManager(root)
    envelope = _build_envelope(config, root)
    context = ToolContext(
        workspace=root,
        workspaces=workspaces,
        max_output=config.tools.max_output_chars,
        timeout=config.tools.timeout_seconds,
        envelope=envelope,
    )
    auxiliary_router = build_auxiliary_router(config)
    tools = (
        registry
        if registry is not None
        else default_registry(
            include_capability_tools=True,
            include_workflow_tools=True,
            memory_router=auxiliary_router,
            browser_settings=config.browser,
        )
    )
    from eurus.browser.integration import ensure_browser_tools

    ensure_browser_tools(tools, config.browser, workspace=root)
    sandbox: Sandbox | None = None
    if shell and "shell" not in tools:
        journal = _execution_journal()
        if config.sandbox.backend == "host":
            sandbox = HostSandbox(root)
            log.warning("sandbox backend is 'host': commands run on this device without isolation")
            journal.record(
                "execution.host_mode",
                workspace=str(root),
                isolated=False,
            )
        else:
            sandbox = ContainerSandbox(
                root,
                runtime=config.sandbox.runtime,
                image=config.sandbox.image,
                memory_mb=config.sandbox.memory_mb,
                cpu_limit=config.sandbox.cpu_limit,
                pids_limit=config.sandbox.pids_limit,
                network_enabled=config.sandbox.network,
                user=config.sandbox.user,
                image_allowlist=config.sandbox.image_allowlist,
                digest_pin_required=config.sandbox.digest_pin_required,
                seccomp_profile=resolve_seccomp_profile(
                    config.sandbox.seccomp_profile,
                    system_dir=paths.home_manager().system_dir,
                ),
                apparmor_profile=config.sandbox.apparmor_profile,
                security_event=lambda event, fields: journal.record(event, **fields),
            )
        tools.register(shell_tool(sandbox))
    for tool in [
        *skill_tools(),
        *document_team_tools(router, tools),
        *delegation_tools(router, tools),
        *coder_delegation_tools(router, tools),
    ]:
        if tool.name not in tools:
            tools.register(tool)
    settings = config.learning
    store = LessonStore(
        paths.database_path(),
        limit=settings.lessons_in_prompt,
        max_lessons=settings.max_lessons,
    )
    reflect = learn and settings.mode != "off"
    skills_on = (
        learn
        and settings.mode != "off"
        and (skill_learning if skill_learning is not None else True)
    )
    with span("build_agent.render_system_tiers"):
        stable_prompt, context_prompt, volatile_prompt = build_system_prompt_tiers(
            root, config.agent.system_prompt
        )
    if root.is_dir():
        try:
            with span("render_project_index"):
                project_context = render_project_index_cached(root)
        except (OSError, ValueError):
            project_context = ""
        if project_context and len(project_context) <= 6_000:
            context_prompt = f"{context_prompt}\n\n{project_context}"
    context_engine = (
        LLMSummarizingContextEngine(auxiliary_router, keep_recent=20)
        if auxiliary_router is not None
        else ExtractiveContextEngine(keep_recent=20)
    )
    agent = Agent(
        router,
        tools,
        context,
        system_prompt=stable_prompt,
        context_prompt=context_prompt,
        volatile_prompt=volatile_prompt,
        iteration_budget=IterationBudget(config.agent.iteration_budget),
        request_timeout=config.network.timeout_seconds,
        approver=approver,
        stream=stream,
        max_parallel_tools=config.tools.max_parallel_tools,
        context_tokens=config.agent.context_tokens,
        context_engine=context_engine,
        lessons=PromptMemory(paths.database_path(), store, scope=str(root)),
        memory_manager=(
            MemoryManager(
                paths.database_path(),
                max_memories=config.memory.max_memories,
                retention_days=config.memory.retention_days,
                max_content_chars=config.memory.max_content_chars,
            )
            if config.memory.enabled
            else None
        ),
        memory_scope=str(root),
        learner=Reflector(router, store, settings.mode) if reflect else None,
        skill_learner=SkillLearner(auxiliary_router or router) if skills_on else None,
        learn_in_background=learn_background,
        background_event_sink=background_event_sink,
        event_recorder=event_recorder,
    )
    if sandbox is not None:
        agent.sandbox = sandbox
    verification_on = config.verification.enabled if verify is None else verify
    if verification_on:
        service = RuntimeVerificationService(
            workspace=root,
            transcript=lambda: agent.history,
            auxiliary=auxiliary_router,
            model_judge=config.verification.model_judge,
        )
        agent.completion_verifier = service.verify_completion
        agent.step_verifier = service.verify_step
    planning_on = config.planning.model_planning if plan is None else plan
    if planning_on and auxiliary_router is not None:
        agent.planner = StructuredPlanner(
            auxiliary_router,
            fallback=TaskPlanner(max_steps=config.planning.max_steps),
            max_steps=config.planning.max_steps,
        )
    else:
        agent.planner = TaskPlanner(max_steps=config.planning.max_steps)
    return agent
