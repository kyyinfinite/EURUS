"""Lightweight, opt-in timing spans for hot paths.

Enabled only when ``EURUS_TIMING`` is set (or ``enable()`` is called by the
benchmark). Every public entry point early-returns when disabled, so the
disabled overhead is a single boolean check.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

TIMING_ENV = "EURUS_TIMING"
DUMP_ENV = "EURUS_TIMING_DUMP"
MAX_SPANS = 8192

_enabled = False
_lock = threading.Lock()
_spans: list[_Span] = []
_sql_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _Span:
    name: str
    seconds: float
    at: float
    attrs: dict[str, Any] = field(default_factory=dict)


def enable() -> None:
    global _enabled
    _enabled = True


def disable() -> None:
    global _enabled
    _enabled = False


def enabled() -> bool:
    return _enabled


def _env_enabled() -> bool:
    return os.environ.get(TIMING_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def autodetect() -> bool:
    if _env_enabled():
        enable()
    return _enabled


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    if not _enabled:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - started, **attrs)


def record(name: str, seconds: float, **attrs: Any) -> None:
    if not _enabled:
        return
    item = _Span(name, seconds, time.time(), dict(attrs))
    with _lock:
        _spans.append(item)
        if len(_spans) > MAX_SPANS:
            del _spans[: len(_spans) - MAX_SPANS]


_sql_statements = 0
_sql_seconds = 0.0


def record_sql(statements: int, seconds: float) -> None:
    global _sql_statements, _sql_seconds
    if not _enabled:
        return
    with _sql_lock:
        _sql_statements += statements
        _sql_seconds += seconds


def sql_snapshot() -> tuple[int, float]:
    with _sql_lock:
        return _sql_statements, _sql_seconds


def reset_sql() -> None:
    global _sql_statements, _sql_seconds
    with _sql_lock:
        _sql_statements = 0
        _sql_seconds = 0.0


def clear() -> None:
    with _lock:
        _spans.clear()
    reset_sql()


def snapshot() -> list[dict[str, Any]]:
    with _lock:
        items = list(_spans)
    return [
        {"name": item.name, "ms": round(item.seconds * 1000, 3), "at": item.at, **item.attrs}
        for item in items
    ]


def summary() -> dict[str, dict[str, float]]:
    with _lock:
        items = list(_spans)
    aggregated: dict[str, dict[str, float]] = {}
    statements, sql_seconds = sql_snapshot()
    for item in items:
        bucket = aggregated.setdefault(item.name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        bucket["count"] += 1
        bucket["total_ms"] += item.seconds * 1000
        bucket["max_ms"] = max(bucket["max_ms"], item.seconds * 1000)
    for bucket in aggregated.values():
        bucket["avg_ms"] = bucket["total_ms"] / bucket["count"] if bucket["count"] else 0.0
        bucket["total_ms"] = round(bucket["total_ms"], 3)
        bucket["avg_ms"] = round(bucket["avg_ms"], 3)
        bucket["max_ms"] = round(bucket["max_ms"], 3)
    if statements:
        aggregated["<sqlite>"] = {
            "count": float(statements),
            "total_ms": round(sql_seconds * 1000, 3),
            "max_ms": 0.0,
            "avg_ms": round(sql_seconds * 1000 / statements, 3),
        }
    return aggregated


def format_summary(title: str = "timing summary") -> str:
    rows = summary()
    if not rows:
        return f"{title}: (no spans recorded)"
    width = max(len(name) for name in rows)
    lines = [f"{title} ({len(rows)} labels):"]
    for name, bucket in sorted(rows.items()):
        lines.append(
            f"  {name:<{width}}  count={int(bucket['count']):<6} "
            f"total={bucket['total_ms']:.1f}ms avg={bucket['avg_ms']:.2f}ms "
            f"max={bucket['max_ms']:.1f}ms"
        )
    return "\n".join(lines)


def dump(path: str, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"spans": snapshot(), "summary": summary()}
    if extra:
        payload.update(extra)
    target = os.fspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


autodetect()
