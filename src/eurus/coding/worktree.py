from __future__ import annotations

import re
import secrets as token_gen
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from eurus.errors import ToolError
from eurus.security.redaction import SECRET_PATTERNS

WORKTREE_TIMEOUT = 30.0
MAX_DIFF_CHARS = 200_000
MAX_FILES_IN_PATCH = 200


class WorktreeError(ToolError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=WORKTREE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git is unavailable in this environment: {exc}") from exc
    if check and result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout


@dataclass(slots=True)
class IsolatedWorktree:
    root: Path
    path: Path
    identifier: str
    cleanup_status: str = "pending"

    def add_all(self) -> None:
        _git(self.path, "add", "-A")

    def diff_against_head(self) -> str:
        output = _git(self.path, "diff", "--cached", "--no-color", "HEAD", check=False)
        return output[:MAX_DIFF_CHARS]

    def cleanup(self) -> str:
        if self.cleanup_status != "pending":
            return self.cleanup_status
        try:
            _git(self.root, "worktree", "remove", "--force", str(self.path), check=False)
            _git(self.root, "worktree", "prune", check=False)
            shutil.rmtree(self.path.parent, ignore_errors=True)
            self.cleanup_status = "cleanup_failed" if self.path.parent.exists() else "cleaned"
        except (WorktreeError, OSError):
            self.cleanup_status = "cleanup_failed"
        return self.cleanup_status


def create_worktree(root: Path, *, prefix: str = "eurus-coder") -> IsolatedWorktree:
    import tempfile

    resolved = root.expanduser().resolve()
    if not (resolved / ".git").exists():
        raise WorktreeError(
            "workspace is not a git repository; isolated code delegation requires a "
            "git worktree and is disabled otherwise"
        )
    _git(resolved, "rev-parse", "--show-toplevel")
    identifier = f"{prefix}-{token_gen.token_hex(6)}"
    base = Path(tempfile.mkdtemp(prefix=f"{identifier}-"))
    worktree_path = base / "wt"
    _git(resolved, "worktree", "add", "--detach", str(worktree_path), "HEAD")
    return IsolatedWorktree(root=resolved, path=worktree_path, identifier=identifier)


@dataclass(frozen=True, slots=True)
class PatchReview:
    approved: bool
    findings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {"approved": self.approved, "findings": list(self.findings)}


def _paths_from_diff(diff: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"^(?:\+\+\+|---) [ab]/(.+)$", diff, re.MULTILINE):
        raw = match.group(1).strip()
        if raw and raw != "/dev/null" and raw not in paths:
            paths.append(raw)
    return paths


def review_patch(diff: str) -> PatchReview:
    findings: list[str] = []
    if not diff.strip():
        return PatchReview(True, ("empty_patch",))
    if len(diff) > MAX_DIFF_CHARS:
        findings.append("patch_truncated_for_review")
    paths = _paths_from_diff(diff)
    if len(paths) > MAX_FILES_IN_PATCH:
        findings.append(f"patch touches more than {MAX_FILES_IN_PATCH} files")
    for path in paths:
        if path.startswith("/") or path.startswith("\\"):
            findings.append(f"absolute path in patch: {path[:120]}")
        if ".." in Path(path).parts:
            findings.append(f"path traversal in patch: {path[:120]}")
        if path.startswith(".git/") or path == ".git":
            findings.append(f"patch modifies git internals: {path[:120]}")
    if re.search(r"^new file mode 120000", diff, re.MULTILINE):
        findings.append("patch creates symbolic links")
    if "GIT binary patch" in diff or re.search(r"^Binary files", diff, re.MULTILINE):
        findings.append("patch contains binary content")
    if re.search(r"\.pyc\b|\.so\b|\.exe\b|\.dll\b", " ".join(paths)):
        findings.append("patch contains compiled or executable artifacts")
    joined = diff
    for pattern in SECRET_PATTERNS:
        if pattern.search(joined):
            findings.append(f"possible secret exposure matching {pattern.pattern}")
            break
    return PatchReview(approved=not findings, findings=tuple(findings[:20]))
