from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

TUI_FILE = "tui.toml"
TABLE = "tui"
LEGACY_FILES = ("theme", "glyphs")
KEYS = ("theme", "font", "glyphs")


@dataclass(frozen=True, slots=True)
class TuiChoices:
    theme: str = ""
    font: str = ""
    glyphs: str = ""

    def with_key(self, key: str, value: str) -> TuiChoices:
        return replace(self, **{key: value})


def _escape(value: str) -> str:
    out = []
    for char in value:
        if char in ("\x00", "\n", "\r", "\t", "\\", '"') or ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def read_choices(home: Path) -> TuiChoices:
    try:
        with (home / TUI_FILE).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return TuiChoices()
    table = data.get(TABLE)
    if not isinstance(table, dict):
        return TuiChoices()
    values: dict[str, str] = {}
    for key in KEYS:
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    return TuiChoices(**values)


def write_choices(choices: TuiChoices, home: Path) -> bool:
    try:
        home.mkdir(parents=True, exist_ok=True)
        lines = [f"[{TABLE}]"]
        for key in KEYS:
            value = getattr(choices, key)
            if value:
                lines.append(f"{key} = {_escape(value)}")
        target = home / TUI_FILE
        temporary = home / (TUI_FILE + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(target)
        return True
    except OSError:
        return False


def _read_legacy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def migrate_legacy_files(home: Path) -> bool:
    """Import legacy theme/glyphs marker files into tui.toml once.

    Returns True when a migration happened. Legacy files are removed only after
    tui.toml was written successfully; they are never read again afterwards.
    """
    target = home / TUI_FILE
    if target.exists():
        return False
    legacy_theme = _read_legacy(home / "theme")
    legacy_glyphs = _read_legacy(home / "glyphs").lower()
    if not legacy_theme and not legacy_glyphs:
        return False
    if not write_choices(TuiChoices(theme=legacy_theme, glyphs=legacy_glyphs), home):
        return False
    for name in LEGACY_FILES:
        try:
            (home / name).unlink(missing_ok=True)
        except OSError:
            return False
    return True


def load_choices(home: Path) -> TuiChoices:
    migrate_legacy_files(home)
    return read_choices(home)


def update_key(home: Path, key: str, value: str) -> bool:
    return write_choices(load_choices(home).with_key(key, value), home)
