import os
from pathlib import Path

import pytest

from eurus.home import HomeManager
from eurus.workspace import WorkspaceError, WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceManager(root, home=HomeManager(root=tmp_path / "home"))


def test_scope_id_validation(manager: WorkspaceManager) -> None:
    for bad in ("..", "a/b", "", ".hidden", "x" * 200, "sp ace", "ünicode"):
        with pytest.raises(WorkspaceError):
            manager.project_workspace(bad)
        with pytest.raises(WorkspaceError):
            manager.run_workspace(bad)
        with pytest.raises(WorkspaceError):
            manager.automation_workspace(bad)
    assert manager.project_workspace("demo-1.2") == (
        manager.home.workspace_dir / "projects" / "demo-1.2"
    )


def test_scope_layout(manager: WorkspaceManager) -> None:
    base = manager.home.workspace_dir
    assert manager.run_workspace("r1") == base / "temp" / "runs" / "r1"
    assert manager.automation_workspace("e1") == base / "automation" / "e1"
    assert manager.shared_workspace() == base / "shared"
    assert manager.artifacts_dir() == base / "artifacts"
    assert manager.default_workspace() == manager.root


def test_resolve_rejects_traversal_absolute_and_nul(manager: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError):
        manager.resolve("../outside")
    with pytest.raises(WorkspaceError):
        manager.resolve("/etc/passwd")
    with pytest.raises(WorkspaceError):
        manager.resolve("a\x00b")
    with pytest.raises(WorkspaceError):
        manager.resolve("")
    with pytest.raises(WorkspaceError):
        manager.resolve("a/../../b")


def test_resolve_allows_nested_paths(manager: WorkspaceManager) -> None:
    nested = manager.root / "deep" / "inner"
    nested.mkdir(parents=True)
    assert manager.resolve("deep/inner/file.txt") == nested.resolve() / "file.txt"


