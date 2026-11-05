from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.cli.common import _build_engine, _engine_stores
from eurus.config import Config
from eurus.gateway.approval import ApprovalContext, current_approval_context
from eurus.gateway.web.approval import WEB_OWNER_ID, WebApprovalBroker
from eurus.mcp.runtime import MCPRuntime
from eurus.providers.factory import build_router
from eurus.providers.router import Router
from eurus.reminders import reset_reminder_context, set_reminder_context
from eurus.runtime.application import AgentRuntime
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run
from eurus.sessions import SessionStore
from eurus.tools.builtin import default_registry

log = logging.getLogger(__name__)

OWNER_ID = WEB_OWNER_ID
RunFactory = Any


def _schedule_store() -> Any:
    from eurus.scheduler import ScheduleStore

    return ScheduleStore(paths.database_path())


class WebRuntime:
    def __init__(self, config: Config, broker: WebApprovalBroker) -> None:
        self.config = config
        self.broker = broker
        paths.home_manager().ensure_layout()
        self.session_store = SessionStore(paths.database_path())
        self.registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
        self.mcp_runtime = MCPRuntime.build(paths.home_dir())
        self.runtime: AgentRuntime | None = None
        self._router_instance: Router | None = None
        self._reminder_store: Any | None = None
        self._hooks: Any | None = None

    def _router(self) -> Router:
        if self._router_instance is None:
            self._router_instance = build_router(self.config)
        return self._router_instance

    async def start(self) -> None:
        from eurus.reminders import ReminderStore, reminder_tools

        await self.mcp_runtime.connect()
        self.mcp_runtime.register_tools(self.registry)
        if self.config.browser.enabled:
            from eurus.browser.integration import ensure_browser_tools

            workspace = Path(self.config.permissions.workspace).expanduser().resolve()
            with contextlib.suppress(Exception):
                ensure_browser_tools(self.registry, self.config.browser, workspace=workspace)

        self._reminder_store = ReminderStore(paths.database_path())
        for reminder_tool in reminder_tools(self._reminder_store):
            if reminder_tool.name not in self.registry:
                self.registry.register(reminder_tool)
        from eurus.schedule_tools import schedule_tool

        self._schedule_store = _schedule_store()
        schedule = schedule_tool(self._schedule_store)
        if schedule.name not in self.registry:
            self.registry.register(schedule)

        from eurus.autonomy_hooks import HookPublisher, StepCounter

        self._hooks = HookPublisher.for_home()
        self._step_counter = StepCounter()

        def agent_factory(session_id: str) -> Agent:
            agent = build_agent(
                self.config,
                self._router(),
                registry=self.registry,
                approver=self.broker.approve,
                stream=True,
                lessons=True,
                learn=False,
            )
            agent.session_id = session_id
            agent.context_store = self.session_store
            return agent

        def engine_factory(agent: Agent) -> AgentEngine:
            runs, events, attempts = _engine_stores()
            return _build_engine(
                self.config,
                agent,
                run_store=runs,
                event_store=events,
                attempt_store=attempts,
            )

        async def close_resources() -> None:
            from eurus.browser.integration import close_registry_browser

            with contextlib.suppress(Exception):
                await close_registry_browser(self.registry)
            await self.mcp_runtime.close()
            if self._router_instance is not None:
                await self._router_instance.aclose()

        self.runtime = AgentRuntime(
            agent_factory,
            self.session_store,
            close_callback=close_resources,
            engine_factory=engine_factory,
            require_engine=True,
        )

    def run_factory(self, run_id: str, session_id: str) -> RunFactory:

        def factory(objective: str) -> Run:
            return Run(
                owner_id=OWNER_ID,
                objective=objective,
                run_id=run_id,
                session_id=session_id,
            )

        return factory

    async def run_turn(
        self,
        session_id: str,
        text: str,
        run_id: str,
        on_event: Any | None = None,
    ) -> str:
        if self.runtime is None:
            raise RuntimeError("web runtime is not started")
        context_token = current_approval_context.set(
            ApprovalContext(task_id=run_id, owner_id=OWNER_ID, chat_id=session_id)
        )
        reminder_token = set_reminder_context(OWNER_ID, session_id, "web")
        from eurus.schedule_tools import reset_schedule_context, set_schedule_context

        schedule_token = set_schedule_context(OWNER_ID, "web", session_id)
        hooks = self._hooks
        counter = self._step_counter
        if hooks is not None:
            hooks.agent_start(run_id=run_id, gateway="web", session_id=session_id)
        output: list[str] = []
        try:
            async for event in self.runtime.run(
                session_id,
                text,
                task_id=run_id,
                run_factory=self.run_factory(run_id, session_id),
            ):
                if on_event is not None:
                    await on_event(event)
                if event.kind == "step" and hooks is not None:
                    hooks.agent_step(
                        run_id=run_id,
                        gateway="web",
                        step=counter.next(),
                        detail=str(getattr(event, "detail", "") or ""),
                    )
                if event.kind == "text" and event.text:
                    output.append(event.text)
                elif event.kind == "text_reset":
                    output.clear()
            return "".join(output).strip()
        except Exception as exc:
            if hooks is not None:
                hooks.agent_end(run_id=run_id, gateway="web", status=type(exc).__name__)
            raise
        else:
            if hooks is not None:
                hooks.agent_end(run_id=run_id, gateway="web", status="completed")
        finally:
            reset_schedule_context(schedule_token)
            reset_reminder_context(reminder_token)
            current_approval_context.reset(context_token)

    def health(self) -> dict[str, Any]:
        if self.runtime is None:
            return {"started": False}
        data = dict(self.runtime.health())
        data["started"] = True
        return data

    async def stop(self) -> None:
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            await runtime.close()


__all__ = ["OWNER_ID", "WebRuntime"]
