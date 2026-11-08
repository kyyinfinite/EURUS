from __future__ import annotations

from dataclasses import dataclass, field

from eurus.models import Usage

WINDDOWN_THRESHOLD = 0.85
MAX_TRACKED_CALLS = 50
MIN_CALLS_FOR_WINDDOWN = 2


@dataclass(slots=True)
class TokenGovernor:
    winddown_threshold: float = WINDDOWN_THRESHOLD
    calls_observed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    input_history: list[int] = field(default_factory=list)
    winddown_requested: bool = False

    def __post_init__(self) -> None:
        if not 0.5 <= self.winddown_threshold < 1.0:
            raise ValueError("winddown_threshold must be within [0.5, 1.0)")

    def observe(self, usage: Usage) -> None:
        self.calls_observed += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens
        self.input_history.append(usage.input_tokens)
        if len(self.input_history) > MAX_TRACKED_CALLS:
            del self.input_history[:-MAX_TRACKED_CALLS]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def average_input_per_call(self) -> int:
        if not self.input_history:
            return 0
        return sum(self.input_history) // len(self.input_history)

    @property
    def context_growth_per_call(self) -> int:
        if len(self.input_history) < 2:
            return 0
        deltas = [
            right - left
            for left, right in zip(self.input_history, self.input_history[1:], strict=False)
        ]
        return sum(deltas) // len(deltas)

    @property
    def cache_read_ratio(self) -> float:
        if self.input_tokens <= 0:
            return 0.0
        return self.cache_read_tokens / self.input_tokens

    def remaining_budget(self, tokens_used: int, max_tokens: int | None) -> int | None:
        if max_tokens is None:
            return None
        return max(0, max_tokens - tokens_used)

    def should_winddown(
        self,
        *,
        tokens_used: int,
        max_tokens: int | None,
        cost_used: float | None = None,
        max_cost: float | None = None,
        cost_known: bool = True,
    ) -> bool:
        if self.winddown_requested:
            return False
        if self.calls_observed < MIN_CALLS_FOR_WINDDOWN:
            return False
        if max_tokens is not None:
            if tokens_used >= max_tokens * self.winddown_threshold:
                return True
            projected = tokens_used + self.average_input_per_call
            if projected >= max_tokens and self.average_input_per_call > 0:
                return True
        return bool(
            max_cost is not None
            and cost_known
            and cost_used is not None
            and cost_used >= max_cost * self.winddown_threshold
        )

    def request_winddown(self) -> None:
        self.winddown_requested = True

    def winddown_note(self, tokens_used: int, max_tokens: int | None) -> str:
        if max_tokens is not None:
            budget = (
                f"{round(100 * tokens_used / max_tokens)}% of the token budget "
                f"({tokens_used}/{max_tokens} tokens)"
            )
        else:
            budget = "most of the cost budget"
        return (
            f"{budget} is spent and the next iteration may not fit. Produce "
            "your final answer now. Do not start new tool calls, planning, or "
            "exploration; summarize what was accomplished, what remains "
            "unfinished, and any caveats."
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "calls_observed": self.calls_observed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_ratio": round(self.cache_read_ratio, 4),
            "average_input_per_call": self.average_input_per_call,
            "context_growth_per_call": self.context_growth_per_call,
            "winddown_requested": self.winddown_requested,
        }

    def status_line(self, tokens_used: int, max_tokens: int | None) -> str:
        if max_tokens is not None:
            share = round(100 * tokens_used / max_tokens)
            return (
                f"tokens: {tokens_used}/{max_tokens} used ({share}%), "
                f"{self.calls_observed} model calls, "
                f"avg input {self.average_input_per_call}"
            )
        return (
            f"tokens: {tokens_used} used, {self.calls_observed} model calls, "
            f"avg input {self.average_input_per_call}"
        )
