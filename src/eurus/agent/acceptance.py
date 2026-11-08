from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eurus.multiagent.contracts import (
    AcceptanceDecision,
    ReviewFinding,
    ReviewResult,
    VerificationEvidence,
)
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database, DatabasePath

VERIFICATION_CATEGORIES = frozenset({"test", "lint", "typecheck", "build", "security", "custom"})
REVIEW_STATUSES = frozenset({"open", "approved", "changes_requested", "rejected"})


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    require_verification: bool = True
    required_check_names: tuple[str, ...] = ()
    blocking_severities: tuple[str, ...] = ("blocker", "critical", "major")
    max_review_cycles: int = 3

    def __post_init__(self) -> None:
        if self.max_review_cycles < 1:
            raise ValueError("max_review_cycles must be >= 1")


class VerificationStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def record(self, evidence: VerificationEvidence, *, owner_id: str) -> str:
        if not evidence.mission_id.strip() or not evidence.task_id.strip():
            raise ValueError("verification evidence must reference a mission and task")
        verification_id = uuid.uuid4().hex
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_verifications(
                    verification_id, owner_id, mission_id, task_id, artifact_id,
                    check_name, category, command_json, working_directory, exit_code,
                    duration_ms, status, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verification_id,
                    owner_id,
                    evidence.mission_id,
                    evidence.task_id,
                    evidence.artifact_id,
                    evidence.check_name,
                    evidence.category,
                    json.dumps(list(evidence.command_argv)),
                    evidence.working_directory,
                    evidence.exit_code,
                    evidence.duration_ms,
                    evidence.status,
                    json.dumps(evidence.to_dict()),
                    _time(_now()),
                ),
            )
        return verification_id

    def list_for_mission(self, mission_id: str, *, owner_id: str) -> list[VerificationEvidence]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT evidence_json FROM eurus_agent_verifications "
                "WHERE owner_id=? AND mission_id=? ORDER BY created_at",
                (owner_id, mission_id),
            ).fetchall()
        evidence: list[VerificationEvidence] = []
        for row in rows:
            data = _json_dict(row["evidence_json"])
            if not data:
                continue
            evidence.append(
                VerificationEvidence(
                    check_name=str(data.get("check_name", "")),
                    status=str(data.get("status", "")),
                    category=str(data.get("category", "test")),
                    command_argv=tuple(data.get("command_argv", ())),
                    working_directory=data.get("working_directory"),
                    exit_code=data.get("exit_code"),
                    duration_ms=data.get("duration_ms"),
                    stdout_tail=str(data.get("stdout_tail", "")),
                    stderr_tail=str(data.get("stderr_tail", "")),
                    environment=dict(data.get("environment", {})),
                    mission_id=str(data.get("mission_id", "")),
                    task_id=str(data.get("task_id", "")),
                    artifact_id=data.get("artifact_id"),
                )
            )
        return evidence


class ReviewStore:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def submit(
        self,
        *,
        owner_id: str,
        mission_id: str,
        task_id: str,
        reviewer_assignment_id: str | None,
        status: str,
        findings: Sequence[ReviewFinding],
        summary: str = "",
        cycle: int = 1,
        artifact_id: str | None = None,
    ) -> ReviewResult:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"unknown review status: {status!r}")
        if not 1 <= cycle <= 100:
            raise ValueError("cycle must be between 1 and 100")
        review_id = uuid.uuid4().hex
        result = ReviewResult(
            review_id=review_id,
            mission_id=mission_id,
            task_id=task_id,
            reviewer_assignment_id=reviewer_assignment_id,
            status=status,
            findings=tuple(findings),
            summary=summary[:8000],
            cycle=cycle,
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_reviews(
                    review_id, owner_id, mission_id, task_id, artifact_id,
                    reviewer_assignment_id, cycle, status, findings_json, summary,
                    created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    owner_id,
                    mission_id,
                    task_id,
                    artifact_id,
                    reviewer_assignment_id,
                    cycle,
                    status,
                    json.dumps([finding.to_dict() for finding in result.findings]),
                    result.summary,
                    _time(result.created_at),
                    _time(_now()) if status != "open" else None,
                ),
            )
        return result

    def list_for_task(self, mission_id: str, task_id: str, *, owner_id: str) -> list[ReviewResult]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_reviews "
                "WHERE owner_id=? AND mission_id=? AND task_id=? ORDER BY created_at",
                (owner_id, mission_id, task_id),
            ).fetchall()
        results: list[ReviewResult] = []
        for row in rows:
            raw_findings = _json_list(row["findings_json"])
            findings = tuple(
                ReviewFinding(
                    finding_id=str(item.get("finding_id", "")),
                    severity=str(item.get("severity", "info")),
                    category=str(item.get("category", "correctness")),
                    location=str(item.get("location", "")),
                    evidence=str(item.get("evidence", "")),
                    suggested_remediation=str(item.get("suggested_remediation", "")),
                    disposition=str(item.get("disposition", "open")),
                    revision_task_id=item.get("revision_task_id"),
                )
                for item in raw_findings
                if isinstance(item, dict)
            )
            results.append(
                ReviewResult(
                    review_id=row["review_id"],
                    mission_id=row["mission_id"],
                    task_id=row["task_id"],
                    reviewer_assignment_id=row["reviewer_assignment_id"],
                    status=row["status"],
                    findings=findings,
                    summary=row["summary"] or "",
                    cycle=int(row["cycle"]),
                    created_at=_parse(row["created_at"]) or _now(),
                )
            )
        return results

    def resolve_finding(
        self,
        review_id: str,
        finding_id: str,
        *,
        owner_id: str,
        disposition: str,
        revision_task_id: str | None = None,
    ) -> bool:
        from eurus.multiagent.contracts import FINDING_DISPOSITIONS

        if disposition not in FINDING_DISPOSITIONS:
            raise ValueError(f"unknown finding disposition: {disposition!r}")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT findings_json FROM eurus_agent_reviews WHERE review_id=? AND owner_id=?",
                (review_id, owner_id),
            ).fetchone()
            if row is None:
                return False
            findings = _json_list(row["findings_json"])
            updated = False
            for item in findings:
                if isinstance(item, dict) and item.get("finding_id") == finding_id:
                    item["disposition"] = disposition
                    if revision_task_id is not None:
                        item["revision_task_id"] = revision_task_id
                    updated = True
            if not updated:
                return False
            connection.execute(
                "UPDATE eurus_agent_reviews SET findings_json=?, resolved_at=? "
                "WHERE review_id=? AND owner_id=?",
                (json.dumps(findings), _time(_now()), review_id, owner_id),
            )
        return True


