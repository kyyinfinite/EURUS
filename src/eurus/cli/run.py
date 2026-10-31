"""Task execution commands routed through the canonical runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import click

from eurus import paths
from eurus.agent.events import AgentEvent
from eurus.agent.factory import build_agent
from eurus.cli.base import _state, main
from eurus.cli.common import (
    _approve,
    _build_engine,
    _engine_stores,
    _render,
    _run_once,
)
from eurus.errors import EurusError
from eurus.mcp.runtime import MCPRuntime
from eurus.providers.factory import build_router
from eurus.sessions import SessionStore
from eurus.tools.builtin import default_registry


@main.command()
@click.argument("prompt", nargs=-1, required=True)
@click.option("--stream/--no-stream", default=True, help="Stream tokens as they arrive.")
@click.pass_context
def run(ctx: click.Context, prompt: tuple[str, ...], stream: bool) -> None:
    config = _state(ctx).config
    try:
        asyncio.run(_run_once(config, " ".join(prompt), stream))
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc


CHAT_HELP = (
    "/tools list tools, /usage token usage, /clear reset conversation, "
    "/privacy export|purge, /exit quit"
)


@main.command()
@click.option("--stream/--no-stream", default=True, help="Stream tokens as they arrive.")
@click.option("--session", "session_id", default=None, help="Persist this chat session in SQLite.")
@click.pass_context
def chat(ctx: click.Context, stream: bool, session_id: str | None) -> None:
    config = _state(ctx).config
    with asyncio.Runner() as runner:
        try:
            router = build_router(config)
        except EurusError as exc:
            raise click.ClickException(str(exc)) from exc
        mcp_runtime = MCPRuntime.build(paths.home_dir())
        try:
            runner.run(mcp_runtime.connect())
            registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
            mcp_runtime.register_tools(registry)
        except (RuntimeError, ValueError) as exc:
            runner.run(mcp_runtime.close())
            runner.run(router.aclose())
            raise click.ClickException(str(exc)) from exc
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

        async def durable_turn(turn_text: str) -> None:
            def render(event: AgentEvent) -> None:
                _render(event, router)

            result = await engine.run(
                turn_text,
                owner_id="local",
                session_id=agent.session_id or session_id,
                on_event=render,
            )
            click.echo(
                f"run {result.run.run_id} {result.run.status.value} ({result.run.stop_reason})",
                err=True,
            )

        privacy_store = SessionStore(paths.database_path())
        store = privacy_store if session_id else None
        if store is not None and session_id is not None:
            resume_id = store.latest_session_descendant(session_id)
            agent.history.extend(store.load_messages(resume_id))
            agent.context_store = store
            agent.session_id = resume_id
        if privacy_store.memory_curation_due():
            click.echo(
                "Memory curation is due. Review saved notes with `eurus memory list`, "
                "then update or remove stale entries.",
                err=True,
            )
            privacy_store.record_event("memory_curation_nudge", session_id=agent.session_id)
        click.echo(f"eurus chat ({config.agent.model}). {CHAT_HELP}", err=True)
        try:
            while True:
                try:
                    text = input("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    click.echo()
                    break
                if not text:
                    continue
                if text in ("/exit", "/quit"):
                    break
                if text == "/clear":
                    agent.reset()
                    if store is not None and session_id is not None:
                        store.save_messages(
                            agent.session_id or session_id,
                            agent.history,
                            project_scope=str(agent.context.workspace.resolve()),
                        )
                    click.echo("conversation cleared", err=True)
                elif text == "/tools":
                    click.echo(", ".join(agent.registry.names()), err=True)
                elif text == "/usage":
                    usage = agent.usage
                    click.echo(f"in={usage.input_tokens} out={usage.output_tokens}", err=True)
                elif text == "/privacy export":
                    click.echo(
                        json.dumps(
                            privacy_store.export_privacy_data(),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif text == "/privacy purge":
                    prompt = (
                        "Type PURGE to clear saved sessions, memories, and local activity "
                        "metadata: "
                    )
                    if input(prompt).strip() == "PURGE":
                        counts = privacy_store.purge_privacy_data()
                        agent.reset()
                        click.echo(json.dumps({"purged": counts}, ensure_ascii=False), err=True)
                    else:
                        click.echo("privacy purge cancelled", err=True)
                else:
                    try:
                        runner.run(durable_turn(text))
                        if store is not None and session_id is not None:
                            store.save_messages(
                                agent.session_id or session_id,
                                agent.history,
                                project_scope=str(agent.context.workspace.resolve()),
                            )
                    except EurusError as exc:
                        click.echo(f"error: {exc}", err=True)
                    except KeyboardInterrupt:
                        click.echo("\ninterrupted", err=True)
        finally:
            from eurus.browser.integration import close_registry_browser

            with contextlib.suppress(Exception):
                runner.run(close_registry_browser(registry))
            runner.run(mcp_runtime.close())
            runner.run(router.aclose())


@main.command("tui")
@click.pass_context
def tui_command(ctx: click.Context) -> None:
    from eurus.tui.app import run_tui

    _state(ctx).console_logging = False
    try:
        run_tui(ctx)
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc


def agent_main() -> None:
    main(args=[*sys.argv[1:], "tui"], prog_name="eurus")
