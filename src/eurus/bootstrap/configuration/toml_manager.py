from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eurus.bootstrap.configuration.env_manager import atomic_write, ensure_private_dir
from eurus.bootstrap.models import BootstrapError
from eurus.config import build_config, deep_merge, read_toml

CONFIG_PERMISSION = 0o600

_SIMPLE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def dumps(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _emit_table(data, (), lines)
    return "\n".join(lines) + ("\n" if lines else "")


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise BootstrapError(f"cannot serialize TOML value: {value!r}")


def _emit_table(table: Mapping[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = [(key, value) for key, value in table.items() if not isinstance(value, Mapping)]
    tables = [(key, value) for key, value in table.items() if isinstance(value, Mapping)]
    for key, value in scalars:
        if isinstance(value, list):
            lines.append(f"{_key_name(key)} = {_format_array(value)}")
        else:
            lines.append(f"{_key_name(key)} = {_format_scalar(value)}")
    for key, value in tables:
        section = (*(prefix), _key_name(key))
        header = "[" + ".".join(section) + "]"
        if lines:
            lines.append("")
        lines.append(header)
        _emit_table(value, section, lines)


def _key_name(key: str) -> str:
    if _SIMPLE_KEY.match(key):
        return key
    return json.dumps(key)


def _format_array(items: list[Any]) -> str:
    return "[" + ", ".join(_format_scalar(item) for item in items) + "]"


class TomlManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}

    @classmethod
    def load(cls, path: Path) -> TomlManager:
        instance = cls(path)
        if path.is_file():
            try:
                instance.data = read_toml(path)
            except Exception as exc:
                raise BootstrapError(f"cannot read {path}: {exc}") from exc
        return instance

    def merge(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        self.data = deep_merge(self.data, dict(updates))
        return self.data

    def validate(self, environ: Mapping[str, str] | None = None) -> None:
        try:
            build_config(self.data, environ)
        except Exception as exc:
            raise BootstrapError(f"merged configuration is invalid: {exc}") from exc

    def save(
        self, *, environ: Mapping[str, str] | None = None, backup_dir: Path | None = None
    ) -> None:
        self.validate(environ)
        if backup_dir is not None and self.path.is_file():
            from eurus.bootstrap.state.backup import backup_file

            backup_file(self.path, backup_dir)
        ensure_private_dir(self.path.parent)
        atomic_write(self.path, dumps(self.data), mode=CONFIG_PERMISSION)

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    def update_section(self, name: str, values: Mapping[str, Any]) -> None:
        section = self.data.get(name)
        if not isinstance(section, Mapping):
            section = {}
        section = deep_merge(section, dict(values))
        self.data[name] = section