class AcceptanceService:
    def __init__(
        self,
        database: Database | DatabasePath,
        *,
        policy: AcceptancePolicy | None = None,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.policy = policy or AcceptancePolicy()
        self.verifications = VerificationStore(self.database)
        self.reviews = ReviewStore(self.database)
        self.tasks = TaskGraphStore(self.database)

    def record_verification(
        self,
        *,
        mission_id: str,
        task_id: str,
        owner_id: str,
        check_name: str,
        status: str,
        category: str = "test",
        command_argv: Sequence[str] = (),
        exit_code: int | None = None,
        duration_ms: int | None = None,
        working_directory: str | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        artifact_id: str | None = None,
        environment: dict[str, Any] | None = None,
    ) -> str:
        evidence = VerificationEvidence(
            check_name=check_name,
            status=status,
            category=category,
            command_argv=tuple(command_argv),
            working_directory=working_directory,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail=stdout_tail[-2000:],
            stderr_tail=stderr_tail[-2000:],
            environment=dict(environment or {}),
            mission_id=mission_id,
            task_id=task_id,
            artifact_id=artifact_id,
        )
        return self.verifications.record(evidence, owner_id=owner_id)

    def evaluate(
        self,
        mission_id: str,
        *,
        owner_id: str,
        required_checks: Sequence[str] = (),
    ) -> AcceptanceDecision:
        tasks = self.tasks.list_tasks(mission_id, owner_id=owner_id)
        verifications = self.verifications.list_for_mission(mission_id, owner_id=owner_id)
        reasons: list[str] = []
        blocking_findings: list[str] = []
        failed_checks: list[str] = []
        unavailable_checks: list[str] = []

        if not tasks:
            reasons.append("mission has no planned tasks")
        non_terminal = [task.task_key for task in tasks if task.status not in _TERMINAL_OK]
        if non_terminal:
            reasons.append(f"tasks not successfully terminal: {sorted(non_terminal)}")

        for task in tasks:
            reviews = self.reviews.list_for_task(mission_id, task.task_id, owner_id=owner_id)
            for review in reviews:
                for finding in review.findings:
                    if (
                        finding.severity in self.policy.blocking_severities
                        and finding.disposition == "open"
                    ):
                        blocking_findings.append(finding.finding_id)
        if blocking_findings:
            reasons.append(f"open blocking review findings: {sorted(set(blocking_findings))}")

        for verification in verifications:
            if verification.failed:
                failed_checks.append(verification.check_name)
            elif verification.unavailable:
                unavailable_checks.append(verification.check_name)
        if failed_checks:
            reasons.append(f"failing checks: {sorted(set(failed_checks))}")

        required = tuple(required_checks) or self.policy.required_check_names
        recorded = {verification.check_name for verification in verifications}
        missing = [name for name in required if name not in recorded]
        if missing:
            unavailable_checks.extend(missing)
            reasons.append(f"required checks never ran: {sorted(set(missing))}")

        if self.policy.require_verification and not verifications and tasks:
            reasons.append("no verification evidence recorded (claims are not evidence)")

        if blocking_findings or failed_checks or non_terminal or not tasks:
            decision = "blocked"
        elif unavailable_checks or (self.policy.require_verification and not verifications):
            decision = "completed_unverified"
        else:
            decision = "completed"
        return AcceptanceDecision(
            mission_id=mission_id,
            decision=decision,
            reasons=tuple(dict.fromkeys(reasons)),
            blocking_finding_ids=tuple(sorted(set(blocking_findings))),
            failed_check_names=tuple(sorted(set(failed_checks))),
            unavailable_check_names=tuple(sorted(set(unavailable_checks))),
        )


_TERMINAL_OK = frozenset({"succeeded", "skipped"})


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
