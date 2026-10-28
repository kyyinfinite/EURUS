from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from eurus.cli import main
from eurus.cli.evals import _compare_learning_report, _record_suite
from eurus.evals import EvalResult
from eurus.evals.results import EvaluationStore
from eurus.learning.skills import SkillDraftStore
from eurus.learning.validation import (
    create_candidate,
    promote_candidate,
    validate_candidate,
)

CLEAN_CONTENT = (
    "Read the target module before editing it. Run the project checks after "
    "every change. Keep diffs minimal and focused on the requested behavior. "
)

SECRET_CONTENT = (
    "Use the API key sk-abcdefghijklmnop1234567890 whenever the service asks "
    "for authentication during this workflow."
)


def run(tmp_path: Path, *args: str) -> object:
    return CliRunner().invoke(main, list(args), env={"EURUS_HOME": str(tmp_path)})


def _draft(tmp_path: Path, name: str = "careful-editing", content: str = CLEAN_CONTENT) -> None:
    store = SkillDraftStore(root=tmp_path / "skills" / "quarantine")
    store.draft(name, "Edit files carefully", content)


def test_skills_list_and_show(tmp_path: Path):
    _draft(tmp_path)
    result = run(tmp_path, "skills", "list")
    assert result.exit_code == 0
    assert "careful-editing" in result.output
    result = run(tmp_path, "skills", "show", "careful-editing")
    assert result.exit_code == 0
    assert "target module" in result.output


def test_skills_promote_requires_explicit_approval(tmp_path: Path):
    _draft(tmp_path)
    result = run(
        tmp_path,
        "skills",
        "promote",
        "careful-editing",
        "--workspace",
        str(tmp_path),
        "--outcome",
        "checks pass",
    )
    assert result.exit_code != 0
    assert "approval" in result.output.lower()


def test_skills_promote_with_approval_installs_validated_skill(tmp_path: Path):
    _draft(tmp_path)
    result = run(
        tmp_path,
        "skills",
        "promote",
        "careful-editing",
        "--yes",
        "--workspace",
        str(tmp_path),
        "--outcome",
        "edits stay minimal and checks pass",
    )
    assert result.exit_code == 0
    installed = tmp_path / ".eurus" / "skills" / "careful-editing" / "SKILL.md"
    assert installed.exists()
    assert "target module" in installed.read_text(encoding="utf-8")


def test_skills_promote_rejects_dangerous_draft(tmp_path: Path):
    _draft(tmp_path, name="leaky-skill", content=SECRET_CONTENT)
    result = run(
        tmp_path,
        "skills",
        "promote",
        "leaky-skill",
        "--yes",
        "--workspace",
        str(tmp_path),
        "--outcome",
        "secrets leak",
    )
    assert result.exit_code != 0


def test_skills_validate_requires_real_checks(tmp_path: Path):
    _draft(tmp_path)
    result = run(tmp_path, "skills", "validate", "careful-editing")
    assert result.exit_code != 0
    result = run(
        tmp_path,
        "skills",
        "validate",
        "careful-editing",
        "--check",
        "reviewed the workflow on two tasks",
        "--outcome",
        "smaller diffs and passing checks",
    )
    assert result.exit_code == 0
    assert "passed validation" in result.output


def test_skills_patch_is_versioned_and_scanned(tmp_path: Path):
    _draft(tmp_path)
    store = SkillDraftStore(root=tmp_path / "skills" / "quarantine")
    workspace = tmp_path
    candidate = create_candidate(
        name="careful-editing",
        description="Edit files carefully",
        procedure=CLEAN_CONTENT,
        source_task_id="quarantine:test",
        evidence=("quarantine draft",),
        expected_outcomes=("smaller diffs and passing checks",),
    )
    validated = validate_candidate(candidate, checks=("manual review",))
    promote_candidate(validated, store, workspace, approved=True)
    replacement = tmp_path / "replacement.md"
    replacement.write_text(
        "Prefer the smallest change that satisfies the request. "
        "Re-run the checks before reporting completion.",
        encoding="utf-8",
    )
    skill_root = workspace / ".eurus" / "skills" / "careful-editing"
    (skill_root / "SKILL.md").write_text(
        "---\nname: careful-editing\ndescription: Edit files carefully\n---\n\n"
        "older content that should be backed up\n",
        encoding="utf-8",
    )
    result = run(
        tmp_path,
        "skills",
        "patch",
        "careful-editing",
        "--content-file",
        str(replacement),
        "--workspace",
        str(workspace),
    )
    assert result.exit_code == 0
    backups = sorted(skill_root.glob("careful-editing.SKILL.md.v*"))
    assert backups, "patching must keep a versioned backup"
    assert "older content" in backups[-1].read_text(encoding="utf-8")
    assert "smallest change" in (skill_root / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="safety scan"):
        dangerous = tmp_path / "dangerous.md"
        dangerous.write_text(SECRET_CONTENT, encoding="utf-8")
        store.patch("careful-editing", workspace, "Edit files carefully", SECRET_CONTENT)


def test_promote_candidate_rejects_unvalidated_or_unapproved():
    candidate = create_candidate(
        name="careful-editing",
        description="Edit files carefully",
        procedure=CLEAN_CONTENT,
        source_task_id="task-1",
        evidence=("evidence line",),
        expected_outcomes=("outcome",),
    )
    with pytest.raises(PermissionError, match="approval"):
        promote_candidate(candidate, None, Path("."), approved=False)
    unvalidated = validate_candidate(candidate, checks=())
    assert unvalidated.validated is False
    assert unvalidated.validation_notes


def _eval(task: str, passed: bool) -> EvalResult:
    return EvalResult(task, passed, 3, 100, 1.0, "completed")


def test_learning_comparison_detects_regressions_and_improvements():
    baseline = [_eval("task-a", True), _eval("task-b", False)]
    treated = [_eval("task-a", False), _eval("task-b", True)]
    report = _compare_learning_report(baseline, treated)
    assert report["regressions"] == ["task-a"]
    assert report["improvements"] == ["task-b"]
    assert report["learning_helps"] is False


def test_learning_comparison_flags_clean_improvement():
    baseline = [_eval("task-a", False)]
    treated = [_eval("task-a", True)]
    report = _compare_learning_report(baseline, treated)
    assert report["regressions"] == []
    assert report["learning_helps"] is True


def test_learning_comparison_records_persisted_for_regression_audit(tmp_path: Path):
    store = EvaluationStore(tmp_path / "learning.sqlite3")
    baseline = [_eval("task-a", True)]
    treated = [_eval("task-a", False)]
    _record_suite(store, "rev-off", baseline, lessons=False)
    _record_suite(store, "rev-on", treated, lessons=True)
    comparison = store.compare("learning-compare", "rev-off", "rev-on")
    assert comparison["baseline"]["pass_rate"] == 1.0
    assert comparison["candidate"]["pass_rate"] == 0.0
    rows = store.query(suite="learning-compare")
    assert len(rows) == 2
    assert {row["passed"] for row in rows} == {True, False}
