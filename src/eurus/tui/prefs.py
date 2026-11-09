from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from eurus import paths

PREFS_FILE = "ui.json"
BOOL_KEYS = ("sidebar", "stream_preview", "markdown")


@dataclass(slots=True)
class ViewPrefs:
    sidebar: bool = True
    stream_preview: bool = True
    markdown: bool = True


def load(environ: Mapping[str, str] | None = None, home: Path | None = None) -> ViewPrefs:
    base = home or paths.home_dir(environ)
    prefs = ViewPrefs()
    try:
        data = json.loads((base / PREFS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    if not isinstance(data, dict):
        return prefs
    for key in BOOL_KEYS:
        value = data.get(key)
        if isinstance(value, bool):
            setattr(prefs, key, value)
    return prefs


def save(prefs: ViewPrefs, home: Path | None = None) -> Path | None:
    base = home or paths.home_dir()
    try:
        paths.ensure_private_dir(base)
        target = base / PREFS_FILE
        target.write_text(json.dumps(asdict(prefs), indent=2) + "\n", encoding="utf-8")
        return target
    except OSError:
        return None
