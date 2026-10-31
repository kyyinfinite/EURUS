"""Memory and privacy commands."""

from __future__ import annotations

import json

import click

from eurus import paths
from eurus.cli.base import main
from eurus.sessions import SessionStore


@main.group("memory")
def memory_group() -> None:
    pass


@memory_group.command("add")
@click.argument("content")
@click.option("--tag", "tags", default="", help="Comma-separated tags.")
@click.option("--category", default="general", help="Memory category, e.g. preference or project.")
@click.option("--scope", default="global", help="Memory scope: global or an explicit project path.")
@click.option(
    "--type",
    "memory_type",
    type=click.Choice(
        ["profile", "preference", "project_fact", "agent_note", "lesson", "episode", "fact"]
    ),
    default="fact",
)
def memory_add(content: str, tags: str, category: str, scope: str, memory_type: str) -> None:
    memory_id = SessionStore(paths.database_path()).add_memory(
        content,
        tags,
        category=category,
        scope=scope,
        memory_type=memory_type,
        trust="user_approved",
    )
    click.echo(f"saved memory {memory_id}")


@memory_group.command("promote")
@click.argument("memory_id", type=int)
@click.confirmation_option(prompt="Promote this quarantined memory to user-approved?")
def memory_promote(memory_id: int) -> None:
    from eurus.memory.manager import MemoryManager

    manager = MemoryManager(paths.database_path())
    try:
        promoted = manager.promote(memory_id, authority="user")
    except PermissionError as exc:
        raise click.ClickException(str(exc)) from exc
    if not promoted:
        raise click.ClickException(
            f"memory {memory_id} is not a quarantined record or does not exist"
        )
    click.echo(f"promoted memory {memory_id} to user_approved")


@memory_group.command("quarantine")
@click.option("--scope", default=None, help="Limit results to this project scope.")
def memory_quarantine(scope: str | None) -> None:
    from eurus.memory.manager import MemoryManager

    manager = MemoryManager(paths.database_path())
    rows = manager.quarantined(scope=scope)
    if not rows:
        click.echo("no quarantined memories")
        return
    for item in rows:
        click.echo(f"#{item['id']} [untrusted] {str(item['content'])[:200]}")
    click.echo(
        "Quarantined records are excluded from prompts. Promote one with "
        "`eurus memory promote <id>` after reviewing it."
    )


@memory_group.command("search")
@click.argument("query")
@click.option("--limit", type=click.IntRange(1, 100), default=10)
@click.option("--scope", default=None, help="Limit results to global notes and this project scope.")
def memory_search(query: str, limit: int, scope: str | None) -> None:
    results = SessionStore(paths.database_path()).search_memories(query, limit, scope=scope)
    for item in results:
        click.echo(f"#{item['id']} [{item['category']}] [{item['tags']}] {item['content']}")
    if not results:
        click.echo("no matching memories")


@memory_group.command("list")
@click.option("--category", default=None, help="Only show one category, e.g. lesson.")
@click.option("--limit", type=click.IntRange(1, 500), default=50)
@click.option("--scope", default=None, help="Limit results to global notes and this project scope.")
def memory_list(category: str | None, limit: int, scope: str | None) -> None:
    store = SessionStore(paths.database_path())
    for item in store.list_memories(category=category, limit=limit, scope=scope):
        click.echo(f"#{item['id']} [{item['category']}] [{item['tags']}] {item['content']}")


@memory_group.command("update")
@click.argument("memory_id", type=int)
@click.argument("content")
@click.option("--tag", "tags", default=None, help="Replace tags.")
@click.option("--category", default=None, help="Replace category.")
def memory_update(memory_id: int, content: str, tags: str | None, category: str | None) -> None:
    store = SessionStore(paths.database_path())
    if not store.update_memory(memory_id, content, tags=tags, category=category):
        raise click.ClickException(f"memory not found: {memory_id}")
    click.echo(f"updated memory {memory_id}")


@memory_group.command("delete")
@click.argument("memory_id", type=int)
@click.confirmation_option(prompt="Delete this memory?")
def memory_delete(memory_id: int) -> None:
    if not SessionStore(paths.database_path()).delete_memory(memory_id):
        raise click.ClickException(f"memory not found: {memory_id}")
    click.echo(f"deleted memory {memory_id}")


@main.group("privacy")
def privacy_group() -> None:
    pass


@privacy_group.command("export")
def privacy_export() -> None:
    data = SessionStore(paths.database_path()).export_privacy_data()
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@privacy_group.command("purge")
@click.confirmation_option(prompt="Clear saved sessions, memories, and local activity metadata?")
def privacy_purge() -> None:
    counts = SessionStore(paths.database_path()).purge_privacy_data()
    click.echo(json.dumps({"purged": counts}, ensure_ascii=False))
