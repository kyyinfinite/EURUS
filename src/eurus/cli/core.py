"""Basic inspection and single-shot model commands."""

from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.table import Table

from eurus.catalog import resolve_api_key
from eurus.cli.base import _state, main
from eurus.config import Config
from eurus.diagnose import probe_all
from eurus.errors import ConfigError, EurusError
from eurus.models import Message, ModelRef, Usage
from eurus.providers.factory import build_router, resolve_chain


@main.command()
@click.pass_context
def providers(ctx: click.Context) -> None:
    config = _state(ctx).config
    table = Table(title="providers")
    for column in ("Name", "Protocol", "Base URL", "Key env", "Status"):
        table.add_column(column)
    for name in sorted(config.providers):
        spec = config.providers[name]
        if not spec.api_key_env:
            status = "no key needed"
        elif resolve_api_key(spec):
            status = "ready"
        else:
            status = "missing key"
        table.add_row(spec.name, spec.protocol, spec.base_url, spec.api_key_env or "-", status)
    Console().print(table)


@main.command()
@click.argument("models", nargs=-1)
@click.pass_context
def diagnose(ctx: click.Context, models: tuple[str, ...]) -> None:

    config = _state(ctx).config
    try:
        if models:
            refs = [ModelRef.parse(text) for text in models]
            unknown = [ref.provider for ref in refs if ref.provider not in config.providers]
            if unknown:
                raise ConfigError(f"unknown provider '{unknown[0]}'")
        else:
            refs = resolve_chain(config)
        results = asyncio.run(probe_all(config, refs))
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title="eurus diagnose")
    for column in ("Model", "Result", "Seconds", "Detail"):
        table.add_column(column)
    for result in results:
        status = "[green]ok[/green]" if result.ok else "[red]failed[/red]"
        table.add_row(result.model, status, f"{result.seconds:.1f}", result.detail)
    Console().print(table)
    if not all(result.ok for result in results):
        ctx.exit(1)


async def _ask(config: Config, messages: list[Message], stream: bool) -> None:
    router = build_router(config)
    usage = Usage()
    try:
        if stream:
            async for event in router.stream(messages):
                if event.kind == "text":
                    click.echo(event.text, nl=False)
                elif event.kind == "usage" and event.usage is not None:
                    usage = event.usage
            click.echo()
        else:
            response = await router.chat(messages)
            click.echo(response.message.content)
            usage = response.usage
        if router.last_used is not None:
            click.echo(
                f"[{router.last_used}] in={usage.input_tokens} out={usage.output_tokens}",
                err=True,
            )
    finally:
        await router.aclose()


@main.command()
@click.argument("prompt", nargs=-1, required=True)
@click.option("--system", "system_prompt", default=None, help="Optional system prompt.")
@click.option("--stream/--no-stream", default=True, help="Stream tokens as they arrive.")
@click.pass_context
def ask(
    ctx: click.Context,
    prompt: tuple[str, ...],
    system_prompt: str | None,
    stream: bool,
) -> None:
    config = _state(ctx).config
    messages: list[Message] = []
    if system_prompt:
        messages.append(Message.system(system_prompt))
    messages.append(Message.user(" ".join(prompt)))
    try:
        asyncio.run(_ask(config, messages, stream))
    except EurusError as exc:
        raise click.ClickException(str(exc)) from exc
