from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from eurus.models import ToolResult

PROGRESS_WINDOW = 6


@dataclass(slots=True)
class IterationBudget:
    limit: int = 500
    used: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("iteration budget must be at least 1")
        if self.used < 0:
            raise ValueError("used iterations cannot be negative")

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def for_subagent(self) -> IterationBudget:
        return IterationBudget(limit=min(self.limit, 50))


@dataclass(slots=True)
class ProgressTracker:
    window: int = PROGRESS_WINDOW
    seen_outputs: dict[str, set[str]] = field(default_factory=dict)
    markers: list[bool] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("progress window must be at least 2")

    def record_iteration(
        self, results: Sequence[ToolResult], assistant_text: str, prior_text: str
    ) -> bool:
        progressed = False
        for result in results:
            if result.is_error:
                continue
            if self._materially_new(result.name, normalize_text(result.output)):
                self.seen_outputs.setdefault(result.name, set()).add(normalize_text(result.output))
                progressed = True
        if (
            not results
            and assistant_text.strip()
            and normalize_text(assistant_text) != normalize_text(prior_text)
        ):
            progressed = True
        self.markers.append(progressed)
        return progressed

    def _materially_new(self, name: str, normalized: str) -> bool:
        known = self.seen_outputs.get(name)
        if not known:
            return True
        if normalized in known:
            return False
        return not any(
            normalized.startswith(f"{other} ") or other.startswith(f"{normalized} ")
            for other in known
        )

    def stalled(self) -> bool:
        if len(self.markers) < self.window:
            return False
        return not any(self.markers[-self.window :])


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())
