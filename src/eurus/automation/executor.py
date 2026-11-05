from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.automation.policies import TOOL_ALLOW_ALL
from eurus.config import Config
from eurus.mcp.runtime import MCPRuntime
from eurus.models import ToolCall
from eurus.providers.factory import build_router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.wiring import build_agent_engine_from_database, deny_remote_approval
from eurus.storage.database import Database
from eurus.storage.jobs import Job
from eurus.tools.base import Tool, ToolRegistry
from eurus.tools.builtin import default_registry

log = logging.getLogger(__name__)

__all__ = ["TOOL_ALLOW_ALL", "AgentJobExecutor", "AgentResources", "ScopedToolRegistry"]

SideEffectMarker = Callable[[], None]
SideEffectMarkerFactory = Callable[[Job], SideEffectMarker | None]


class JobExecutor(Protocol):
    async def execute(self, job: Job, context: ExecutionContext) -> str: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    execution_id: str
    job_id: str
    owner_id: str
    correlation_id: str
    mode: str
    budget: BudgetSpec
    allowed_tools: frozenset[str]
    blocked_tools: frozenset[str]
    policy_scope: str
    deadline_at: datetime | None
    attempts: int
    max_attempts: int
    goal_id: str | None = None
    schedule_id: str | None = None
    workflow_instance_id: str | None = None
    parent_execution_id: str | None = None
    checkpoint_ref: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")

    @classmethod
    def from_job(
        cls,
        job: Job,
        *,
        budget: BudgetSpec,
        allowed_tools: frozenset[str] = frozenset(),
        blocked_tools: frozenset[str] = frozenset(),
        policy_scope: str = "automation",
        deadline_at: datetime | None = None,
    ) -> ExecutionContext:
        payload: Mapping[str, Any] = job.payload if job.payload is not None else {}
        return cls(
            execution_id=job.job_id,
            job_id=job.job_id,
            owner_id=job.owner_id,
            correlation_id=job.correlation_id,
            mode=str(payload.get("mode", job.source)),
            budget=budget,
            allowed_tools=allowed_tools,
            blocked_tools=blocked_tools,
            policy_scope=policy_scope,
            deadline_at=deadline_at,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            goal_id=payload.get("goal_id"),
            schedule_id=job.schedule_id,
            workflow_instance_id=payload.get("workflow_instance_id"),
            parent_execution_id=payload.get("parent_execution_id"),
            checkpoint_ref=payload.get("checkpoint_ref"),
            payload=payload,
        )


ToolProvider = Callable[[Job, ExecutionContext], Sequence[Tool]]


class ScopedToolRegistry:
    def __init__(
        self, inner: ToolRegistry, *, allowed: frozenset[str], blocked: frozenset[str]
    ) -> None:
        self.inner = inner
        self.allowed = frozenset(allowed)
        self.blocked = frozenset(blocked)
        self.allow_all = TOOL_ALLOW_ALL in self.allowed
        self.deny_all = TOOL_ALLOW_ALL in self.blocked

    def _visible(self, name: str) -> bool:
        if name in self.blocked:
            return False
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        return name in self.allowed

    def register(self, tool: Tool) -> None:
        self.inner.register(tool)

    def unregister(self, name: str) -> Tool | None:
        return self.inner.unregister(name)

    def get(self, name: str) -> Tool | None:
        if not self._visible(name):
            return None
        return self.inner.get(name)

    def names(self) -> list[str]:
        return [name for name in self.inner.names() if self._visible(name)]

    def all(self) -> list[Tool]:
        return [tool for tool in self.inner.all() if self._visible(tool.name)]

    def specs(self) -> tuple[Any, ...]:
        return tuple(tool.spec() for tool in self.all())

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._visible(name) and name in self.inner

    async def execute(self, call: ToolCall, context: Any) -> Any:
        return await self.inner.execute(call, context)


