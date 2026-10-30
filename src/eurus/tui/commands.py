from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    usage: str = ""
    shortcut: str = ""


COMMANDS: tuple[Command, ...] = (
    Command("/help", "Show commands and shortcuts", shortcut="F1"),
    Command("/model", "Switch provider and model", "/model [provider|provider/model]", "Ctrl+P"),
    Command("/models", "List configured model choices"),
    Command("/provider", "Show provider credential status"),
    Command("/skills", "List skills and which are active"),
    Command(
        "/memory",
        "Review and manage saved memories",
        "/memory [list|search|add|edit|delete] ...",
    ),
    Command(
        "/theme",
        "Open the theme picker or switch, preview and create color themes",
        "/theme [name|list|preview|new|path|termux|reset]",
        "Ctrl+T",
    ),
    Command(
        "/font",
        "List, install or reset the terminal font",
        "/font [list|install|status|reset]",
    ),
    Command("/glyphs", "Switch the icon set", "/glyphs [unicode|ascii|nerd|auto]"),
    Command("/sessions", "List saved conversations"),
    Command("/resume", "Resume a saved conversation", "/resume [latest|session-id]"),
    Command("/tools", "List registered tools"),
    Command(
        "/browser",
        "Show browser automation status; optionally stop the browser",
        "/browser [status|stop]",
    ),
    Command(
        "/grants",
        "Review or revoke session approval grants",
        "/grants [revoke <fingerprint> | revoke-all]",
    ),
    Command("/status", "Show session, model and token status"),
    Command("/config", "Show effective settings"),
    Command("/workspace", "Show the active workspace", shortcut="Ctrl+W"),
    Command("/settings", "Show or change interface settings", shortcut="Ctrl+O"),
    Command("/compact", "Trim old conversation context"),
    Command("/retry", "Resend the last cancelled prompt"),
    Command("/undo", "Restore files changed by the latest run", "/undo [run-id]"),
    Command("/clear", "Clear conversation and screen", shortcut="Ctrl+N"),
    Command("/exit", "Leave Eurus"),
)

ALIASES: dict[str, str] = {"/quit": "/exit", "/?": "/help"}


def resolve(name: str, commands: Sequence[Command] = COMMANDS) -> list[Command]:
    key = ALIASES.get(name.lower(), name.lower())
    exact = [command for command in commands if command.name == key]
    if exact:
        return exact
    return [command for command in commands if command.name.startswith(key)]


def suggest(name: str, commands: Sequence[Command] = COMMANDS) -> list[Command]:
    names = difflib.get_close_matches(
        name.lower(), [command.name for command in commands], n=3, cutoff=0.5
    )
    return [command for command in commands if command.name in names]
