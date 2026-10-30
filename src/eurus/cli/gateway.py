"""Gateway commands including the Telegram adapter."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import click

from eurus import paths
from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.cli.base import _state, main
from eurus.cli.common import _build_engine, _engine_stores
from eurus.errors import EurusError
from eurus.mcp.runtime import MCPRuntime
from eurus.providers.factory import build_router
from eurus.runtime.engine import AgentEngine
from eurus.sessions import SessionStore
from eurus.tools.builtin import default_registry


@main.group("gateway")
def gateway_group() -> None:
    pass


@gateway_group.command("status")
def gateway_status() -> None:
    from eurus.gateway.health import describe_gateway
    from eurus.gateway.tasks import TaskStore

    path = paths.home_dir() / "sessions.sqlite3"
    if not path.exists():
        click.echo("No gateway task database found.")
        return
    status = describe_gateway(TaskStore(path))
    counts = ", ".join(f"{name}={total}" for name, total in sorted(status.counts.items()))
    click.echo(f"Tasks: {counts or 'none'}")
    click.echo("Interrupted tasks needing review:")
    for item in status.needs_review:
        click.echo(f"  {item.task_id} {item.updated_at} action={item.recovery_action}")
    if not status.needs_review:
        click.echo("  none")
    click.echo("Recent failures:")
    for item in status.recent_failures:
        click.echo(f"  {item.task_id} {item.updated_at} error={item.error}")
    if not status.recent_failures:
        click.echo("  none")


@gateway_group.command("prune")
@click.option("--days", type=click.FloatRange(min=1), default=90.0, show_default=True)
@click.option("--yes", is_flag=True, help="Delete finished tasks instead of only counting them.")
def gateway_prune(days: float, yes: bool) -> None:
    from eurus.gateway.tasks import TaskStore

    path = paths.home_dir() / "sessions.sqlite3"
    if not path.exists():
        click.echo("No gateway task database found.")
        return
    store = TaskStore(path)
    count = store.prune_tasks(days, dry_run=not yes)
    if yes:
        click.echo(f"Deleted {count} finished tasks older than {days:g} days.")
    else:
        click.echo(f"{count} finished tasks are older than {days:g} days. Re-run with --yes.")


@gateway_group.command("telegram")
@click.option("--max-concurrent", type=click.IntRange(1, 8), default=2, show_default=True)
@click.pass_context
def gateway_telegram(ctx: click.Context, max_concurrent: int) -> None:
    from eurus.gateway.security import GatewayAccessPolicy, parse_id_allowlist
    from eurus.gateway.tasks import TaskStore
    from eurus.gateway.telegram import run_telegram_polling, token_from_environment
    from eurus.gateway.telegram.approval import TelegramApprovalBroker, parse_approval_timeout

    try:
        token = token_from_environment()
        allowed_users = parse_id_allowlist(os.environ.get("EURUS_TELEGRAM_ALLOWED_USER_IDS", ""))
        allowed_groups = parse_id_allowlist(
            os.environ.get("EURUS_TELEGRAM_ALLOWED_GROUP_IDS", ""), allow_negative=True
        )
        if not allowed_users:
            raise RuntimeError(
                "Eurus Telegram access configuration must contain numeric Telegram user IDs"
            )
        policy = GatewayAccessPolicy(allowed_users, allowed_groups)
        approval_timeout = parse_approval_timeout(
            os.environ.get("EURUS_TELEGRAM_APPROVAL_TIMEOUT", "120")
        )
        config = _state(ctx).config
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    store = TaskStore(paths.home_dir() / "sessions.sqlite3")
    session_store = SessionStore(paths.home_dir() / "sessions.sqlite3")

    async def serve() -> None:
        from eurus.runtime.application import AgentRuntime

        router = build_router(config)
        mcp = MCPRuntime.build(paths.home_dir())
        runtime: AgentRuntime | None = None
        approval_broker = TelegramApprovalBroker(timeout=approval_timeout)
        try:
            await mcp.connect()
            registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
            mcp.register_tools(registry)
            from eurus.reminders import ReminderStore, reminder_tools

            reminder_store = ReminderStore(paths.home_dir() / "sessions.sqlite3")
            for reminder_tool in reminder_tools(reminder_store):
                if reminder_tool.name not in registry:
                    registry.register(reminder_tool)

            def agent_factory(session_id: str) -> Agent:
                agent = build_agent(
                    config,
                    router,
                    registry=registry,
                    approver=approval_broker.approve,
                    stream=True,
                    lessons=True,
                    learn=False,
                )
                agent.session_id = session_id
                agent.context_store = session_store
                return agent

            def engine_factory(agent: Agent) -> AgentEngine:
                runs, events, attempts = _engine_stores()
                return _build_engine(
                    config, agent, run_store=runs, event_store=events, attempt_store=attempts
                )

            async def close_resources() -> None:
                from eurus.browser.integration import close_registry_browser

                with contextlib.suppress(Exception):
                    await close_registry_browser(registry)
                await mcp.close()
                await router.aclose()

            runtime = AgentRuntime(
                agent_factory,
                session_store,
                close_callback=close_resources,
                engine_factory=engine_factory,
                require_engine=True,
            )

            async def runner(task: Any) -> str:
                if runtime is None:
                    raise RuntimeError("agent runtime is not initialized")
                output: list[str] = []
                async for event in runtime.run(
                    task.session_id, task.objective, task_id=task.task_id
                ):
                    if event.kind == "text" and event.text:
                        output.append(event.text)
                    elif event.kind == "text_reset":
                        output.clear()
                answer = "".join(output).strip()
                if not answer:
                    agent = runtime.agents.get(task.session_id)
                    if agent is not None:
                        answer = next(
                            (
                                message.content
                                for message in reversed(agent.history)
                                if message.role == "assistant" and message.content
                            ),
                            "Task ended without a verifiable text response.",
                        )
                return answer

            try:
                await run_telegram_polling(
                    token=token,
                    policy=policy,
                    store=store,
                    runner=runner,
                    max_concurrent=max_concurrent,
                    approval_broker=approval_broker,
                    event_bridge=runtime.event_bridge,
                    reminder_store=reminder_store,
                )
            finally:
                await runtime.close()
        except BaseException:
            if runtime is None:
                from eurus.browser.integration import close_registry_browser

                with contextlib.suppress(Exception):
                    await close_registry_browser(registry)
                await mcp.close()
                await router.aclose()
            raise

    try:
        asyncio.run(serve())
    except (RuntimeError, EurusError) as exc:
        raise click.ClickException(str(exc)) from exc
