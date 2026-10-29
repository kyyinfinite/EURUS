from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

MASK = "REDACTED"

# Single source of truth for secret-shaped values. Every checker in the code
# base (log scrubbing, boundary details, memory privacy, patch review, chat
# transports) matches this one ordered list.
_SECRET_PATTERN_TEXT = (
    r"\bsk-[A-Za-z0-9_-]{8,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bgh[opusr]_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    r"\bgsk_[A-Za-z0-9]{8,}\b",
    r"\bnvapi-[A-Za-z0-9_-]{8,}\b",
    r"\bAIza[0-9A-Za-z_-]{20,}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
    r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])",
    r"(?i)\b(?:api[_-]?key|access[_-]?token|session[_-]?cookie)\b\s*[:=]\s*\S+",
    r"(?i)\b(?:my\s+)?(?:password|passphrase|private\s+key|recovery\s+phrase)\s+(?:is|=|:)\s*\S+",
)
SECRET_PATTERNS = tuple(re.compile(pattern) for pattern in _SECRET_PATTERN_TEXT)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)

# Memory privacy is stricter than display scrubbing: any sensitive-name
# assignment ("password: hunter2", "token: abc") rejects the content outright,
# while display scrubbing keeps "normal token: value" readable.
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|session[_ -]?cookie|password|passwd|secret|"
    r"token|authorization|cookie|credential)\b\s*[:=]\s*\S+"
)

# Key names whose VALUES must be masked. Matching is word-aware: a key matches
# when its normalized form equals one of these names or ends with one of the
# suffixes, so "keyword" and "primary_key" stay visible while "api_key",
# "session_token" or "x-password" are masked.
SENSITIVE_KEY_NAMES = frozenset(
    {
        "token",
        "password",
        "passwd",
        "secret",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "auth",
        "api_key",
        "apikey",
        "private_key",
        "passphrase",
    }
)
SENSITIVE_KEY_SUFFIXES = (
    "_token",
    "_password",
    "_passwd",
    "_secret",
    "_credential",
    "_cookie",
    "_api_key",
    "_apikey",
    "_private_key",
    "_passphrase",
)

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_ASSIGNMENT = re.compile(r"(?i)\b([A-Za-z_][A-Za-z0-9_.\-]*)\s*([=:])[ \t]*(\S+(?:[ \t]+\S+)*)")


def normalize_key(key: str) -> str:
    cleaned = _CAMEL_BOUNDARY.sub(r"\1_\2", key)
    return _NON_WORD.sub("_", cleaned.lower()).strip("_")


def is_sensitive_key(key: str) -> bool:
    normalized = normalize_key(key)
    if not normalized:
        return False
    if normalized in SENSITIVE_KEY_NAMES:
        return True
    return normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def scrub_secrets(text: str) -> str:
    text = _PRIVATE_KEY_BLOCK.sub(MASK, text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def _assignment_repl(match: re.Match[str]) -> str:
    if not is_sensitive_key(match.group(1)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{MASK}"


def redact_text(text: str) -> str:
    return scrub_secrets(_ASSIGNMENT.sub(_assignment_repl, text))


def redact_value(key: str, value: Any) -> Any:
    if is_sensitive_key(key):
        return MASK
    if isinstance(value, Mapping):
        return {str(name): redact_value(str(name), item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value("", item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {str(name): redact_value(str(name), item) for name, item in arguments.items()}


def summarize_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(redact_arguments(arguments), ensure_ascii=False, default=str)