class MutatingToolMarker:
    def __init__(self, inner: ToolRegistry, marker: SideEffectMarker) -> None:
        self.inner = inner
        self.marker = marker
        self.marked = False

    def _wrap(self, tool: Tool) -> Tool:
        if not tool.mutates:
            return tool
        marker = self.marker
        original = tool.handler

        async def marked_handler(arguments: dict[str, Any], context: Any) -> Any:
            marker()
            return await original(arguments, context)

        return replace(tool, handler=marked_handler)

    def register(self, tool: Tool) -> None:
        self.inner.register(tool)

    def unregister(self, name: str) -> Tool | None:
        return self.inner.unregister(name)

    def get(self, name: str) -> Tool | None:
        tool = self.inner.get(name)
        if tool is None:
            return None
        return self._wrap(tool)

    def names(self) -> list[str]:
        return self.inner.names()

    def all(self) -> list[Tool]:
        return [self._wrap(tool) for tool in self.inner.all()]

    def specs(self) -> tuple[Any, ...]:
        return tuple(tool.spec() for tool in self.all())

    def __len__(self) -> int:
        return len(self.inner)

    def __contains__(self, name: object) -> bool:
        return name in self.inner

    async def execute(self, call: ToolCall, context: Any) -> Any:
        return await self.inner.execute(call, context)


class AgentResources:
    def __init__(
        self,
        agent: Agent,
        engine: AgentEngine,
        cleanup: Callable[[], Awaitable[None]],
    ) -> None:
        self.agent = agent
        self.engine = engine
        self.cleanup = cleanup


AgentResourceFactory = Callable[[], Awaitable[AgentResources]]


def default_agent_resource_factory(
    config: Config,
    *,
    database: Database | None = None,
    model: str | None = None,
) -> AgentResourceFactory:
    from eurus import paths

    async def factory() -> AgentResources:
        router = build_router(config, model=model) if model else build_router(config)
        mcp = MCPRuntime.build(paths.home_dir())
        registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
        try:
            await mcp.connect()
            mcp.register_tools(registry)
            agent = build_agent(
                config,
                router,
                registry=registry,
                approver=deny_remote_approval,
                stream=False,
                lessons=True,
                learn=False,
            )
        except BaseException:
            await mcp.close()
            await router.aclose()
            raise
        engine, opened = build_agent_engine_from_database(config, agent, database=database)

        async def cleanup() -> None:
            from eurus.browser.integration import close_registry_browser

            with contextlib.suppress(Exception):
                await close_registry_browser(registry)
            await mcp.close()
            await router.aclose()

        return AgentResources(agent=agent, engine=engine, cleanup=cleanup)

    return factory


