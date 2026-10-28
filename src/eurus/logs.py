from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from eurus.catalog import resolve_api_key
from eurus.config import Config, LoggingConfig
from eurus.observability import CorrelationFilter, correlation_for, correlation_suffix
from eurus.security.redaction import scrub_secrets

LOGGER_NAME = "eurus"
REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 6
EXTRA_SECRET_ENV = ("EURUS_TELEGRAM_BOT_TOKEN",)

_PREFIXED_SECRETS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gsk_[A-Za-z0-9]{8,}"),
    re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)
_ASSIGNMENT = re.compile(
    r"""(api[_-]?key|token|secret|password)(\s*[=:]\s*)[^\s,;"']+""",
    re.IGNORECASE,
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern in _PREFIXED_SECRETS:
        text = pattern.sub(REDACTED, text)
    text = scrub_secrets(text)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)


def collect_secrets(config: Config, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    values = [resolve_api_key(spec, environ) for spec in config.providers.values()]
    values.extend(source.get(name, "").strip() for name in EXTRA_SECRET_ENV)
    return tuple(value for value in values if value)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str | None = None, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        suffix = correlation_suffix(correlation_for(record))
        if suffix:
            text = f"{text} [{suffix}]"
        return redact(text, self._secrets)


class JsonFormatter(RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **correlation_for(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False), self._secrets)


def shutdown_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def setup_logging(
    settings: LoggingConfig,
    *,
    secrets: Iterable[str] = (),
    console: bool = True,
) -> logging.Logger:
    shutdown_logging()
    secret_values = tuple(secrets)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, settings.level.upper()))
    logger.propagate = False

    if console:
        console_handler: logging.Handler
        if settings.json:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(JsonFormatter(secrets=secret_values))
        else:
            console_handler = RichHandler(
                console=Console(stderr=True),
                show_path=False,
                markup=False,
            )
            console_handler.setFormatter(RedactingFormatter("%(message)s", secret_values))
        console_handler.addFilter(CorrelationFilter())
        logger.addHandler(console_handler)

    if settings.file:
        target = Path(settings.file).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        if settings.json:
            file_handler.setFormatter(JsonFormatter(secrets=secret_values))
        else:
            file_handler.setFormatter(
                RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", secret_values)
            )
        file_handler.addFilter(CorrelationFilter())
        logger.addHandler(file_handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
