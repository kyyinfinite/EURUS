from __future__ import annotations

from eurus.office.errors import OfficeWorkflowError
from eurus.office.models.task import JobStage

TRANSITIONS: dict[JobStage, frozenset[JobStage]] = {
    JobStage.RECEIVED: frozenset(
        {JobStage.BRIEF_ANALYSIS, JobStage.FAILED, JobStage.CANCELLED, JobStage.REQUIRES_USER_INPUT}
    ),
    JobStage.BRIEF_ANALYSIS: frozenset(
        {JobStage.TEAM_PLANNING, JobStage.FAILED, JobStage.CANCELLED, JobStage.REQUIRES_USER_INPUT}
    ),
    JobStage.TEAM_PLANNING: frozenset(
        {JobStage.RESEARCH, JobStage.DESIGN, JobStage.FAILED, JobStage.CANCELLED}
    ),
    JobStage.RESEARCH: frozenset({JobStage.DESIGN, JobStage.FAILED, JobStage.CANCELLED}),
    JobStage.DESIGN: frozenset({JobStage.PRODUCTION, JobStage.FAILED, JobStage.CANCELLED}),
    JobStage.PRODUCTION: frozenset({JobStage.STRUCTURAL_QA, JobStage.FAILED, JobStage.CANCELLED}),
    JobStage.STRUCTURAL_QA: frozenset(
        {
            JobStage.RENDERING,
            JobStage.REVISION,
            JobStage.FINALIZATION,
            JobStage.FAILED,
            JobStage.CANCELLED,
            JobStage.QUALITY_LIMIT_REACHED,
        }
    ),
    JobStage.RENDERING: frozenset(
        {JobStage.VISUAL_QA, JobStage.REVISION, JobStage.FAILED, JobStage.CANCELLED}
    ),
    JobStage.VISUAL_QA: frozenset(
        {
            JobStage.CRITIC_REVIEW,
            JobStage.REVISION,
            JobStage.FINALIZATION,
            JobStage.FAILED,
            JobStage.CANCELLED,
            JobStage.QUALITY_LIMIT_REACHED,
        }
    ),
    JobStage.CRITIC_REVIEW: frozenset(
        {
            JobStage.CLIENT_ACCEPTANCE,
            JobStage.REVISION,
            JobStage.FAILED,
            JobStage.CANCELLED,
            JobStage.QUALITY_LIMIT_REACHED,
        }
    ),
    JobStage.CLIENT_ACCEPTANCE: frozenset(
        {
            JobStage.REVISION,
            JobStage.FINALIZATION,
            JobStage.QUALITY_LIMIT_REACHED,
            JobStage.REQUIRES_USER_INPUT,
            JobStage.FAILED,
            JobStage.CANCELLED,
        }
    ),
    JobStage.REVISION: frozenset(
        {JobStage.DESIGN, JobStage.PRODUCTION, JobStage.FAILED, JobStage.CANCELLED}
    ),
    JobStage.FINALIZATION: frozenset({JobStage.COMPLETED, JobStage.FAILED, JobStage.CANCELLED}),
    JobStage.COMPLETED: frozenset(),
    JobStage.FAILED: frozenset(),
    JobStage.CANCELLED: frozenset(),
    JobStage.REQUIRES_USER_INPUT: frozenset(),
    JobStage.QUALITY_LIMIT_REACHED: frozenset({JobStage.FINALIZATION}),
}

TERMINAL_STAGES = frozenset(
    {
        JobStage.COMPLETED,
        JobStage.FAILED,
        JobStage.CANCELLED,
        JobStage.REQUIRES_USER_INPUT,
        JobStage.QUALITY_LIMIT_REACHED,
    }
)


def validate_transition(current: JobStage, target: JobStage) -> None:
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise OfficeWorkflowError(f"invalid stage transition: {current.value} -> {target.value}")


def is_terminal(stage: JobStage) -> bool:
    return stage in TERMINAL_STAGES
