from __future__ import annotations

import contextlib
import logging
from collections import deque
from threading import Lock
from typing import Any

from eurus.logs import LOGGER_NAME, redact


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(level=logging.INFO)
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._secrets = tuple(secret for secret in secrets if secret)
        self._sequence = 0
        self._lock = Lock()

    def update_secrets(self, secrets: tuple[str, ...]) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if self._secrets:
            with contextlib.suppress(Exception):
                message = redact(message, secrets=self._secrets)
        with self._lock:
            self._sequence += 1
            self._records.append(
                {
                    "sequence": self._sequence,
                    "timestamp": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message[:2000],
                }
            )

    def snapshot(self, *, after_sequence: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = [item for item in self._records if item["sequence"] > after_sequence]
        return items[-limit:]


def attach_ring_buffer(*, capacity: int = 500, secrets: tuple[str, ...] = ()) -> RingBufferHandler:
    handler = RingBufferHandler(capacity=capacity, secrets=secrets)
    logging.getLogger(LOGGER_NAME).addHandler(handler)
    return handler
