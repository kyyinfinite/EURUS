from __future__ import annotations

import os
from pathlib import Path

from eurus.bootstrap.models import BootstrapError
from eurus.dotenv import KEY_NAME
from eurus.paths import ensure_private_dir as _ensure_private_dir

ENV_PERMISSION = 0o600
DIR_PERMISSION = 0o700


def atomic_write(path: Path, text: str, *, mode: int = ENV_PERMISSION) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_PERMISSION)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_private_dir(path: Path) -> Path:
    return _ensure_private_dir(path)


def tighten_permissions(path: Path) -> bool:
    if not path.exists():
        return False
    before = path.stat().st_mode & 0o777
    desired = 0o600 if path.is_file() else 0o700
    if before == desired:
        return False
    try:
        os.chmod(path, desired)
    except OSError as exc:
        raise BootstrapError(f"cannot tighten permissions on {path}: {exc}") from exc
    return True


class EnvFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []
        self.values: dict[str, str] = {}

    @classmethod
    def load(cls, path: Path) -> EnvFile:
        instance = cls(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return instance
        instance.lines = text.splitlines()
        instance._parse()
        return instance

    def _parse(self) -> None:
        self.values = {}
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            stripped = stripped.removeprefix("export ").lstrip()
            name, separator, value = stripped.partition("=")
            name = name.strip()
            if separator and KEY_NAME.match(name):
                self.values[name] = value.strip().strip("'\"")

    def get(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.values

    def set(self, name: str, value: str) -> bool:
        if not KEY_NAME.match(name):
            raise BootstrapError(f"invalid environment variable name: {name!r}")
        if "\n" in value or "\r" in value:
            raise BootstrapError(f"value for {name} must not contain line breaks")
        old = self.values.get(name)
        self.values[name] = value
        quoted = self._quote(value)
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            candidate = stripped.removeprefix("export ").lstrip()
            candidate_name, sep, _ = candidate.partition("=")
            if sep and candidate_name.strip() == name:
                prefix = "export " if line.strip().startswith("export ") else ""
                self.lines[index] = f"{prefix}{name}={quoted}"
                return old != value
        self.lines.append(f"{name}={quoted}")
        return old != value

    def remove(self, name: str) -> bool:
        if name not in self.values:
            return False
        del self.values[name]
        self.lines = [
            line
            for line in self.lines
            if not (
                line.strip()
                and not line.strip().startswith("#")
                and line.strip().removeprefix("export ").lstrip().partition("=")[0].strip() == name
            )
        ]
        return True

    @staticmethod
    def _quote(value: str) -> str:
        if value and (value[0] in "'\"" or " #" in value):
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return value

    def render(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines else "")

    def save(self) -> bool:
        atomic_write(self.path, self.render(), mode=ENV_PERMISSION)
        return True

    def save_if_changed(self, previous: dict[str, str]) -> bool:
        if self.values == previous:
            return False
        self.save()
        return True

    def snapshot(self) -> dict[str, str]:
        return dict(self.values)


GLOBAL_ENV_TEMPLATE: tuple[str, ...] = (
    "GROQ_API_KEY=",
    "OPENAI_API_KEY=",
    "ANTHROPIC_API_KEY=",
    "GEMINI_API_KEY=",
    "NVIDIA_API_KEY=",
    "OPENROUTER_API_KEY=",
    "DEEPSEEK_API_KEY=",
    "MISTRAL_API_KEY=",
    "TOGETHER_API_KEY=",
    "XAI_API_KEY=",
    "EURUS_TELEGRAM_BOT_TOKEN=",
    "OLLAMA_BASE_URL=http://127.0.0.1:11434",
    "NVIDIA_BASE_URL=",
    "EURUS_MODEL=",
    "EURUS_FALLBACK=",
    "EURUS_PERMISSION_MODE=",
    "EURUS_LOG_LEVEL=",
    "EURUS_TIMEOUT=",
    "EURUS_MAX_RETRIES=",
    "EURUS_LOCAL_ONLY=",
    "EURUS_CDP_ENDPOINT=",
)


def global_env_path(environ: dict[str, str] | None = None) -> Path:
    from eurus import paths

    return paths.home_dir(environ) / ".env"


def ensure_global_env(environ: dict[str, str] | None = None) -> tuple[Path, bool]:
    path = global_env_path(environ)
    created = False
    if not path.exists():
        env = EnvFile(path)
        env.lines = list(GLOBAL_ENV_TEMPLATE)
        env._parse()
        env.save()
        created = True
    tighten_permissions(path)
    return path, created
