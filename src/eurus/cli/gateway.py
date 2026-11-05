from __future__ import annotations

import asyncio
import contextlib
import logging
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

log = logging.getLogger(__name__)


@main.group("gateway")
def gateway_group() -> None:
    pass


def _schedule_store() -> Any:
    from eurus.scheduler import ScheduleStore

    return ScheduleStore(paths.database_path())


def _job_store() -> Any:
    from eurus.storage.database import Database
    from eurus.storage.jobs import JobStore

    return JobStore(Database(paths.database_path()))


def _telegram_delivery_sender(adapter: Any) -> Any:
    async def send(target: str, subject: str, body: str) -> None:
        text = f"{subject}\n\n{body}".strip() if body else subject
        await adapter.send_text(target, text)

    return send


@gateway_group.command("status")
def gateway_status() -> None:
    from eurus.gateway.health import describe_gateway
    from eurus.gateway.tasks import TaskStore

    path = paths.database_path()
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

    path = paths.database_path()
    if not path.exists():
        click.echo("No gateway task database found.")
        return
    store = TaskStore(path)
    count = store.prune_tasks(days, dry_run=not yes)
    if yes:
        click.echo(f"Deleted {count} finished tasks older than {days:g} days.")
    else:
        click.echo(f"{count} finished tasks are older than {days:g} days. Re-run with --yes.")


@gateway_group.command("web")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address (loopback by default).",
)
@click.option(
    "--port",
    default=8712,
    type=click.IntRange(1, 65535),
    show_default=True,
    help="Local port.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=False,
    show_default=True,
    help="Open the browser.",
)
@click.pass_context
def gateway_web(ctx: click.Context, host: str, port: int, open_browser: bool) -> None:
    import os

    from eurus.errors import EurusError
    from eurus.gateway.web.app import build_context, serve
    from eurus.gateway.web.auth import GatewayCredentialStore, GatewaySecurity

    if host not in ("127.0.0.1", "localhost", "::1"):
        click.echo(
            f"Warning: binding to {host} exposes the gateway beyond this machine. "
            "Token authentication stays enabled, but loopback is recommended.",
            err=True,
        )
    config = _state(ctx).config
    try:
        token = os.environ.get("EURUS_GATEWAY_TOKEN", "").strip()
        if not token:
            token = GatewayCredentialStore().load_or_create()
        security = GatewaySecurity(token, host=host, port=port)
        approval_timeout = float(os.environ.get("EURUS_GATEWAY_APPROVAL_TIMEOUT", "600") or "600")
        context = build_context(config, security=security, approval_timeout=approval_timeout)
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        asyncio.run(
            serve(
                context,
                host=host,
                port=port,
                open_browser=open_browser,
            )
        )
    except (RuntimeError, EurusError) as exc:
        raise click.ClickException(str(exc)) from exc


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

    store = TaskStore(paths.database_path())
    session_store = SessionStore(paths.database_path())

    async def serve() -> None:
        from eurus.autonomy_service import AutonomyService
        from eurus.runtime.application import AgentRuntime

        router = build_router(config)
        mcp = MCPRuntime.build(paths.home_dir())
        runtime: AgentRuntime | None = None
        autonomy: AutonomyService | None = None
        approval_broker = TelegramApprovalBroker(timeout=approval_timeout)
        try:
            await mcp.connect()
            registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
            mcp.register_tools(registry)
            from eurus.reminders import ReminderStore, reminder_tools

            reminder_store = ReminderStore(paths.database_path())
            for reminder_tool in reminder_tools(reminder_store):
                if reminder_tool.name not in registry:
                    registry.register(reminder_tool)

            from eurus.schedule_tools import schedule_tool

            registry.register(schedule_tool(_schedule_store()))

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
            autonomy = AutonomyService(config)
            await autonomy.start()

            def register_delivery_channel(telegram_adapter: Any) -> None:
                autonomy.register_channel("telegram", _telegram_delivery_sender(telegram_adapter))

            from eurus.autonomy_hooks import HookPublisher, StepCounter

            hooks = HookPublisher.for_home()
            counter = StepCounter()

            async def runner(task: Any) -> str:
                if runtime is None:
                    raise RuntimeError("agent runtime is not initialized")
                from eurus.owner import OWNER_ID
                from eurus.runtime.models import Run
                from eurus.schedule_tools import reset_schedule_context, set_schedule_context

                conversation_id = str(task.state.get("notify_chat_id") or task.session_id)
                schedule_token = set_schedule_context(OWNER_ID, "telegram", conversation_id)
                hooks.agent_start(
                    run_id=task.task_id, gateway="telegram", session_id=task.session_id
                )
                output: list[str] = []

                def linked_run_factory(objective: str) -> Any:
                    run_row = Run(owner_id=OWNER_ID, objective=objective)
                    run_row.session_id = task.session_id
                    try:
                        store.link_run(task.task_id, run_row.run_id)
                    except (ValueError, OSError):
                        log.warning(
                            "task run linkage failed task_id=%s", task.task_id, exc_info=True
                        )
                    return run_row

                try:
                    async for event in runtime.run(
                        task.session_id,
                        task.objective,
                        task_id=task.task_id,
                        run_factory=linked_run_factory,
                    ):
                        if event.kind == "step":
                            hooks.agent_step(
                                run_id=task.task_id,
                                gateway="telegram",
                                step=counter.next(),
                                detail=str(getattr(event, "detail", "") or ""),
                            )
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
                    hooks.agent_end(run_id=task.task_id, gateway="telegram", status="completed")
                    return answer
                except BaseException as exc:
                    hooks.agent_end(
                        run_id=task.task_id, gateway="telegram", status=type(exc).__name__
                    )
                    raise
                finally:
                    reset_schedule_context(schedule_token)

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
                    register_delivery_channel=register_delivery_channel,
                    router_extras={
                        "schedule_store": _schedule_store(),
                        "job_store": _job_store(),
                        "autonomy_status": autonomy.status,
                    },
                )
            finally:
                if autonomy is not None:
                    with contextlib.suppress(Exception):
                        await autonomy.stop()
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
