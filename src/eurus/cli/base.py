from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from eurus import __version__, paths
from eurus.config import LOG_LEVELS, Config, load_config
from eurus.dotenv import load_dotenv
from eurus.errors import ConfigError
from eurus.logs import collect_secrets, setup_logging, shutdown_logging


class State:
    def __init__(self, config_path: Path | None, overrides: dict[str, Any]) -> None:
        self.config_path = config_path
        self.overrides = overrides
        self.console_logging = True
        self._config: Config | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                config = load_config(config_path=self.config_path, overrides=self.overrides)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from exc
            setup_logging(
                config.logging,
                secrets=collect_secrets(config),
                console=self.console_logging,
            )
            self._config = config
        return self._config


def _state(ctx: click.Context) -> State:
    obj = ctx.find_root().obj
    if not isinstance(obj, State):
        raise click.ClickException("internal error: state not initialised")
    return obj


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="eurus")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Use this config file instead of the user and project files.",
)
@click.option("--model", default=None, help="Override agent.model, as provider/model.")
@click.option(
    "--log-level",
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    default=None,
    help="Override logging.level.",
)
@click.pass_context
def main(
    ctx: click.Context,
    config_path: Path | None,
    model: str | None,
    log_level: str | None,
) -> None:
    overrides: dict[str, Any] = {}
    if model:
        overrides["agent"] = {"model": model}
    if log_level:
        overrides["logging"] = {"level": log_level.upper()}
    if os.environ.get("EURUS_NO_DOTENV", "").strip() not in ("1", "true"):
        load_dotenv([Path.cwd() / ".env", paths.home_dir() / ".env"], os.environ)
    ctx.call_on_close(shutdown_logging)
    ctx.obj = State(config_path, overrides)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
