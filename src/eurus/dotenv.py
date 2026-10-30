from __future__ import annotations

import re
from collections.abc import MutableMapping, Sequence
from pathlib import Path

KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXTRA_ALLOWED = frozenset({"OLLAMA_BASE_URL", "NVIDIA_BASE_URL"})
EURUS_ENV_ALLOWED = frozenset(
    {
        "EURUS_MODEL",
        "EURUS_SKILLS",
        "EURUS_FALLBACK",
        "EURUS_MAX_STEPS",
        "EURUS_TEMPERATURE",
        "EURUS_PERMISSION_MODE",
        "EURUS_WORKSPACE",
        "EURUS_LOG_LEVEL",
        "EURUS_LOG_FILE",
        "EURUS_LOG_JSON",
        "EURUS_TIMEOUT",
        "EURUS_MAX_RETRIES",
        "EURUS_LOCAL_ONLY",
        "EURUS_COOLDOWN",
        "EURUS_MAX_COOLDOWN",
        "EURUS_RETRY_JITTER",
        "EURUS_FIRST_TOKEN",
        "EURUS_TELEGRAM_BOT_TOKEN",
        "EURUS_TELEGRAM_ALLOWED_USER_IDS",
        "EURUS_TELEGRAM_ALLOWED_GROUP_IDS",
        "EURUS_TELEGRAM_APPROVAL_TIMEOUT",
        "EURUS_REMINDER_TIMEZONE",
        "EURUS_CHROMIUM",
        "EURUS_CDP_ENDPOINT",
    }
)
QUOTES = "'\""


def is_allowed(name: str) -> bool:
    if name in EURUS_ENV_ALLOWED or name in EXTRA_ALLOWED:
        return True
    if name.startswith("EURUS_"):
        return False
    return name.endswith("_API_KEY")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTES:
        return value[1:-1]
    marker = value.find(" #")
    return value if marker == -1 else value[:marker].rstrip()


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if separator and KEY_NAME.match(name):
            values[name] = _unquote(value.strip())
    return values


def load_dotenv(files: Sequence[Path], environ: MutableMapping[str, str]) -> list[Path]:
    loaded: list[Path] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for name, value in parse_dotenv(text).items():
            if is_allowed(name) and value.strip() and not environ.get(name, "").strip():
                environ[name] = value
        loaded.append(path)
    return loaded
