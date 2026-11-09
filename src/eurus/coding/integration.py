from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from eurus.coding.conflict_resolution import ConflictReport, classify_conflict
from eurus.coding.paths import git_child_env
from eurus.coding.worktree import (
    PatchMetadata,
    WorktreeError,
    create_worktree,
    review_patch,
)

log = logging.getLogger(__name__)

POST_CHECK = Callable[[Path], list["CheckResult"]]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class IntegrationReport:
    applied: bool
    integration_base: str
    changed_paths: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    conflict_report: ConflictReport | None = None
    post_checks: tuple[CheckResult, ...] = field(default_factory=tuple)
    rollback_ref: str = ""
    review_findings: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def requires_resolution(self) -> bool:
        return bool(self.conflicts) or bool(self.review_findings)


def integrate_patch(
    root: Path,
    patch: PatchMetadata,
    *,
    post_checks: POST_CHECK | None = None,
) -> IntegrationReport:
    resolved_root = root.expanduser().resolve()
    review = review_patch(patch.diff)
    if not review.approved:
        return IntegrationReport(
            applied=False,
            integration_base="",
            conflicts=(),
            review_findings=review.findings,
            detail="patch rejected by pre-integration review",
        )
    if not patch.diff.strip():
        return IntegrationReport(
            applied=False,
            integration_base="",
            detail="empty patch; nothing to integrate",
        )
    worktree = create_worktree(resolved_root, prefix="eurus-integration")
    try:
        integration_base = worktree.base_commit
        base_mismatch = bool(patch.base_commit and patch.base_commit != integration_base)
        apply_check = _git_apply_check(worktree.path, patch.diff)
        if apply_check is not None:
            report = classify_conflict(apply_check, patch.changed_paths)
            if base_mismatch:
                report = ConflictReport(
                    kind=report.kind,
                    paths=report.paths,
                    summary=(
                        f"patch base {patch.base_commit[:12]} differs from integration HEAD "
                        f"{integration_base[:12]}; {report.summary}"
                    ),
                    resolution_objective=report.resolution_objective,
                    details=report.details,
                )
            log.warning(
                "patch %s conflicts with integration workspace: %s",
                patch.diff_hash[:12],
                report.summary,
            )
            return IntegrationReport(
                applied=False,
                integration_base=integration_base,
                conflicts=tuple(report.paths),
                conflict_report=report,
                detail=report.summary,
            )
        _git_apply(worktree.path, patch.diff)
        worktree.stage_paths(patch.changed_paths)
        staged = tuple(worktree.changed_paths())
        results: list[CheckResult] = []
        if post_checks is not None:
            results = list(post_checks(worktree.path))
        detail = (
            "patch applied in integration worktree"
            if not base_mismatch
            else (
                f"patch applied in integration worktree; note: patch base "
                f"{patch.base_commit[:12]} differed from integration HEAD "
                f"{integration_base[:12]} but applied cleanly"
            )
        )
        return IntegrationReport(
            applied=True,
            integration_base=integration_base,
            changed_paths=staged,
            conflicts=(),
            post_checks=tuple(results),
            rollback_ref=integration_base,
            detail=detail,
        )
    finally:
        worktree.cleanup()


def _git_apply_check(worktree_path: Path, diff: str) -> str | None:
    process = subprocess.run(
        ["git", "-C", str(worktree_path), "apply", "--check", "--whitespace=nowarn", "-"],
        input=diff,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=git_child_env(),
    )
    if process.returncode == 0:
        return None
    return process.stderr.strip() or process.stdout.strip() or "git apply --check failed"


def _git_apply(worktree_path: Path, diff: str) -> None:
    process = subprocess.run(
        ["git", "-C", str(worktree_path), "apply", "--whitespace=nowarn", "-"],
        input=diff,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=git_child_env(),
    )
    if process.returncode != 0:
        raise WorktreeError(
            f"patch apply failed after a successful check: "
            f"{(process.stderr or process.stdout).strip()[:300]}"
        )
