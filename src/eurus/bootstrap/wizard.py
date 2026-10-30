from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eurus.bootstrap.configuration.secrets import hidden_input
from eurus.bootstrap.models import SkipStep

STATUS_STYLES = {
    "READY": "green",
    "OK": "green",
    "MISSING": "yellow",
    "OUTDATED": "yellow",
    "WARN": "yellow",
    "BROKEN": "red",
    "BLOCKED": "red",
    "ERROR": "red",
    "OPTIONAL": "bright_black",
    "UNKNOWN": "bright_black",
    "SKIP": "bright_black",
}

INTERACTIVE_STREAMS = ("dumb",)


@dataclass(slots=True)
class BootstrapIO:
    console: Console = field(default_factory=lambda: Console(emoji=False))
    non_interactive: bool = False
    assume_yes: bool = False
    quiet: bool = False

    def interactive(self) -> bool:
        if self.non_interactive:
            return False
        try:
            return sys.stdin is not None and sys.stdin.isatty() and sys.stdout is not None
        except (OSError, ValueError):
            return False

    def ask(self, prompt: str, default: str = "") -> str:
        if not self.interactive():
            return default
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default
        return answer or default

    def hidden(self, prompt: str) -> str:
        if not self.interactive():
            return ""
        try:
            return hidden_input(f"{prompt}: ").strip()
        except (EOFError, OSError):
            return ""

    def confirm(self, prompt: str, default: bool = False) -> bool:
        if self.assume_yes:
            return True
        if not self.interactive():
            return default
        suffix = " [Y/n]" if default else " [y/N]"
        try:
            answer = input(f"{prompt}{suffix}: ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        return answer in {"y", "yes"}

    def choose(self, prompt: str, options: list[str], default_index: int = 0) -> int:
        for index, label in enumerate(options, start=1):
            self.echo(f"  {index}. {label}")
        if not self.interactive():
            return default_index
        raw = self.ask(prompt, str(default_index + 1))
        try:
            index = int(raw) - 1
        except ValueError:
            return default_index
        if 0 <= index < len(options):
            return index
        return default_index

    def echo(self, text: str = "") -> None:
        if not self.quiet:
            self.console.print(text, markup=False)

    def info(self, text: str) -> None:
        if not self.quiet:
            self.console.print(f"[bright_black]{text}[/bright_black]")

    def ok(self, text: str) -> None:
        if not self.quiet:
            self.console.print(f"[green]{text}[/green]")

    def warn(self, text: str) -> None:
        if not self.quiet:
            self.console.print(f"[yellow]{text}[/yellow]")

    def error(self, text: str) -> None:
        self.console.print(f"[red]{text}[/red]")

    def status_line(self, label: str, status: str, detail: str = "") -> None:
        style = STATUS_STYLES.get(status, "white")
        prefix = "[OK]" if status in {"READY", "OK"} else f"[{status}]"
        if not self.quiet:
            self.console.print(
                f"[{style}]{prefix:<9}[/{style}] {label}" + (f" - {detail}" if detail else "")
            )

    def require_interactive(self, message: str) -> None:
        if not self.interactive():
            raise SkipStep(message)

    def spinner(self, message: str) -> Any:
        return self.console.status(message, spinner="line")


@dataclass(slots=True)
class WizardChoice:
    key: str
    label: str
    description: str = ""


def render_choice_table(io: BootstrapIO, choices: list[WizardChoice]) -> None:
    table = Table(show_header=True, header_style="bold", pad_edge=False, box=None)
    table.add_column("#", justify="right", width=3)
    table.add_column("Option")
    table.add_column("Description")
    for index, choice in enumerate(choices, start=1):
        table.add_row(str(index), choice.label, choice.description)
    io.console.print(table)


def selection_summary(title: str, rows: list[tuple[str, str]]) -> Panel:
    body = Text()
    for label, value in rows:
        body.append(f"{label:<24}", style="bold")
        body.append(f"{value}\n")
    return Panel(body, title=title, expand=False)


def abort(io: BootstrapIO, message: str) -> None:
    io.error(message)
    raise SkipStep(message)
