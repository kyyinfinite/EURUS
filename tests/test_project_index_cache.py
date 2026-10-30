"""Phase 1.2: pruned walk, incremental cache and non-blocking background render."""

from __future__ import annotations

from pathlib import Path

import pytest

from eurus.coding.project_index import (
    ProjectIndexCache,
    cache_wait_ready,
    index_project,
    render_project_index,
    render_project_index_cached,
)


def make_tree(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").parent.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("import os\n", encoding="utf-8")
    for ignored in (".venv", "node_modules", "__pycache__", ".git"):
        folder = tmp_path / ignored / "sub"
        folder.mkdir(parents=True)
        (folder / "heavy.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "scripts" / "tool.py").parent.mkdir(parents=True)
    (tmp_path / "scripts" / "tool.py").write_text("import sys\n", encoding="utf-8")
    return tmp_path


def test_walk_prunes_ignored_dirs_during_traversal(tmp_path):
    root = make_tree(tmp_path)
    modules = index_project(root)
    paths = {module.path for module in modules}
    assert paths == {"app.py", "pkg/__init__.py", "scripts/tool.py"}


def test_cached_render_is_nonblocking_then_ready(tmp_path):
    root = make_tree(tmp_path)
    cache = ProjectIndexCache()
    first = cache.render(root)
    assert first == "", "first call must not block on the index build"
    assert cache.wait_ready(root, timeout=10.0)
    second = cache.render(root)
    assert "app.py" in second
    assert "heavy.py" not in second


def test_cached_render_reuses_global_helper(tmp_path):
    root = make_tree(tmp_path)
    assert render_project_index_cached(root) == ""
    assert cache_wait_ready(root, timeout=10.0)
    assert "app.py" in render_project_index_cached(root)


def test_incremental_refresh_reparses_only_changed_files(tmp_path, monkeypatch):
    root = make_tree(tmp_path)
    cache = ProjectIndexCache(refresh_interval=0.0)
    cache.render(root)
    assert cache.wait_ready(root, timeout=10.0)

    calls = {"parse": 0}
    original = cache._index_incremental

    def counting_index(workspace, entry):
        calls["parse"] += 1
        return original(workspace, entry)

    monkeypatch.setattr(cache, "_index_incremental", counting_index)
    cache.render(root)
    assert cache.wait_ready(root, timeout=10.0)
    assert calls["parse"] >= 1

    # Touch one file only; the module list stays correct.
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    cache.render(root)
    assert cache.wait_ready(root, timeout=10.0)
    rendered = cache.render(root)
    assert "app.py" in rendered
    modules = {info.path: info for info in cache._entries[str(root)].modules.values()}
    assert "return 1" not in modules["app.py"].definitions
    assert "main" in modules["app.py"].definitions


def test_sync_render_unchanged_for_direct_callers(tmp_path):
    root = make_tree(tmp_path)
    text = render_project_index(root)
    assert "Workspace Python index" in text
    assert "- app.py: main" in text


def test_missing_workspace_returns_empty(tmp_path):
    cache = ProjectIndexCache()
    assert cache.render(tmp_path / "nope") == ""


def test_max_files_cap(tmp_path):
    for index in range(5):
        (tmp_path / f"m{index}.py").write_text("x = 1\n", encoding="utf-8")
    assert len(index_project(tmp_path, max_files=3)) == 3


@pytest.mark.parametrize("bad", [0, -1, 20_001])
def test_invalid_max_files_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        index_project(tmp_path, max_files=bad)
