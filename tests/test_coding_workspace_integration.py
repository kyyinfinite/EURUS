from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from eurus.coding.conflict_resolution import classify_conflict
from eurus.coding.integration import integrate_patch
from eurus.coding.worktree import WorktreeError, create_worktree, review_patch


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@eurus.local"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Eurus Test"], cwd=path, check=True)
    (path / "app.py").write_text("value = 1\n")
    (path / "README.md").write_text("demo\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _worktree_diff(worktree_path: Path, edits: dict[str, str], *, new_files: bool = False) -> str:
    for name, content in edits.items():
        target = worktree_path / name
        if new_files:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git("add", "-A", cwd=worktree_path)
    return _git("diff", "--cached", "--no-color", "HEAD", cwd=worktree_path)


class TestWorktreeMetadata:
    def test_records_base_commit_and_clean_parent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path / "repo")
        worktree = create_worktree(tmp_path / "repo")
        try:
            head = _git("rev-parse", "HEAD", cwd=tmp_path / "repo").strip()
            assert worktree.base_commit == head
            assert worktree.parent_dirty is False
            assert worktree.parent_dirty_paths == ()
        finally:
            worktree.cleanup()

    def test_detects_dirty_parent_workspace(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "notes.txt").write_text("uncommitted\n")
        worktree = create_worktree(repo)
        try:
            assert worktree.parent_dirty is True
            assert "notes.txt" in worktree.parent_dirty_paths
            assert not (worktree.path / "notes.txt").exists()
        finally:
            worktree.cleanup()

    def test_requires_git_repository(self, tmp_path: Path) -> None:
        with pytest.raises(WorktreeError, match="not a git repository"):
            create_worktree(tmp_path)

    def test_stage_paths_rejects_unsafe_paths(self, tmp_path: Path) -> None:
        _init_repo(tmp_path / "repo")
        worktree = create_worktree(tmp_path / "repo")
        try:
            with pytest.raises(WorktreeError, match="no paths"):
                worktree.stage_paths([])
            with pytest.raises(WorktreeError, match="unsafe path"):
                worktree.stage_paths(["../../etc/passwd"])
            with pytest.raises(WorktreeError, match="unsafe path"):
                worktree.stage_paths(["/etc/passwd"])
        finally:
            worktree.cleanup()

    def test_stage_paths_scopes_staging(self, tmp_path: Path) -> None:
        _init_repo(tmp_path / "repo")
        worktree = create_worktree(tmp_path / "repo")
        try:
            (worktree.path / "app.py").write_text("value = 2\n")
            (worktree.path / "README.md").write_text("changed\n")
            worktree.stage_paths(["app.py"])
            staged = _git("diff", "--cached", "--name-only", cwd=worktree.path).split()
            assert staged == ["app.py"], "only the permitted path is staged"
        finally:
            worktree.cleanup()

    def test_patch_metadata_captures_provenance(self, tmp_path: Path) -> None:
        _init_repo(tmp_path / "repo")
        worktree = create_worktree(tmp_path / "repo")
        try:
            _worktree_diff(worktree.path, {"app.py": "value = 42\n"})
            metadata = worktree.patch_metadata(assignment_id="asg-1", task_id="task-1")
            assert metadata.base_commit == worktree.base_commit
            assert metadata.changed_paths == ("app.py",)
            assert metadata.diff_hash and len(metadata.diff_hash) == 64
            assert metadata.assignment_id == "asg-1"
            assert metadata.review_status == "pending"
        finally:
            worktree.cleanup()


