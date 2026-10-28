"""Session and learning commands."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from eurus import paths
from eurus.cli.base import main
from eurus.sessions import SessionStore


@main.group("sessions")
def sessions_group() -> None:
    pass


@sessions_group.command("list")
def sessions_list() -> None:
    table = Table(title="sessions")
    for column in ("ID", "Title", "Messages", "Updated"):
        table.add_column(column)
    for item in SessionStore(paths.home_dir() / "sessions.sqlite3").list_sessions():
        table.add_row(
            str(item["id"]),
            str(item["title"]),
            str(item["message_count"]),
            str(item["updated_at"]),
        )
    Console().print(table)


@sessions_group.command("show")
@click.argument("session_id")
def sessions_show(session_id: str) -> None:
    store = SessionStore(paths.home_dir() / "sessions.sqlite3")
    messages = store.load_messages(session_id)
    if not messages:
        raise click.ClickException(f"session not found or empty: {session_id}")
    for message in messages:
        click.echo(f"[{message.role}] {message.content}")
        for call in message.tool_calls:
            click.echo(f"  tool call: {call.name} {json.dumps(call.arguments, ensure_ascii=False)}")


@sessions_group.command("summarize")
@click.argument("session_id")
def sessions_summarize(session_id: str) -> None:
    store = SessionStore(paths.home_dir() / "sessions.sqlite3")
    messages = store.load_messages(session_id)
    if not messages:
        raise click.ClickException(f"session not found or empty: {session_id}")
    try:
        summary = store.build_session_summary(session_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    store.save_session_summary(session_id, summary)
    click.echo(summary)


@main.group("learn")
def learn_group() -> None:
    pass


@learn_group.command("consolidate")
@click.option(
    "--apply",
    is_flag=True,
    help="Delete exact duplicate memories after showing the plan.",
)
def learn_consolidate(apply: bool) -> None:
    store = SessionStore(paths.home_dir() / "sessions.sqlite3")
    result = store.consolidate_memories(dry_run=not apply)
    action = "deleted" if apply else "would delete"
    click.echo(
        f"scanned={result['scanned']} duplicates={result['duplicates']} "
        f"{action}={result['deleted'] if apply else result['duplicates']}"
    )
    if result["duplicates"] and not apply:
        click.echo("Review this plan, then rerun with --apply to remove exact duplicates.")


@sessions_group.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Delete this session and its messages?")
def sessions_delete(session_id: str) -> None:
    if not SessionStore(paths.home_dir() / "sessions.sqlite3").delete_session(session_id):
        raise click.ClickException(f"session not found: {session_id}")
    click.echo(f"deleted session {session_id}")