def test_resolve_follows_symlinks_but_rejects_escape(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = manager.root / "jump"
    link.symlink_to(outside)
    with pytest.raises(WorkspaceError):
        manager.resolve("jump/secret.txt")
    inside_target = manager.root / "real"
    inside_target.mkdir()
    link_inside = manager.root / "alias"
    link_inside.symlink_to(inside_target)
    assert manager.resolve("alias/file.txt") == (inside_target / "file.txt").resolve()


def test_resolve_strict_rejects_symlink_components(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = manager.root / "rootlink"
    root_link.symlink_to(outside)
    with pytest.raises(WorkspaceError):
        manager.resolve_strict("rootlink/file.txt")
    nested = manager.root / "docs"
    nested.mkdir()
    (nested / "escape").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        manager.resolve_strict("docs/escape/file.txt")
    (nested / "final").symlink_to(outside / "x.txt")
    with pytest.raises(WorkspaceError):
        manager.resolve_strict("docs/final")


def test_resolve_strict_allows_missing_leaf(manager: WorkspaceManager) -> None:
    assert manager.resolve_strict("does/not/exist/yet.txt") == (
        manager.root / "does" / "not" / "exist" / "yet.txt"
    )


def test_validate_containment(manager: WorkspaceManager) -> None:
    inside = manager.root / "sub"
    inside.mkdir()
    assert manager.validate(inside) == inside.resolve()
    assert manager.validate("sub") == inside.resolve()
    with pytest.raises(WorkspaceError):
        manager.validate(inside.parent.parent)
    with pytest.raises(WorkspaceError):
        manager.validate("../../etc")


def test_validate_strict_rejects_hardlinks(manager: WorkspaceManager) -> None:
    original = manager.root / "origin.txt"
    original.write_text("data", encoding="utf-8")
    alias = manager.root / "alias.txt"
    os.link(original, alias)
    with pytest.raises(WorkspaceError):
        manager.validate_strict("alias.txt")
    with pytest.raises(WorkspaceError):
        manager.validate_strict("origin.txt")
    plain = manager.root / "plain.txt"
    plain.write_text("solo", encoding="utf-8")
    assert manager.validate_strict("plain.txt") == plain


def test_open_read_write_round_trip(manager: WorkspaceManager) -> None:
    descriptor = manager.open_write("logs/out.bin")
    try:
        assert os.write(descriptor, b"payload") == 7
    finally:
        os.close(descriptor)
    descriptor = manager.open_read("logs/out.bin")
    try:
        assert os.read(descriptor, 16) == b"payload"
    finally:
        os.close(descriptor)
    with pytest.raises(WorkspaceError):
        manager.open_write("logs/out.bin")
    descriptor = manager.open_write("logs/out.bin", exclusive=False)
    try:
        assert os.write(descriptor, b"x") == 1
    finally:
        os.close(descriptor)
    assert (manager.root / "logs" / "out.bin").read_bytes() == b"x"


def test_open_write_refuses_symlink_final_component(manager: WorkspaceManager) -> None:
    victim = manager.root / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link = manager.root / "sneaky.txt"
    link.symlink_to(victim)
    with pytest.raises(WorkspaceError):
        manager.open_write("sneaky.txt", exclusive=False)
    assert victim.read_text(encoding="utf-8") == "keep"
    with pytest.raises(WorkspaceError):
        manager.open_read("sneaky.txt")


def test_open_write_non_exclusive_refuses_hardlink(manager: WorkspaceManager) -> None:
    original = manager.root / "origin.bin"
    original.write_bytes(b"abc")
    alias = manager.root / "alias.bin"
    os.link(original, alias)
    with pytest.raises(WorkspaceError):
        manager.open_write("alias.bin", exclusive=False)
    assert original.read_bytes() == b"abc"


def test_open_rejects_traversal_and_root(manager: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError):
        manager.open_write("../escape.txt")
    with pytest.raises(WorkspaceError):
        manager.open_write("/tmp/escape.txt")
    with pytest.raises(WorkspaceError):
        manager.open_write(".")


def test_create_and_cleanup_round_trip(manager: WorkspaceManager) -> None:
    scope = manager.project_workspace("alpha")
    created = manager.create(scope)
    assert created.is_dir()
    (created / "artifact.txt").write_text("x", encoding="utf-8")
    manager.cleanup(scope)
    assert not scope.exists()


def test_cleanup_refuses_unmanaged_scopes(manager: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError):
        manager.cleanup(manager.root)
    with pytest.raises(WorkspaceError):
        manager.cleanup(manager.root / "random")
    outside = manager.home.workspace_dir.parent / "elsewhere"
    with pytest.raises(WorkspaceError):
        manager.cleanup(outside)


def test_cleanup_refuses_symlinked_scope(manager: WorkspaceManager) -> None:
    scope = manager.run_workspace("swap")
    manager.create(scope)
    decoy = manager.home.workspace_dir / "decoy"
    decoy.mkdir()
    scope.rmdir()
    scope.symlink_to(decoy)
    with pytest.raises(WorkspaceError):
        manager.cleanup(scope)
    assert decoy.is_dir()


def test_sweep_removes_only_expired_workspaces(manager: WorkspaceManager) -> None:
    old_run = manager.create(manager.run_workspace("old"))
    new_run = manager.create(manager.run_workspace("new"))
    old_automation = manager.create(manager.automation_workspace("old"))
    projects = manager.create(manager.project_workspace("keep"))
    (old_run / "f.txt").write_text("x", encoding="utf-8")
    stale = 1_000_000_000.0
    os.utime(old_run, (stale, stale))
    os.utime(old_automation, (stale, stale))
    removed = manager.sweep(3600.0, now=stale + 7200.0)
    assert removed == 2
    assert not old_run.exists()
    assert not old_automation.exists()
    assert new_run.exists()
    assert projects.exists()


def test_sweep_rejects_non_positive_age(manager: WorkspaceManager) -> None:
    with pytest.raises(WorkspaceError):
        manager.sweep(0)


def test_create_requires_managed_scope(manager: WorkspaceManager) -> None:
    assert manager.create(manager.root).is_dir()
    with pytest.raises(WorkspaceError):
        manager.create(manager.root / "unmanaged")


def test_scenario_agent_cannot_read_user_ssh(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    (tmp_path / "realhome" / ".ssh").mkdir(parents=True)
    with pytest.raises(WorkspaceError):
        manager.validate("~/.ssh/id_rsa")
    with pytest.raises(WorkspaceError):
        manager.validate_strict("~/.ssh/id_rsa")


def test_scenario_agent_cannot_reach_eurus_home(manager: WorkspaceManager) -> None:
    home_root = manager.home.root
    for sensitive in (
        home_root / "secrets",
        home_root / "data" / "eurus.sqlite3",
        home_root / "config.toml",
    ):
        with pytest.raises(WorkspaceError):
            manager.validate(sensitive)


def test_scenario_cross_project_access_is_rejected(manager: WorkspaceManager) -> None:
    alpha = manager.create(manager.project_workspace("alpha"))
    beta = manager.create(manager.project_workspace("beta"))
    (beta / "secret.txt").write_text("beta", encoding="utf-8")
    assert alpha != beta
    with pytest.raises(WorkspaceError):
        manager.validate(alpha / ".." / "beta" / "secret.txt")
    alpha_manager = WorkspaceManager(alpha, home=manager.home)
    with pytest.raises(WorkspaceError):
        alpha_manager.validate(beta / "secret.txt")


def test_scenario_cross_run_access_is_rejected(manager: WorkspaceManager) -> None:
    run_a = manager.create(manager.run_workspace("run-a"))
    run_b = manager.create(manager.run_workspace("run-b"))
    (run_b / "private.txt").write_text("b", encoding="utf-8")
    run_manager = WorkspaceManager(run_a, home=manager.home)
    with pytest.raises(WorkspaceError):
        run_manager.validate(run_b / "private.txt")
    with pytest.raises(WorkspaceError):
        run_manager.resolve("../run-b/private.txt")


def test_scenario_cleanup_is_scoped(manager: WorkspaceManager) -> None:
    alpha = manager.create(manager.project_workspace("alpha"))
    beta = manager.create(manager.project_workspace("beta"))
    (beta / "keep.txt").write_text("keep", encoding="utf-8")
    manager.cleanup(alpha)
    assert not alpha.exists()
    assert (beta / "keep.txt").read_text(encoding="utf-8") == "keep"
