from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus.sessions import DEFAULT_MEMORY_OWNER, SessionStore

log = logging.getLogger(__name__)

DEFAULT_EFFECTIVENESS_WINDOW = 50
DEFAULT_MIN_SAMPLES = 5
DEFAULT_DEMOTE_BELOW = 0.5

__all__ = [
    "SkillEffectiveness",
    "SkillOutcomeHook",
    "SkillOutcomeStore",
    "evaluate_skill_effectiveness",
]


@dataclass(frozen=True, slots=True)
class SkillEffectiveness:
    skill_name: str
    samples: int
    passed: int
    pass_rate: float | None
    should_demote: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "samples": self.samples,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "should_demote": self.should_demote,
            "reason": self.reason,
        }


def evaluate_skill_effectiveness(
    skill_name: str,
    outcomes: Sequence[bool],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    demote_below: float = DEFAULT_DEMOTE_BELOW,
) -> SkillEffectiveness:
    if min_samples < 1:
        raise ValueError("min_samples must be at least 1")
    if not 0.0 <= demote_below <= 1.0:
        raise ValueError("demote_below must be between 0 and 1")
    samples = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome)
    rate = passed / samples if samples else None
    if samples < min_samples:
        return SkillEffectiveness(
            skill_name=skill_name,
            samples=samples,
            passed=passed,
            pass_rate=rate,
            should_demote=False,
            reason=f"insufficient samples ({samples}<{min_samples})",
        )
    assert rate is not None
    if rate < demote_below:
        return SkillEffectiveness(
            skill_name=skill_name,
            samples=samples,
            passed=passed,
            pass_rate=rate,
            should_demote=True,
            reason=f"pass rate {rate:.2f} below demotion threshold {demote_below:.2f}",
        )
    return SkillEffectiveness(
        skill_name=skill_name,
        samples=samples,
        passed=passed,
        pass_rate=rate,
        should_demote=False,
        reason=f"pass rate {rate:.2f} at or above threshold",
    )


class SkillOutcomeStore:
    def __init__(self, database: Path) -> None:
        self.store = SessionStore(database)

    def record(
        self,
        skill_name: str,
        passed: bool,
        *,
        owner_id: str = DEFAULT_MEMORY_OWNER,
        task_ref: str = "",
        details: str = "",
    ) -> int:
        name = skill_name.strip().lower()
        if not name:
            raise ValueError("skill_name must not be empty")
        if len(name) > 64:
            raise ValueError("skill_name exceeds 64 characters")
        with self.store._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO skill_outcomes(skill_name, passed, owner_id, task_ref, details, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name,
                    int(bool(passed)),
                    owner_id.strip() or DEFAULT_MEMORY_OWNER,
                    task_ref[:200],
                    details[:400],
                    datetime.now(UTC).isoformat(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a skill outcome ID")
            return int(cursor.lastrowid)

    def outcomes_for(
        self, skill_name: str, *, owner_id: str = DEFAULT_MEMORY_OWNER, limit: int = 100
    ) -> list[bool]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT passed FROM skill_outcomes WHERE skill_name=? AND owner_id IN (?, ?) "
                "ORDER BY id DESC LIMIT ?",
                (skill_name.strip().lower(), DEFAULT_MEMORY_OWNER, owner_id, limit),
            ).fetchall()
        return [bool(int(row["passed"])) for row in rows]

    def effectiveness(
        self,
        skill_name: str,
        *,
        owner_id: str = DEFAULT_MEMORY_OWNER,
        window: int = DEFAULT_EFFECTIVENESS_WINDOW,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        demote_below: float = DEFAULT_DEMOTE_BELOW,
    ) -> SkillEffectiveness:
        if not 1 <= window <= 1000:
            raise ValueError("window must be between 1 and 1000")
        outcomes = self.outcomes_for(skill_name, owner_id=owner_id, limit=window)
        return evaluate_skill_effectiveness(
            skill_name,
            outcomes,
            min_samples=min_samples,
            demote_below=demote_below,
        )

    def report(
        self,
        *,
        owner_id: str = DEFAULT_MEMORY_OWNER,
        window: int = DEFAULT_EFFECTIVENESS_WINDOW,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        demote_below: float = DEFAULT_DEMOTE_BELOW,
    ) -> list[SkillEffectiveness]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT skill_name FROM skill_outcomes WHERE owner_id IN (?, ?) "
                "ORDER BY skill_name",
                (DEFAULT_MEMORY_OWNER, owner_id),
            ).fetchall()
        return [
            self.effectiveness(
                str(row["skill_name"]),
                owner_id=owner_id,
                window=window,
                min_samples=min_samples,
                demote_below=demote_below,
            )
            for row in rows
        ]


class SkillOutcomeHook:
    def __init__(
        self,
        store: SkillOutcomeStore,
        *,
        min_result_chars: int = 0,
    ) -> None:
        self.store = store
        self.min_result_chars = min_result_chars

    def __call__(self, job: Any, status: str, result: str, error: str | None) -> None:
        payload = getattr(job, "payload", None)
        if not isinstance(payload, dict):
            return
        skill_names = payload.get("skills")
        if not isinstance(skill_names, (list, tuple)) or not skill_names:
            return
        if self.min_result_chars and len(result or "") < self.min_result_chars:
            return
        passed = status == "completed"
        details = f"status={status}"
        if error:
            details = f"{details}; error={str(error)[:200]}"
        for name in skill_names:
            try:
                self.store.record(
                    str(name),
                    passed,
                    owner_id=str(getattr(job, "owner_id", "") or DEFAULT_MEMORY_OWNER),
                    task_ref=str(getattr(job, "job_id", "") or ""),
                    details=details,
                )
            except Exception:
                log.debug("skill outcome recording failed for %s", name, exc_info=True)