class AgentJobExecutor:
    def __init__(
        self,
        config: Config,
        *,
        resource_factory: AgentResourceFactory | None = None,
        database: Database | None = None,
        tool_provider: ToolProvider | None = None,
        side_effect_marker_factory: SideEffectMarkerFactory | None = None,
        trajectory_store: Any = None,
    ) -> None:
        self.config = config
        self.database = database
        self.resource_factory = resource_factory or default_agent_resource_factory(
            config, database=database
        )
        self.tool_provider = tool_provider
        self.side_effect_marker_factory = side_effect_marker_factory
        self.trajectory_store = trajectory_store
        self._model_factories: dict[str, AgentResourceFactory] = {"": self.resource_factory}
        self._owns_default_factory = resource_factory is None

    def _factory_for_model(self, model_override: str) -> AgentResourceFactory:
        if not model_override:
            return self.resource_factory
        if self._owns_default_factory:
            cached = self._model_factories.get(model_override)
            if cached is None:
                cached = default_agent_resource_factory(
                    self.config, database=self.database, model=model_override
                )
                self._model_factories[model_override] = cached
            return cached
        return self.resource_factory

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        objective_prefix = _objective_prefix(context)
        prelude = await _job_prelude(context, self.config)
        objective = f"{objective_prefix}{prelude}{job.objective}"
        if context.payload.get("verbatim"):
            self._record_trajectory(
                job,
                "job_finished",
                outcome="completed",
                payload={"result": _short_text(job.objective), "verbatim": True},
            )
            return job.objective.strip()
        model_override = _model_override(context.payload, self.config)
        factory = self._factory_for_model(model_override)
        self._record_trajectory(
            job,
            "job_started",
            outcome="started",
            payload={
                "objective": _short_text(job.objective),
                "mode": context.mode,
                "source": job.source,
                "attempt": job.attempts + 1,
                "model_policy": str(context.payload.get("model_policy", "") or ""),
                "model_override": model_override,
            },
        )
        resources = await factory()
        try:
            self._record_trajectory(
                job,
                "model_resolved",
                outcome="resolved",
                payload={
                    "model_override": model_override,
                    "model_chain": [
                        str(ref) for ref in _router_chain(getattr(resources.agent, "router", None))
                    ],
                },
            )
            marker = (
                self.side_effect_marker_factory(job)
                if self.side_effect_marker_factory is not None
                else None
            )
            if self.tool_provider is not None:
                for tool in self.tool_provider(job, context):
                    resources.agent.registry.register(tool)
            _apply_scope(resources.agent, context, marker)
            run = Run(
                run_id=_run_reference(job),
                owner_id=job.owner_id,
                objective=job.objective,
                parent_run_id=context.parent_execution_id,
                budget=_budget_dict(context.budget),
            )
            result = await resources.engine.run(
                objective,
                owner_id=job.owner_id,
                run=run,
                budget=context.budget,
                success_criteria=_success_criteria(context),
            )
            if result.run.status != RunStatus.COMPLETED or not result.output.strip():
                self._record_trajectory(
                    job,
                    "job_failed",
                    outcome="failed",
                    payload={
                        "stop_reason": result.run.stop_reason or "no_verifiable_text_result",
                        "iterations": getattr(result.run, "checkpoint_version", 0),
                    },
                )
                raise RuntimeError(result.run.stop_reason or "no_verifiable_text_result")
            self._record_trajectory(
                job,
                "job_finished",
                outcome="completed",
                payload={"result": _short_text(result.output)},
            )
            return result.output.strip()
        except BaseException as exc:
            self._record_trajectory(
                job,
                "job_failed",
                outcome="failed",
                payload={"error": _short_text(f"{type(exc).__name__}: {exc}")},
            )
            raise
        finally:
            await resources.cleanup()

    def _record_trajectory(
        self, job: Job, event_type: str, *, outcome: str, payload: dict[str, Any]
    ) -> None:
        if self.trajectory_store is None:
            return
        with contextlib.suppress(Exception):
            self.trajectory_store.record(
                job.job_id,
                event_type,
                session_id=None,
                outcome=outcome,
                payload=payload,
            )


def _short_text(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit]


def _agent_database_path() -> Path:
    from eurus import paths

    return paths.database_path()


async def _job_prelude(context: ExecutionContext, config: Config) -> str:
    parts: list[str] = []
    skill_names = context.payload.get("skills")
    if isinstance(skill_names, (list, tuple)) and skill_names:
        outcome_store = None
        try:
            from eurus.learning.effectiveness import SkillOutcomeStore

            outcome_store = SkillOutcomeStore(_agent_database_path())
        except Exception:
            log.debug("skill outcome store unavailable", exc_info=True)
        parts.extend(
            await asyncio.to_thread(
                _load_skill_blocks,
                skill_names,
                config,
                outcome_store=outcome_store,
                owner_id=str(getattr(context, "owner_id", "local") or "local"),
            )
        )
    pre_script = str(context.payload.get("pre_script") or "").strip()
    if pre_script:
        output = await _run_pre_script(pre_script, config)
        if output:
            parts.append(f"[pre_script output]\n{output}\n")
    prelude = "".join(parts)
    return _short_text(prelude, 20_000)


