from __future__ import annotations

import os
import shutil
from collections.abc import Mapping

from eurus.tui.workspace.geometry import (
    WORKSPACE_MIN_COLUMNS,
    PanelFrame,
    PanelLayout,
    compute_layout,
    workspace_supported,
)
from eurus.tui.workspace.store import WorkspaceStore

WORKSPACE_MODES = ("auto", "on", "off")


def workspace_setting(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("EURUS_WORKSPACE", "auto").strip().lower()
    return value if value in WORKSPACE_MODES else "auto"


def workspace_mode(
    environ: Mapping[str, str] | None = None,
    *,
    columns: int | None = None,
    tty: bool | None = None,
) -> bool:
    setting = workspace_setting(environ)
    if setting == "on":
        return True
    if setting == "off":
        return False
    if tty is None:
        try:
            tty = sys_is_tty()
        except (OSError, ValueError):
            tty = False
    if not tty:
        return False
    width = shutil.get_terminal_size((80, 24)).columns if columns is None else columns
    return workspace_supported(width)


def sys_is_tty() -> bool:
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


__all__ = [
    "PanelFrame",
    "PanelLayout",
    "WorkspaceStore",
    "WORKSPACE_MIN_COLUMNS",
    "compute_layout",
    "workspace_mode",
    "workspace_setting",
    "workspace_supported",
]
