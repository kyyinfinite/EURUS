from __future__ import annotations

from dataclasses import dataclass

MAX_REVISION_CEILING = 10
MODEL_CALL_CEILING = 400
WORKER_CEILING = 12
DEADLINE_CEILING = 7200.0
SCORE_CEILING = 100.0


@dataclass(frozen=True, slots=True)
class OfficeConfig:
    enabled: bool = True
    max_revision_cycles: int = 3
    max_model_calls: int = 60
    max_workers: int = 3
    deadline_seconds: float = 1800.0
    tool_timeout_seconds: float = 1800.0
    content_score_min: float = 85.0
    visual_score_min: float = 85.0
    structural_score_min: float = 95.0
    allow_research_delegation: bool = True

    def thresholds(self) -> tuple[float, float, float]:
        return (
            self.content_score_min,
            self.visual_score_min,
            self.structural_score_min,
        )
