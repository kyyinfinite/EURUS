from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        *,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1 or max_keys < 1:
            raise ValueError("rate limiter limits must be positive")
        if window_seconds <= 0:
            raise ValueError("rate limiter window must be positive")
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self.clock = clock
        self._events: dict[str, deque[float]] = {}

    @property
    def tracked_keys(self) -> int:
        return len(self._events)

    def allow(self, key: str) -> bool:
        now = self.clock()
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.max_keys:
                self._evict(now)
            events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        if len(events) >= self.max_events:
            return False
        events.append(now)
        return True

    def retry_after(self, key: str) -> float:
        events = self._events.get(key)
        if not events or len(events) < self.max_events:
            return 0.0
        return max(0.0, self.window_seconds - (self.clock() - events[0]))

    def _evict(self, now: float) -> None:
        idle = [
            key
            for key, events in self._events.items()
            if not events or now - events[-1] >= self.window_seconds
        ]
        for key in idle:
            del self._events[key]
        if len(self._events) >= self.max_keys:
            del self._events[next(iter(self._events))]
