from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CONFLICT_KINDS = frozenset(
    {
        "overlapping_path",
        "patch_does_not_apply",
        "missing_target_file",
        "unknown",
    }
)

_PATH_FROM_ERROR = re.compile(r"error: patch failed: (\S+)")
_PATH_FROM_DOES_NOT_APPLY = re.compile(r"error: (\S+): (?:patch does not apply|does not exist)")


@dataclass(frozen=True, slots=True)
class ConflictReport:
    kind: str
    paths: tuple[str, ...] = ()
    summary: str = ""
    resolution_objective: str = ""
    details: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "paths": list(self.paths),
            "summary": self.summary,
            "resolution_objective": self.resolution_objective,
            "details": list(self.details),
        }


def classify_conflict(
    git_apply_stderr: str, attempted_paths: tuple[str, ...] = ()
) -> ConflictReport:
    stderr = (git_apply_stderr or "").strip()
    paths: list[str] = []
    for pattern in (_PATH_FROM_ERROR, _PATH_FROM_DOES_NOT_APPLY):
        for match in pattern.finditer(stderr):
            path = match.group(1).strip()
            if path and path not in paths:
                paths.append(path)
    if not paths:
        paths = [path for path in attempted_paths if path]
    if "does not exist" in stderr:
        kind = "missing_target_file"
    elif "patch failed" in stderr or "does not apply" in stderr:
        kind = "overlapping_path" if paths else "patch_does_not_apply"
    else:
        kind = "unknown"
    affected = tuple(paths[:50])
    summary = f"integration conflict ({kind}) touching: {', '.join(affected) or 'unknown paths'}"
    objective = (
        "Resolve patch integration conflict manually: rebase the specialist patch "
        f"onto the current integration base for paths {', '.join(affected) or '(unknown)'} "
        "and resubmit. Automatic overwrite is not permitted."
    )
    return ConflictReport(
        kind=kind,
        paths=affected,
        summary=summary,
        resolution_objective=objective,
        details=tuple(stderr.splitlines()[:20]),
    )
