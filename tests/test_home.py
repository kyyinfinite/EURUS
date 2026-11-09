import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from eurus import __version__, paths
from eurus.home import (
    ADOPTION_MARKER_NAME,
    HOME_LAYOUT_VERSION,
    HomeError,
    HomeManager,
    resolve_home,
)


def test_resolve_home_prefers_eurus_home_override(tmp_path: Path) -> None:
    environ = {"EURUS_HOME": str(tmp_path / "custom")}
    assert resolve_home(environ) == tmp_path / "custom"
    assert HomeManager(environ).root == tmp_path / "custom"


def test_resolve_home_defaults_to_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EURUS_HOME", raising=False)
    assert resolve_home({}) == tmp_path / ".eurus"
    assert paths.home_dir({}) == tmp_path / ".eurus"


def test_paths_home_dir_and_manager_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    assert paths.home_dir() == HomeManager().root
    assert paths.home_manager().root == tmp_path
    assert paths.user_config_path() == tmp_path / "config.toml"


def test_ensure_layout_creates_required_directories(tmp_path: Path) -> None:
    home = HomeManager(root=tmp_path)
    home.ensure_layout()
    for directory in (tmp_path, tmp_path / "data", tmp_path / "system"):
        assert directory.is_dir()
        assert os.stat(directory).st_mode & 0o777 == 0o700


def test_ensure_layout_tightens_pre_existing_open_directories(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o755)
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o711)
    HomeManager(root=tmp_path).ensure_layout()
    assert os.stat(secrets).st_mode & 0o777 == 0o700
    assert os.stat(logs).st_mode & 0o777 == 0o700


def test_ensure_layout_leaves_missing_optional_directories_absent(tmp_path: Path) -> None:
    HomeManager(root=tmp_path).ensure_layout()
    assert not (tmp_path / "secrets").exists()
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "memory").exists()


def test_manifest_records_version_and_layout(tmp_path: Path) -> None:
    home = HomeManager(root=tmp_path)
    home.ensure_layout()
    manifest = json.loads(home.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["home_layout"] == HOME_LAYOUT_VERSION
    assert home.version_file.read_text(encoding="utf-8").strip() == __version__


def test_manifest_preserves_created_at(tmp_path: Path) -> None:
    home = HomeManager(root=tmp_path)
    home.ensure_layout()
    first = json.loads(home.manifest_path.read_text(encoding="utf-8"))
    home.ensure_layout()
    second = json.loads(home.manifest_path.read_text(encoding="utf-8"))
    assert first["created_at"] == second["created_at"]


def test_ensure_dir_rejects_file_in_place_of_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(HomeError):
        HomeManager(root=tmp_path).ensure_dir(blocker)


def test_database_path_fresh_install_needs_no_legacy(tmp_path: Path) -> None:
    home = HomeManager(root=tmp_path)
    target = home.database_path()
    assert target == tmp_path / "data" / "eurus.sqlite3"
    assert (tmp_path / "data").is_dir()
    assert not target.exists()


def test_database_adoption_copies_legacy_data(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (42)")
    connection.commit()
    connection.close()
    home = HomeManager(root=tmp_path)
    adopted = home.database_path()
    assert adopted == tmp_path / "data" / "eurus.sqlite3"
    assert legacy.exists()
    with sqlite3.connect(adopted) as check:
        assert check.execute("SELECT x FROM t").fetchone() == (42,)
    marker = json.loads((tmp_path / "data" / ADOPTION_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["migrated_from"] == str(legacy)


def test_database_adoption_checkpoints_pending_wal(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    writer = sqlite3.connect(legacy)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (7)")
    writer.commit()
    reader = sqlite3.connect(legacy)
    try:
        adopted = HomeManager(root=tmp_path).database_path()
        with sqlite3.connect(adopted) as check:
            assert check.execute("SELECT x FROM t").fetchone() == (7,)
    finally:
        reader.close()
        writer.close()


def test_database_adoption_is_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.execute("INSERT INTO t VALUES (1)")
    home = HomeManager(root=tmp_path)
    first = home.database_path()
    with sqlite3.connect(first) as connection:
        connection.execute("INSERT INTO t VALUES (2)")
    second = home.database_path()
    assert first == second
    with sqlite3.connect(first) as connection:
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_database_adoption_rejects_corrupt_legacy(tmp_path: Path) -> None:
    (tmp_path / "sessions.sqlite3").write_bytes(b"this is not a database")
    with pytest.raises(HomeError):
        HomeManager(root=tmp_path).database_path()


def test_concurrent_adoption_serializes_on_one_file(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.execute("INSERT INTO t VALUES (5)")
    results: list[Path] = []

    def adopt() -> None:
        results.append(HomeManager(root=tmp_path).database_path())

    threads = [threading.Thread(target=adopt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
    with sqlite3.connect(results[0]) as check:
        assert check.execute("SELECT x FROM t").fetchone() == (5,)


def test_paths_database_path_matches_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
    assert paths.database_path() == HomeManager().database_path()
    assert (tmp_path / "data" / "eurus.sqlite3").exists()


def test_home_accessors_are_stable_paths(tmp_path: Path) -> None:
    home = HomeManager(root=tmp_path)
    assert home.skills_quarantine_dir == tmp_path / "skills" / "quarantine"
    assert home.browser_profile_dir == tmp_path / "browser" / "profile"
    assert home.workspace_dir == tmp_path / "workspace"
    assert home.secrets_dir == tmp_path / "secrets"
    assert home.legacy_database_file == tmp_path / "sessions.sqlite3"


def test_tool_context_accepts_workspace_manager(tmp_path: Path) -> None:
    from eurus.tools.base import ToolContext
    from eurus.workspace import WorkspaceManager

    context = ToolContext(workspace=tmp_path)
    assert context.workspaces is None
    manager = WorkspaceManager(tmp_path)
    equipped = ToolContext(workspace=tmp_path, workspaces=manager)
    assert equipped.workspaces is manager
    assert equipped.workspaces.root == tmp_path
