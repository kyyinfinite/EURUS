from __future__ import annotations

import contextlib
import json
from typing import Any

import click

from eurus import paths
from eurus.agent.events import AgentEvent
from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.coding.checkpoint import CheckpointStore
from eurus.config import Config
from eurus.mcp.manager import MCPToolManager
from eurus.models import ToolCall, Usage
from eurus.providers.factory import build_router
from eurus.providers.router import Router
from eurus.runtime.engine import AgentEngine, RunResult
from eurus.runtime.pricing import build_pricing_catalog
from eurus.security.approval import approval_request
from eurus.sessions import SessionStore
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool
from eurus.tools.builtin import default_registry

"""Shared helpers for agent construction and durable execution."""


def _render(event: AgentEvent, router: Router) -> None:
    if event.kind == "text":
        click.echo(event.text, nl=False)
    elif event.kind == "text_reset":
        click.echo("\n[retrying model request]", err=True)
    elif event.kind == "tool_call" and event.tool_call is not None:
        shown = json.dumps(event.tool_call.arguments, ensure_ascii=False)[:200]
        click.echo(f"\n-> {event.tool_call.name} {shown}", err=True)
    elif event.kind == "tool_result" and event.tool_result is not None:
        result = event.tool_result
        preview = result.output.replace("\n", " ")[:200]
        click.echo(f"<- {'error' if result.is_error else 'ok'}: {preview}", err=True)
    elif event.kind == "lesson":
        click.echo(f"saved lesson: {event.text}", err=True)
    elif event.kind == "skill":
        click.echo(f"skill draft quarantined: {event.text}", err=True)
    elif event.kind == "done":
        click.echo()
        usage = event.usage or Usage()
        used = f"[{router.last_used}] " if router.last_used is not None else ""
        click.echo(
            f"{used}{event.stop_reason} in={usage.input_tokens} out={usage.output_tokens}",
            err=True,
        )


async def _approve(call: ToolCall, tool: Tool) -> bool:
    request = approval_request(call, tool)
    click.echo(f"\nApproval required: {request.tool_name}", err=True)
    click.echo(request.summary, err=True)
    click.echo(f"Action fingerprint: {request.action_fingerprint[:16]}", err=True)
    allowed = click.confirm("Allow this action?", default=False, err=True)
    SessionStore(paths.home_dir() / "sessions.sqlite3").record_event(
        "approval_accepted" if allowed else "approval_rejected",
        metadata={"tool": call.name, "mutating": tool.mutates},
    )
    return allowed


async def _deny_remote_approval(call: ToolCall, tool: Tool) -> bool:
    return False


def _engine_stores() -> tuple[RunStore, EventStore, ToolAttemptStore]:
    database = Database(paths.home_dir() / "sessions.sqlite3")
    return RunStore(database), EventStore(database), ToolAttemptStore(database)


def _build_engine(
    config: Any,
    agent: Agent,
    *,
    run_store: RunStore | None,
    event_store: EventStore | None,
    attempt_store: ToolAttemptStore | None,
) -> AgentEngine:
    return AgentEngine(
        agent,
        run_store=run_store,
        event_store=event_store,
        attempt_store=attempt_store,
        checkpoint_store=CheckpointStore(),
        pricing=build_pricing_catalog(
            {"updated_at": config.pricing.updated_at, "models": config.pricing.models},
            home=paths.home_dir(),
        ),
    )


async def _run_once(config: Config, text: str, stream: bool) -> RunResult:
    router = build_router(config)
    mcp = MCPToolManager.load(paths.home_dir() / "mcp.json")
    try:
        await mcp.connect()
        registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
        mcp.register_tools(registry)
        agent = build_agent(
            config,
            router,
            registry=registry,
            approver=_approve,
            stream=stream,
            lessons=True,
            learn=True,
        )
        runs, events, attempts = _engine_stores()
        engine = _build_engine(
            config, agent, run_store=runs, event_store=events, attempt_store=attempts
        )

        def render(event: AgentEvent) -> None:
            _render(event, router)

        result = await engine.run(text, owner_id="local", on_event=render)
        status = result.run.status.value
        verified = (
            "verified"
            if result.verified
            else "unverified"
            if result.verified is None
            else "verification_failed"
        )
        cost = f" cost~{result.cost_estimate:.4f}" if result.cost_estimate is not None else ""
        click.echo(
            f"run {result.run.run_id} {status} ({result.run.stop_reason}) {verified}{cost}",
            err=True,
        )
        return result
    finally:
        from eurus.browser.integration import close_registry_browser

        with contextlib.suppress(Exception):
            await close_registry_browser(registry)
        await mcp.close()
        await router.aclose()
