from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from eurus.agent.acceptance import VerificationStore
from eurus.agent.review_loop import ReviewLoop
from eurus.multiagent.contracts import ReviewFinding, TaskSpec
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database, DatabasePath

log = logging.getLogger(__name__)

NON_TERMINAL_TASK_STATUSES = frozenset(
    {"pending", "ready", "dispatched", "running", "review_pending"}
)
STALE_TASK_STATUSES = frozenset({"dispatched", "running", "review_pending"})
RECONCILE_STATUSES = frozenset({"dispatched", "running"})


@dataclass(frozen=True, slots=True)
class SupervisionReport:
    mission_id: str
    checked: int = 0
    stale_assignments: tuple[str, ...] = ()
    resynced_tasks: tuple[str, ...] = ()
    cancelled_assignments: tuple[str, ...] = ()
    blocked_dependents: int = 0
    deadline_expired: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "checked": self.checked,
            "stale_assignments": list(self.stale_assignments),
            "resynced_tasks": list(self.resynced_tasks),
            "cancelled_assignments": list(self.cancelled_assignments),
            "blocked_dependents": self.blocked_dependents,
            "deadline_expired": self.deadline_expired,
            "notes": list(self.notes),
        }


class MissionSupervision:
    def __init__(
        self,
        database: Database | DatabasePath,
        orchestrator: AgentOrchestrator,
        *,
        stale_after_seconds: float = 900.0,
        review: ReviewLoop | None = None,
        verifications: VerificationStore | None = None,
    ) -> None:
        if not 5 <= stale_after_seconds <= 86_400:
            raise ValueError("stale_after_seconds must be between 5 and 86400")
        self.database = database if isinstance(database, Database) else Database(database)
        self.orchestrator = orchestrator
        self.tasks = TaskGraphStore(self.database)
        self.review = review if review is not None else ReviewLoop(self.database)
        self.verifications = verifications or VerificationStore(self.database)
        self.stale_after_seconds = stale_after_seconds

    def tick(
        self, mission_id: str, *, owner_id: str, deadline: datetime | None = None
    ) -> SupervisionReport:
        tasks = self.tasks.list_tasks(mission_id, owner_id=owner_id)
        if deadline is not None and datetime.now(UTC) >= deadline:
            return self._expire(
                mission_id,
                owner_id=owner_id,
                checked=len(tasks),
                notes=("deadline passed; cancelling remaining descendants",),
            )

        stale, resynced, notes = self._reconcile(mission_id, tasks, owner_id=owner_id)
        review_notes = self._review_pending(mission_id, owner_id=owner_id)
        return SupervisionReport(
            mission_id=mission_id,
            checked=len(tasks),
            stale_assignments=stale,
            resynced_tasks=resynced,
            notes=tuple([*notes, *review_notes]),
        )

    def _review_pending(self, mission_id: str, *, owner_id: str) -> tuple[str, ...]:
        notes: list[str] = []
        evidence_rows = self.verifications.list_for_mission(mission_id, owner_id=owner_id)
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status != "review_pending":
                continue
            findings: list[ReviewFinding] = []
            task_evidence = [row for row in evidence_rows if row.task_id == task.task_id]
            for row in task_evidence:
                if row.failed:
                    findings.append(
                        ReviewFinding(
                            finding_id=f"{task.task_id}:{row.check_name}:failed",
                            severity="blocker",
                            category="correctness",
                            location=row.check_name,
                            evidence=(row.stderr_tail or row.stdout_tail or "")[:400],
                            suggested_remediation="fix the failing check and resubmit",
                        )
                    )
            if not task_evidence and task.acceptance_criteria:
                findings.append(
                    ReviewFinding(
                        finding_id=f"{task.task_id}:no-evidence",
                        severity="blocker",
                        category="correctness",
                        location=task.task_key,
                        evidence=(
                            "acceptance criteria are defined but no verification "
                            "evidence was recorded"
                        ),
                        suggested_remediation="run the acceptance checks and record evidence",
                    )
                )
            if not findings and not task_evidence:
                findings.append(
                    ReviewFinding(
                        finding_id=f"{task.task_id}:unverified",
                        severity="minor",
                        category="correctness",
                        location=task.task_key,
                        evidence=(
                            "approved without verification evidence; no acceptance "
                            "criteria were defined for this task"
                        ),
                    )
                )
            has_blocker = any(finding.severity == "blocker" for finding in findings)
            outcome = self.review.submit_review(
                owner_id=owner_id,
                mission_id=mission_id,
                task_id=task.task_id,
                reviewer_assignment_id=None,
                status="changes_requested" if has_blocker else "approved",
                findings=findings,
                summary="deterministic review of recorded verification evidence",
            )
            if outcome.approved:
                notes.append(f"task {task.task_key!r} review approved")
            elif outcome.escalated:
                notes.append(
                    f"task {task.task_key!r} review escalated after {outcome.cycle} cycles"
                )
            elif outcome.revision_task_id is not None:
                notes.append(
                    f"task {task.task_key!r} review requested revision "
                    f"{outcome.revision_task_id[:8]}"
                )
        return tuple(notes)

    def _reconcile(
        self, mission_id: str, tasks: list[TaskSpec], *, owner_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        stale_ids: list[str] = []
        resynced: list[str] = []
        notes: list[str] = []
        for task in tasks:
            if task.status not in RECONCILE_STATUSES:
                continue
            if not task.assignment_id:
                self.tasks.update_status(
                    task.task_id,
                    "ready",
                    owner_id=owner_id,
                    clear_assignment=True,
                )
                resynced.append(task.task_id)
                notes.append(f"task {task.task_key!r} had no assignment; reset to ready")
                continue
            assignment = self.orchestrator.store.get(task.assignment_id)
            if assignment is None:
                self.tasks.update_status(
                    task.task_id,
                    "ready",
                    owner_id=owner_id,
                    clear_assignment=True,
                )
                resynced.append(task.task_id)
                notes.append(f"task {task.task_key!r} assignment vanished; reset to ready")
                continue
            if assignment.is_terminal:
                if assignment.status == "succeeded":
                    self.tasks.update_status(task.task_id, "review_pending", owner_id=owner_id)
                    notes.append(
                        f"task {task.task_key!r} assignment succeeded; moved to review_pending"
                    )
                else:
                    self.tasks.update_status(
                        task.task_id,
                        "ready",
                        owner_id=owner_id,
                        clear_assignment=True,
                    )
                    notes.append(
                        f"task {task.task_key!r} assignment {assignment.status}; reset to ready"
                    )
                resynced.append(task.task_id)
                continue
            beat = self.orchestrator.store.last_heartbeat(assignment.assignment_id)
            reference = beat or assignment.updated_at
            age = (datetime.now(UTC) - reference).total_seconds()
            if age > self.stale_after_seconds:
                stale_ids.append(assignment.assignment_id)
                cancelled = self.orchestrator.cancel(assignment.assignment_id, owner_id=owner_id)
                if cancelled is not None:
                    self.tasks.update_status(
                        task.task_id,
                        "ready",
                        owner_id=owner_id,
                        clear_assignment=True,
                    )
                    resynced.append(task.task_id)
                    notes.append(
                        f"task {task.task_key!r} was stale ({age:.0f}s silent); "
                        "assignment cancelled and task re-queued"
                    )
        return tuple(stale_ids), tuple(resynced), tuple(notes)

    def _expire(
        self,
        mission_id: str,
        *,
        owner_id: str,
        checked: int,
        notes: tuple[str, ...],
    ) -> SupervisionReport:
        cancelled: list[str] = []
        blocked = 0
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status not in NON_TERMINAL_TASK_STATUSES:
                continue
            if task.assignment_id and task.status != "pending":
                assignment = self.orchestrator.store.get(task.assignment_id)
                if assignment is not None and not assignment.is_terminal:
                    self.orchestrator.cancel(task.assignment_id, owner_id=owner_id)
                    cancelled.append(task.assignment_id)
            try:
                self.tasks.update_status(task.task_id, "cancelled", owner_id=owner_id)
            except KeyError:
                continue
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            if task.status == "succeeded":
                continue
            if task.status in {"failed", "cancelled", "skipped"}:
                blocked += len(self.tasks.dependents_of(task.task_id))
        return SupervisionReport(
            mission_id=mission_id,
            checked=checked,
            cancelled_assignments=tuple(cancelled),
            blocked_dependents=blocked,
            deadline_expired=True,
            notes=notes,
        )