def _load_skill_blocks(
    skill_names: Sequence[Any],
    config: Config,
    *,
    outcome_store: Any = None,
    owner_id: str = "local",
) -> list[str]:
    from eurus.skills.loader import resolve_skills

    workspace = Path(config.permissions.workspace).expanduser()
    blocks: list[str] = []
    selected: list[str] = []
    for entry in skill_names:
        name = str(entry)
        if outcome_store is not None:
            try:
                effectiveness = outcome_store.effectiveness(name, owner_id=owner_id)
            except Exception:
                log.debug("skill effectiveness lookup failed for %s", name, exc_info=True)
                effectiveness = None
            if effectiveness is not None and effectiveness.should_demote:
                log.warning(
                    "skill %s is demoted by its effectiveness gate (%s); skipping auto-load",
                    name,
                    effectiveness.reason,
                )
                continue
        selected.append(name)
    try:
        loaded = resolve_skills(selected, workspace)
    except Exception:
        log.exception("scheduled run could not load skills %s", list(skill_names))
        return blocks
    for skill in loaded:
        content = _short_text(skill.content, 8000)
        blocks.append(f"[skill {skill.name}]\n{content}\n")
    return blocks


async def _run_pre_script(script: str, config: Config) -> str:
    from eurus.execution.host import scrubbed_env

    timeout = float(getattr(getattr(config, "autonomy", None), "pre_script_timeout_seconds", 30.0))
    try:
        process = await asyncio.create_subprocess_shell(
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrubbed_env(),
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        return f"[pre_script failed to start: {type(exc).__name__}]"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_process_group(process)
        await process.wait()
        return "[pre_script timed out]"
    output = stdout.decode(errors="replace").strip()
    if process.returncode != 0:
        error_text = stderr.decode(errors="replace").strip()[:500]
        note = f"[pre_script exited with code {process.returncode}]"
        return f"{note}\n{output}".strip() if output else f"{note} {error_text}".strip()
    return output


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()


def _model_override(payload: Mapping[str, Any], config: Any) -> str:
    from eurus.multiagent.routing import resolve_model_override

    try:
        return resolve_model_override(payload, config)
    except ValueError:
        log.warning("job carried an unknown model policy; falling back to the default chain")
        return ""


def _router_chain(router: Any) -> Sequence[Any]:
    chain = getattr(router, "chain", None)
    return chain if isinstance(chain, Sequence) else ()


def _objective_prefix(context: ExecutionContext) -> str:
    parts: list[str] = []
    if context.goal_id:
        parts.append(f"[goal {context.goal_id}]")
    role = str(context.payload.get("role", "")).strip()
    if role:
        parts.append(f"[{role} specialist]")
    if not parts:
        return ""
    return " ".join(parts) + " "


def _apply_scope(agent: Agent, context: ExecutionContext, marker: SideEffectMarker | None) -> None:
    chain: Any = agent.registry
    if marker is not None:
        chain = MutatingToolMarker(chain, marker)
    scoped: Any = ScopedToolRegistry(
        chain, allowed=context.allowed_tools, blocked=context.blocked_tools
    )
    agent.registry = scoped
    agent.boundary.registry = scoped


def _run_reference(job: Job) -> str:
    if job.lease_version <= 1:
        return job.job_id
    return f"{job.job_id}.{job.lease_version}"


def _budget_dict(budget: BudgetSpec) -> dict[str, object]:
    return {
        "max_iterations": budget.max_iterations,
        "max_runtime_seconds": budget.max_runtime_seconds,
        "max_tool_calls": budget.max_tool_calls,
        "deadline_at": budget.deadline_at.isoformat() if budget.deadline_at else None,
    }


def _success_criteria(context: ExecutionContext) -> str | None:
    criteria = str(context.payload.get("success_criteria", "") or "").strip()
    return criteria or None
