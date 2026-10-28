"""Configuration commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from eurus import paths
from eurus.cli.base import _state, main
from eurus.config import sample_config


@main.group("config")
def config_group() -> None:
    pass


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    click.echo(json.dumps(_state(ctx).config.to_dict(), indent=2))


@config_group.command("path")
def config_path_command() -> None:
    project = paths.project_config_path(Path.cwd())
    click.echo(f"home: {paths.home_dir()}")
    click.echo(f"user: {paths.user_config_path()}")
    click.echo(f"project: {project if project else '-'}")


@config_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite an existing user config.")
def config_init(force: bool) -> None:
    target = paths.user_config_path()
    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists, use --force to overwrite")
    paths.ensure_private_dir(target.parent)
    target.write_text(sample_config(), encoding="utf-8")
    click.echo(f"written {target}")
