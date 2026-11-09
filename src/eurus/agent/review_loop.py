from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from eurus.agent.acceptance import ReviewStore
from eurus.multiagent.contracts import ReviewFinding, TaskSpec
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database, DatabasePath

log = logging.getLogger(__name__)

REVISION_KEY_PATTERN = re.compile(r"^(?P<base>.+?)(?:-revision-(?P<n>\d+))?$")


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    review_id: str
    approved: bool
    revision_task_id: str | None
    cycle: int
    escalated: bool = False
    blockers: tuple[str, ...] = ()


class ReviewLoop:
    def __init__(
        self,
        database: Database | DatabasePath,
        *,
        max_cycles: int = 3,
        revision_role: str = "coder",
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.max_cycles = max_cycles
        self.revision_role = revision_role
        self.reviews = ReviewStore(self.database)
        self.tasks = TaskGraphStore(self.database)

    def submit_review(
        self,
        *,
        owner_id: str,
        mission_id: str,
        task_id: str,
        reviewer_assignment_id: str | None,
        status: str,
        findings: tuple[ReviewFinding, ...] | list[ReviewFinding],
        summary: str = "",
        artifact_id: str | None = None,
    ) -> ReviewOutcome:
        task = self.tasks.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task {task_id!r} not found for this owner")
        cycle = self._next_cycle(mission_id, task.task_key, owner_id=owner_id)
        review = self.reviews.submit(
            owner_id=owner_id,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id=reviewer_assignment_id,
            status=status,
            findings=findings,
            summary=summary,
            cycle=cycle,
            artifact_id=artifact_id,
        )
        blocking = [finding for finding in review.findings if finding.is_blocking]
        if review.status == "approved" and not blocking:
            self.tasks.update_status(task_id, "succeeded", owner_id=owner_id)
            self._close_parent_revision(task, owner_id=owner_id)
            return ReviewOutcome(
                review_id=review.review_id,
                approved=True,
                revision_task_id=None,
                cycle=cycle,
            )
        if not blocking:
            return ReviewOutcome(
                review_id=review.review_id,
                approved=False,
                revision_task_id=None,
                cycle=cycle,
            )
        if cycle >= self.max_cycles:
            log.warning(
                "review cycle %d exhausted for task %s; escalating %d blocker(s)",
                cycle,
                task.task_key,
                len(blocking),
            )
            self.tasks.update_status(task_id, "blocked", owner_id=owner_id)
            return ReviewOutcome(
                review_id=review.review_id,
                approved=False,
                revision_task_id=None,
                cycle=cycle,
                escalated=True,
                blockers=tuple(finding.finding_id for finding in blocking),
            )
        revision = self._create_revision_task(
            owner_id=owner_id,
            mission_id=mission_id,
            task=task,
            findings=blocking,
            cycle=cycle,
        )
        self.tasks.update_status(task_id, "revision_required", owner_id=owner_id)
        for finding in blocking:
            self.reviews.resolve_finding(
                review.review_id,
                finding.finding_id,
                owner_id=owner_id,
                disposition="open",
                revision_task_id=revision.task_id,
            )
        return ReviewOutcome(
            review_id=review.review_id,
            approved=False,
            revision_task_id=revision.task_id,
            cycle=cycle,
        )

    def _close_parent_revision(self, task: TaskSpec, *, owner_id: str) -> None:
        if not task.parent_task_id:
            return
        parent = self.tasks.get(task.parent_task_id, owner_id=owner_id)
        if parent is None or parent.status in {"succeeded", "cancelled", "skipped"}:
            return
        self.tasks.update_status(task.parent_task_id, "succeeded", owner_id=owner_id)
        for parent_review in self.reviews.list_for_task(
            task.mission_id, task.parent_task_id, owner_id=owner_id
        ):
            for finding in parent_review.findings:
                if finding.revision_task_id == task.task_id and finding.disposition == "open":
                    self.reviews.resolve_finding(
                        parent_review.review_id,
                        finding.finding_id,
                        owner_id=owner_id,
                        disposition="fixed",
                        revision_task_id=task.task_id,
                    )

    def _next_cycle(self, mission_id: str, task_key: str, *, owner_id: str) -> int:
        base_match = REVISION_KEY_PATTERN.match(task_key)
        base = base_match.group("base") if base_match else ""
        highest = 0
        for task in self.tasks.list_tasks(mission_id, owner_id=owner_id):
            match = REVISION_KEY_PATTERN.match(task.task_key)
            if match is None or match.group("base") != base:
                continue
            revision_number = int(match.group("n") or 0)
            highest = max(highest, revision_number)
        return highest + 1

    def _create_revision_task(
        self,
        *,
        owner_id: str,
        mission_id: str,
        task: TaskSpec,
        findings: list[ReviewFinding],
        cycle: int,
    ) -> TaskSpec:
        revision_key = f"{task.task_key}-revision-{cycle + 1}"
        objective_lines = [f"Revision {cycle + 1} for task {task.task_key!r}:", ""]
        objective_lines.extend(
            f"- [{finding.severity}/{finding.category}] {finding.location or 'artifact'}: "
            f"{finding.evidence or finding.suggested_remediation or finding.finding_id}"
            for finding in findings
        )
        revision = TaskSpec(
            task_id=uuid.uuid4().hex,
            mission_id=mission_id,
            task_key=revision_key,
            title=f"Revision {cycle + 1}: {task.title or task.task_key}"[:500],
            objective="\n".join(objective_lines)[:20000],
            parent_task_id=task.task_id,
            depends_on=(task.task_id,),
            required_capabilities=task.required_capabilities,
            preferred_role=task.preferred_role or self.revision_role,
            acceptance_criteria=task.acceptance_criteria,
            priority=max(1, task.priority - 1),
        )
        self.tasks.add_task(revision, owner_id=owner_id)
        return revision