class TestPatchIntegration:
    def test_applies_clean_patch_in_integration_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        producer = create_worktree(repo, prefix="producer")
        try:
            patch_diff = _worktree_diff(producer.path, {"app.py": "value = 42\n"})
            assert "app.py" in patch_diff
            patch = producer.patch_metadata(assignment_id="asg-1")
        finally:
            producer.cleanup()

        report = integrate_patch(repo, patch)
        assert report.applied is True
        assert report.conflicts == ()
        assert report.rollback_ref == patch.base_commit
        assert report.changed_paths == ("app.py",)
        assert (repo / "app.py").read_text() == "value = 1\n"

    def test_detects_conflicting_patch_without_overwrite(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        base_head = _git("rev-parse", "HEAD", cwd=repo).strip()

        producer = create_worktree(repo, prefix="producer")
        try:
            patch_diff = _worktree_diff(producer.path, {"app.py": "value = 100\n"})
            assert "app.py" in patch_diff
            patch = producer.patch_metadata()
        finally:
            producer.cleanup()

        (repo / "app.py").write_text("value = 200\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "conflicting change", cwd=repo)
        assert _git("rev-parse", "HEAD", cwd=repo).strip() != base_head

        report = integrate_patch(repo, patch)
        assert report.applied is False
        assert "app.py" in report.conflicts
        assert report.conflict_report is not None
        assert report.conflict_report.resolution_objective
        assert (repo / "app.py").read_text() == "value = 200\n", "no destructive overwrite"

    def test_stale_base_patch_applies_with_mismatch_note(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        producer = create_worktree(repo, prefix="producer")
        try:
            patch_diff = _worktree_diff(producer.path, {"app.py": "value = 7\n"})
            assert "app.py" in patch_diff
            patch = producer.patch_metadata()
        finally:
            producer.cleanup()
        _git("commit", "-q", "--allow-empty", "-m", "advance", cwd=repo)
        report = integrate_patch(repo, patch)
        assert report.applied is True
        assert "differed from integration HEAD" in report.detail
        assert report.rollback_ref

    def test_pre_integration_review_blocks_unsafe_patch(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        diff = (
            "diff --git a/.git/config b/.git/config\n"
            "--- a/.git/config\n"
            "+++ b/.git/config\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        from eurus.coding.worktree import PatchMetadata

        metadata = PatchMetadata(
            diff=diff,
            base_commit=_git("rev-parse", "HEAD", cwd=repo).strip(),
            changed_paths=(".git/config",),
            diff_hash="0" * 64,
        )
        report = integrate_patch(repo, metadata)
        assert report.applied is False
        assert any("git internals" in finding for finding in report.review_findings)

    def test_post_checks_run_after_apply(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        from eurus.coding.worktree import PatchMetadata

        diff = _diff_for(repo, "app.py", "value = 5\n")
        metadata = PatchMetadata(
            diff=diff,
            base_commit=_git("rev-parse", "HEAD", cwd=repo).strip(),
            changed_paths=("app.py",),
            diff_hash="1" * 64,
        )

        def check(worktree_path: Path) -> list:
            content = (worktree_path / "app.py").read_text()
            return [
                type("R", (), {"name": "content_check", "passed": "5" in content, "detail": ""})()
            ]

        report = integrate_patch(repo, metadata, post_checks=check)
        assert report.applied is True
        assert len(report.post_checks) == 1
        assert report.post_checks[0].passed is True

    def test_empty_patch_is_noop(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        from eurus.coding.worktree import PatchMetadata

        metadata = PatchMetadata(
            diff="",
            base_commit=_git("rev-parse", "HEAD", cwd=repo).strip(),
            changed_paths=(),
            diff_hash="2" * 64,
        )
        report = integrate_patch(repo, metadata)
        assert report.applied is False
        assert "empty patch" in report.detail


def _diff_for(repo: Path, name: str, content: str) -> str:
    producer = create_worktree(repo, prefix="diffgen")
    try:
        return _worktree_diff(producer.path, {name: content})
    finally:
        producer.cleanup()


class TestConflictClassification:
    def test_classifies_patch_failure_with_path(self) -> None:
        report = classify_conflict(
            "error: patch failed: app.py\nerror: app.py: patch does not apply",
            ("app.py",),
        )
        assert report.kind == "overlapping_path"
        assert "app.py" in report.paths
        assert report.resolution_objective
        assert "Automatic overwrite is not permitted" in report.resolution_objective

    def test_classifies_missing_target(self) -> None:
        report = classify_conflict("error: missing.py: does not exist in index", ())
        assert report.kind == "missing_target_file"
        assert "missing.py" in report.paths

    def test_unknown_stderr_falls_back(self) -> None:
        report = classify_conflict("something went wrong", ("a.py", "b.py"))
        assert report.kind == "unknown"
        assert set(report.paths) == {"a.py", "b.py"}


def test_review_patch_flags_secret_exposure() -> None:
    diff = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1 +1 @@\n"
        '-api_key = ""\n'
        '+api_key = "sk-live-abcdefghijklmnop1234567890"\n'
    )
    review = review_patch(diff)
    if not review.approved:
        assert any("secret" in finding for finding in review.findings)


def test_failed_worktree_add_cleans_temp_directory(tmp_path, monkeypatch):
    from eurus.coding import worktree as worktree_module

    _init_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    real_git = worktree_module._git
    created: list[Path] = []

    def failing_git(root, *args, check=True):
        if args and args[0] == "worktree":
            raise WorktreeError("simulated worktree failure")
        return real_git(root, *args, check=check)

    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(worktree_module, "_git", failing_git)
    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)

    with pytest.raises(WorktreeError):
        create_worktree(repo)

    assert created
    assert all(not path.exists() for path in created)
