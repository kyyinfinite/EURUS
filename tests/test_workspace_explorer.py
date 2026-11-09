import os
import stat

import pytest

from eurus.tui.workspace.explorer import IGNORED_DIRECTORIES, FileExplorer
from eurus.tui.workspace.preview import FilePreview, load_preview


def make_workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")
    (tmp_path / "src" / "util.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02\x03")
    (tmp_path / "big.txt").write_text("x" * 1_200_000, encoding="utf-8")
    for ignored in IGNORED_DIRECTORIES:
        (tmp_path / ignored).mkdir(exist_ok=True)
        (tmp_path / ignored / "junk.txt").write_text("junk", encoding="utf-8")
    return tmp_path


def test_explorer_lists_root_directories_first(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    names = [(node.name, node.is_dir) for node in explorer.rows]
    dirs = [name for name, is_dir in names if is_dir]
    files = [name for name, is_dir in names if not is_dir]
    assert "src" in dirs and "docs" in dirs
    assert "README.md" in files and "binary.dat" in files
    first_file = next(i for i, (_, is_dir) in enumerate(names) if not is_dir)
    assert all(is_dir for _, is_dir in names[:first_file])


def test_explorer_ignores_generated_directories(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    relatives = [node.relative for node in explorer.rows]
    for ignored in IGNORED_DIRECTORIES:
        assert ignored not in relatives


def test_explorer_expand_collapse_and_navigation(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "src")
    assert explorer.toggle_selected()
    children = [node.relative for node in explorer.rows if node.relative.startswith("src/")]
    assert "src/app.py" in children and "src/util.ts" in children
    assert any(node.expanded for node in explorer.rows if node.name == "src")
    explorer.collapse_or_parent()
    assert not any(node.relative.startswith("src/") for node in explorer.rows)
    explorer.toggle_selected()
    child_index = next(i for i, node in enumerate(explorer.rows) if node.name == "app.py")
    explorer.selected = child_index
    explorer.collapse_or_parent()
    assert explorer.selected_relative() == "src"
    assert any(node.relative == "src/app.py" for node in explorer.rows)
    explorer.move(1)
    assert explorer.selected_relative() == "src/app.py"


def test_explorer_open_file_records_recent(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "README.md")
    assert explorer.open_selected() == "README.md"
    assert explorer.recent == ["README.md"]
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "src")
    explorer.toggle_selected()
    explorer.selected = next(
        i for i, node in enumerate(explorer.rows) if node.relative == "src/app.py"
    )
    assert explorer.open_selected() == "src/app.py"
    assert explorer.recent == ["src/app.py", "README.md"]


def test_explorer_recent_section_shows_when_collapsed(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "README.md")
    explorer.open_selected()
    recent_rows = [node for node in explorer.rows if node.recent]
    assert any(node.relative == "README.md" for node in recent_rows)
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "src")
    explorer.toggle_selected()
    assert not any(node.recent for node in explorer.rows)


def test_explorer_open_directory_is_rejected(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "docs")
    assert explorer.open_selected() is None


