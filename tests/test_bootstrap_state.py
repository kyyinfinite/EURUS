import json
import os
import stat

from eurus.bootstrap.state.backup import backup_file, create_backup_dir
from eurus.bootstrap.state.store import (
    SCHEMA_VERSION,
    BootstrapStore,
    file_mode,
    is_world_readable,
    mark_setup_finished,
    mark_setup_started,
    mark_setup_step,
    migrate_state,
    new_state,
    prune_backups,
)


def _store(tmp_path):
    return BootstrapStore(tmp_path / "home")


def test_store_constructor_does_not_create_dirs(tmp_path):
    store = _store(tmp_path)
    assert not store.dir.exists()


def test_store_save_creates_private_bootstrap_dir(tmp_path):
    store = _store(tmp_path)
    store.save_state(new_state())
    assert store.dir.is_dir()
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700


def test_store_save_and_load_state_round_trip(tmp_path):
    store = _store(tmp_path)
    state = new_state()
    state["features"]["web"] = "installed"
    store.save_state(state)
    loaded = store.load_state()
    assert loaded["features"]["web"] == "installed"
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["created_at"]


def test_store_state_file_is_private(tmp_path):
    store = _store(tmp_path)
    store.save_state(new_state())
    assert stat.S_IMODE(store.state_path.stat().st_mode) == 0o700


def test_load_state_on_corrupt_json_returns_empty(tmp_path):
    store = _store(tmp_path)
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not json", encoding="utf-8")
    assert store.load_state() == {}


def test_save_platform_and_dependencies(tmp_path):
    store = _store(tmp_path)
    store.save_platform({"distro_id": "debian"})
    store.save_dependencies({"httpx": {"status": "READY"}})
    assert json.loads(store.platform_path.read_text())["distro_id"] == "debian"
    assert json.loads(store.dependencies_path.read_text())["httpx"]["status"] == "READY"


def test_history_append_and_limit(tmp_path):
    store = _store(tmp_path)
    for index in range(205):
        store.append_history({"action": f"action-{index}"})
    history = store.load_history()
    assert len(history) == 200
    assert history[-1]["action"] == "action-204"
    assert history[0]["action"] == "action-5"
    assert all("ts" in entry for entry in history)


def test_history_on_non_list_returns_empty(tmp_path):
    store = _store(tmp_path)
    store.history_path.parent.mkdir(parents=True, exist_ok=True)
    store.history_path.write_text('{"not": "a list"}', encoding="utf-8")
    assert store.load_history() == []


def test_interrupted_setup_detection(tmp_path):
    store = _store(tmp_path)
    assert store.interrupted_setup() is None
    state = new_state()
    mark_setup_started(state, ["a", "b"])
    store.save_state(state)
    interrupted = store.interrupted_setup()
    assert interrupted is not None
    assert interrupted["steps"] == {"a": "pending", "b": "pending"}
    store.clear_interrupted()
    assert store.interrupted_setup() is None
    assert store.load_state()["last_quick_setup"]["interrupted"] is True


def test_finished_setup_is_not_interrupted(tmp_path):
    store = _store(tmp_path)
    state = new_state()
    mark_setup_started(state, ["a"])
    mark_setup_finished(state, "done")
    store.save_state(state)
    assert store.interrupted_setup() is None


def test_mark_setup_step_and_finish(tmp_path):
    state = new_state()
    mark_setup_started(state, ["platform", "health"])
    mark_setup_step(state, "platform", "done")
    mark_setup_finished(state, "done")
    assert state["last_quick_setup"]["steps"]["platform"] == "done"
    assert state["last_quick_setup"]["steps"]["health"] == "pending"
    assert state["last_quick_setup"]["status"] == "done"
    assert state["last_quick_setup"]["finished_at"]


def test_mark_helpers_tolerate_missing_quick_setup_key():
    state = {}
    assert mark_setup_step(state, "x", "done") == {}
    mark_setup_finished(state)
    assert "last_quick_setup" not in state


def test_reset_removes_only_state_files(tmp_path):
    store = _store(tmp_path)
    store.save_state(new_state())
    store.save_platform({"a": 1})
    store.save_dependencies({})
    store.append_history({"action": "x"})
    keep = store.dir / "keepme.txt"
    keep.write_text("keep", encoding="utf-8")
    removed = store.reset()
    assert len(removed) == 4
    assert not store.state_path.exists()
    assert not store.platform_path.exists()
    assert not store.dependencies_path.exists()
    assert not store.history_path.exists()
    assert keep.exists()


def test_migrate_state_upgrades_old_payloads():
    migrated = migrate_state({"features": {"web": "yes"}})
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert "created_at" in migrated
    migrated["schema_version"] = 99
    assert migrate_state(migrated)["schema_version"] == SCHEMA_VERSION


def test_file_mode_and_world_readable(tmp_path):
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o644)
    assert is_world_readable(target) is True
    os.chmod(target, 0o600)
    assert is_world_readable(target) is False
    assert file_mode(tmp_path / "missing") == 0


def test_backup_file_copies_and_restricts(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text("data = 1", encoding="utf-8")
    os.chmod(source, 0o644)
    backup_dir = create_backup_dir(tmp_path / "backups", "test")
    backup = backup_file(source, backup_dir)
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "data = 1"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "config.toml" in backup.name


def test_backup_file_collision_gets_unique_name(tmp_path):
    source = tmp_path / "f.txt"
    source.write_text("v1", encoding="utf-8")
    backup_dir = create_backup_dir(tmp_path / "backups", "t")
    first = backup_file(source, backup_dir)
    source.write_text("v2", encoding="utf-8")
    second = backup_file(source, backup_dir)
    assert first != second
    assert first.read_text() == "v1"
    assert second.read_text() == "v2"


def test_create_backup_dir_is_timestamped_and_private(tmp_path):
    backup_dir = create_backup_dir(tmp_path / "backups", "repair")
    assert backup_dir.is_dir()
    assert "repair" in backup_dir.name
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700


def test_prune_backups_keeps_newest(tmp_path):
    backups = tmp_path / "backups"
    for index in range(5):
        entry = backups / f"2026-01-0{index}_t{index}"
        entry.mkdir(parents=True)
        (entry / "data.txt").write_text(str(index), encoding="utf-8")
    removed = prune_backups(backups, keep=2)
    assert len(removed) == 3
    remaining = sorted(item.name for item in backups.iterdir())
    assert remaining == ["2026-01-03_t3", "2026-01-04_t4"]


def test_prune_backups_missing_dir_is_noop(tmp_path):
    assert prune_backups(tmp_path / "nope") == []
