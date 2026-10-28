from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from eurus.errors import (
    AllProvidersFailedError,
    AuthenticationError,
    ConfigError,
    InvalidRequestError,
    PolicyError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ToolError,
)

CORRELATION_FIELDS = ("correlation_id", "task_id", "session_id", "platform")
MAX_FIELD_LENGTH = 128

ErrorCategory = Literal[
    "cancelled",
    "timeout",
    "authentication",
    "rate_limit",
    "invalid_request",
    "provider_unavailable",
    "provider",
    "tool",
    "policy",
    "config",
    "validation",
    "network",
    "internal",
]

_UNSAFE = re.compile(r"[^\w:.\-@/]")
_EMPTY: Mapping[str, str] = MappingProxyType({})
_context: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "eurus_log_context", default=_EMPTY
)


def sanitize_field(value: str) -> str:
    return _UNSAFE.sub("_", value.strip())[:MAX_FIELD_LENGTH]


def _merge(
    base: Mapping[str, str], fields: Mapping[str, str | None], *, strict: bool = True
) -> dict[str, str]:
    merged = dict(base)
    for name, value in fields.items():
        if name not in CORRELATION_FIELDS:
            if strict:
                raise ValueError(f"unknown correlation field: {name}")
            continue
        if value:
            cleaned = sanitize_field(value)
            if cleaned:
                merged[name] = cleaned
    return merged


def current_context() -> dict[str, str]:
    return dict(_context.get())


def bind_context(**fields: str | None) -> contextvars.Token[Mapping[str, str]]:
    merged = _merge(_context.get(), fields)
    return _context.set(MappingProxyType(merged))


def unbind_context(token: contextvars.Token[Mapping[str, str]]) -> None:
    _context.reset(token)


def correlation_for(record: logging.LogRecord) -> dict[str, str]:
    explicit = getattr(record, "correlation", None)
    if isinstance(explicit, Mapping):
        return {str(name): str(value) for name, value in explicit.items()}
    return current_context()


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        explicit = getattr(record, "correlation", None)
        merged = current_context()
        if isinstance(explicit, Mapping):
            merged = _merge(merged, {str(k): str(v) for k, v in explicit.items()}, strict=False)
        record.correlation = merged
        return True


def correlation_suffix(fields: Mapping[str, str]) -> str:
    return " ".join(f"{name}={fields[name]}" for name in CORRELATION_FIELDS if name in fields)


def classify_error(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, AuthenticationError):
        return "authentication"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, InvalidRequestError):
        return "invalid_request"
    if isinstance(exc, ProviderUnavailableError):
        return "provider_unavailable"
    if isinstance(exc, ProviderError | AllProvidersFailedError):
        return "provider"
    if isinstance(exc, ToolError):
        return "tool"
    if isinstance(exc, PolicyError):
        return "policy"
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, ValueError | KeyError):
        return "validation"
    if isinstance(exc, OSError):
        return "network"
    return "internal"


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