def test_explorer_search_finds_real_files(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.type_filter("app")
    relatives = [hit.relative for hit in explorer.hits]
    assert "src/app.py" in relatives
    explorer.type_filter("app.py")
    explorer.selected = 0
    assert explorer.open_selected() == "src/app.py"
    explorer.type_filter("zzz-nothing")
    assert explorer.row_count() == 0
    assert explorer.open_selected() is None
    explorer.clear_filter()
    assert "README.md" in [node.name for node in explorer.rows]


def test_explorer_search_on_dir_navigates_into_tree(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.type_filter("docs")
    explorer.selected = next(i for i, hit in enumerate(explorer.hits) if hit.is_dir)
    assert explorer.toggle_selected()
    assert explorer.filter == ""
    assert explorer.selected_relative() == "docs"
    assert any(node.relative == "docs/notes.md" for node in explorer.rows)


def test_explorer_scroll_keeps_selection_visible(tmp_path):
    root = make_workspace(tmp_path)
    for index in range(40):
        (root / f"file{index:02}.txt").write_text("x", encoding="utf-8")
    explorer = FileExplorer(root)
    explorer.selected = explorer.row_count() - 1
    explorer.scroll_to(10)
    assert explorer.offset >= explorer.selected - 9
    explorer.selected = 0
    explorer.scroll_to(10)
    assert explorer.offset == 0


def test_explorer_dirty_markers(tmp_path):
    root = make_workspace(tmp_path)
    explorer = FileExplorer(root)
    explorer.mark_dirty("src/app.py", True)
    assert explorer.is_dirty("src/app.py")
    explorer.mark_dirty("src/app.py", False)
    assert not explorer.is_dirty("src/app.py")


def test_explorer_permission_error_is_reported(tmp_path):
    root = make_workspace(tmp_path)
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("x", encoding="utf-8")
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as privileged user")
        explorer = FileExplorer(root)
        explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "locked")
        explorer.toggle_selected()
        children = [node for node in explorer.rows if node.relative.startswith("locked/")]
        assert children and children[0].error
    finally:
        os.chmod(locked, stat.S_IRWXU)


def test_explorer_rejects_paths_outside_workspace(tmp_path):
    root = make_workspace(tmp_path)
    outside = tmp_path.parent / "eurus-outside-file.txt"
    outside.write_text("nope", encoding="utf-8")
    try:
        explorer = FileExplorer(root)
        assert not explorer.inside_root(outside)
        assert explorer.inside_root(root / "README.md")
    finally:
        outside.unlink()


def test_explorer_skips_symlinks(tmp_path):
    root = make_workspace(tmp_path)
    (tmp_path / "linked.py").symlink_to(root / "README.md")
    explorer = FileExplorer(root)
    assert "linked.py" not in [node.name for node in explorer.rows]


def test_preview_loads_text_binary_and_large(tmp_path):
    root = make_workspace(tmp_path)
    text = load_preview(root, "README.md")
    assert text.error == "" and "# project" in text.content
    binary = load_preview(root, "binary.dat")
    assert binary.binary and not binary.content
    large = load_preview(root, "big.txt")
    assert large.too_large and not large.content
    missing = load_preview(root, "nope.txt")
    assert missing.error


def test_preview_render_slices_and_line_numbers(tmp_path):
    root = make_workspace(tmp_path)
    preview = FilePreview(root, "src/app.py")
    assert preview.number_width >= 1
    preview.scroll(1, 2)
    assert preview.offset == 1
    preview.scroll(-10, 2)
    assert preview.offset == 0
    preview.ensure_visible(2, 2)
    assert preview.offset == 1


def test_preview_detects_modification_state(tmp_path):
    root = make_workspace(tmp_path)
    preview = FilePreview(root, "src/app.py")
    assert preview.loaded.size > 0
    (root / "src" / "app.py").write_text("changed\n", encoding="utf-8")
    fresh = FilePreview(root, "src/app.py")
    assert "changed" in fresh.loaded.content


def test_syntax_highlight_python(tmp_path):
    from eurus.tui.workspace.syntax import highlight_line, lexer_for

    lexer = lexer_for("app.py")
    assert lexer is not None
    fragments = highlight_line("def main():", lexer)
    text = "".join(part[1] for part in fragments)
    assert "def main():" in text
    styles = [style for style, _ in fragments]
    assert any("token-keyword" in style for style in styles)
    plain = highlight_line("just text", None)
    assert plain == [("class:tok-whitespace", "just text")]


def test_shell_opens_editable_file_into_editor_tab(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    explorer = ui.explorer
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "src")
    explorer.toggle_selected()
    explorer.selected = next(
        i for i, node in enumerate(explorer.rows) if node.relative == "src/app.py"
    )
    ui._open_explorer_selection()
    assert ui.tabs.active.kind == "editor"
    assert ui.tabs.active.path == "src/app.py"
    rendered = ui._render_workspace_panel()
    text = "".join(part[1] for part in rendered)
    assert "def main():" in text
    assert "1" in text


def test_shell_opens_binary_file_into_preview_tab(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    explorer = ui.explorer
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "blob.bin")
    ui._open_explorer_selection()
    assert ui.tabs.active.kind == "preview"
    rendered = ui._render_workspace_panel()
    text = "".join(part[1] for part in rendered)
    assert "binary" in text.lower()


def test_shell_explorer_render_shows_selection_and_dirty(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    ui.explorer.mark_dirty("app.py", True)
    explorer = ui.explorer
    explorer.selected = next(i for i, node in enumerate(explorer.rows) if node.name == "app.py")
    rendered = ui._render_explorer()
    text = "".join(part[1] for part in rendered)
    assert "app.py" in text
    assert "●" in text
    styles = [part[0] for part in rendered]
    assert any("explorer-selected" in style for style in styles)
