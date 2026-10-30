from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Iterable

from eurus.logs import redact

MASK_CHAR = "…"
MIN_MASK_LEN = 8


def mask_secret(value: str) -> str:
    text = value.strip()
    if not text:
        return "(not set)"
    if len(text) < MIN_MASK_LEN:
        return "***"
    return f"{text[:4]}{MASK_CHAR}{text[-4:]}"


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    return redact(text, secrets)


def validate_secret_format(name: str, value: str) -> str:
    text = value.strip()
    if not text:
        return f"{name} must not be empty"
    if len(text) > 512:
        return f"{name} is too long"
    if text.lower().startswith("bearer "):
        return f"{name} should not include the bearer prefix"
    if re.search(r"[\s'\"]", text):
        return f"{name} must not contain spaces, quotes or line breaks"
    return ""


def validate_telegram_token(token: str) -> str:
    text = token.strip()
    if not text:
        return "bot token must not be empty"
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", text):
        return "that does not look like a Telegram bot token (expected numeric-id:secret)"
    return ""


def hidden_input(prompt: str) -> str:
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(prompt)
    except (OSError, ValueError, EOFError):
        pass
    return getpass.getpass(prompt)


def secrets_from_environ(environ: dict[str, str]) -> tuple[str, ...]:
    values = [
        value.strip()
        for name, value in environ.items()
        if (name.endswith("_API_KEY") or name == "EURUS_TELEGRAM_BOT_TOKEN") and value.strip()
    ]
    return tuple(values)


def clean_state_payload(
    payload: dict[str, object],
    forbidden_keys: tuple[str, ...] = ("api_key", "token", "secret", "password"),
) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(item in lowered for item in forbidden_keys):
            cleaned[key] = "[REDACTED]"
        elif isinstance(value, dict):
            cleaned[key] = clean_state_payload(value, forbidden_keys)
        else:
            cleaned[key] = value
    return cleaned
