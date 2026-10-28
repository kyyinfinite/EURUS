"""TUI theme and font management commands."""

from __future__ import annotations

import click

from eurus.cli.base import main


@main.group("theme", help="Manage TUI color themes.")
def theme_group() -> None:
    return None


@theme_group.command("list")
def theme_list() -> None:
    from eurus.tui import themes

    current = themes.active_name().lower()
    for name in themes.available():
        marker = "*" if name == current else " "
        click.echo(f"{marker} {name:<14} {themes.describe_theme(name)}")


@theme_group.command("use")
@click.argument("name")
def theme_use(name: str) -> None:
    from eurus.tui import themes

    try:
        themes.resolve(name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    themes.save_active(name.strip().lower())
    click.echo(f"Theme: {name.strip().lower()}")


@theme_group.command("new")
@click.argument("name")
@click.option("--base", default="eurus", show_default=True, help="Theme to inherit from.")
def theme_new(name: str, base: str) -> None:
    from eurus.tui import themes

    try:
        click.echo(f"Created {themes.scaffold(name, base)}")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@theme_group.command("path")
def theme_path() -> None:
    from eurus.tui import themes

    click.echo(str(themes.user_dir()))


@main.group("font", help="Install a terminal font for the Eurus TUI.")
def font_group() -> None:
    return None


@font_group.command("list")
def font_list() -> None:
    from eurus.tui import fonts

    for spec in fonts.CATALOG.values():
        click.echo(f"{spec.key:<22} {spec.family} - {spec.note}")


@font_group.command("status")
def font_status() -> None:
    from eurus.tui import fonts

    target = fonts.detect_target()
    click.echo(f"{target.kind}: {fonts.status(target)}")


@font_group.command(
    "install",
    help="Install a catalog font (see `eurus font list`) or a local .ttf/.otf/.zip file.",
)
@click.argument("source")
def font_install(source: str) -> None:
    from eurus.tui import fonts

    try:
        result = fonts.install_source(source)
    except fonts.FontError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(result.message)
    click.echo("Run /glyphs nerd in the TUI to enable icon glyphs.")


@font_group.command("reset")
def font_reset() -> None:
    from eurus.tui import fonts

    try:
        click.echo(fonts.reset(fonts.detect_target()))
    except fonts.FontError as exc:
        raise click.ClickException(str(exc)) from exc
