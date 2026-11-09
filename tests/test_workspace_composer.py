import asyncio

from eurus.tui.workspace.completer import WorkspaceCompleter
from eurus.tui.workspace.fileindex import build_index, fuzzy_score


def make_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "util.ts").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("junk\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("cfg\n", encoding="utf-8")
    return tmp_path


def test_file_index_walks_workspace_and_skips_ignores(tmp_path):
    root = make_files(tmp_path)
    index = build_index(root)
    assert "README.md" in index.files
    assert "src/app.py" in index.files
    assert "docs/notes.md" in index.files
    assert not any("node_modules" in relative for relative in index.files)
    assert not any(relative.startswith(".git") for relative in index.files)
    assert not any(".hidden" in relative for relative in index.files)
    assert index.files == sorted(index.files)


def test_file_index_skips_symlinks(tmp_path):
    root = make_files(tmp_path)
    (root / "linked.py").symlink_to(root / "README.md")
    index = build_index(root)
    assert "linked.py" not in index.files


def test_fuzzy_score_prefers_basename_prefix():
    assert fuzzy_score("src/app.py", "app") < fuzzy_score("src/zapple.py", "app")
    assert fuzzy_score("src/app.py", "zzz") == -1


def test_index_matches_rank_relevant_files(tmp_path):
    root = make_files(tmp_path)
    index = build_index(root)
    matches = index.matches("app")
    assert "src/app.py" in matches
    assert matches[0] == "src/app.py"
    assert index.matches("nothing-matches-this") == []


def test_completer_completes_slash_commands(tmp_path):
    from eurus.tui.commands import COMMANDS

    root = make_files(tmp_path)
    index = build_index(root)
    completer = WorkspaceCompleter(COMMANDS, lambda: index)

    class Doc:
        def __init__(self, text):
            self.text_before_cursor = text

    completions = list(completer.get_completions(Doc("/mod"), None))
    names = [completion.text for completion in completions]
    assert "/model" in names

    completions = list(completer.get_completions(Doc("hello"), None))
    assert completions == []


def test_completer_completes_at_file_references(tmp_path):
    from eurus.tui.commands import COMMANDS

    root = make_files(tmp_path)
    index = build_index(root)
    completer = WorkspaceCompleter(COMMANDS, lambda: index)

    class Doc:
        def __init__(self, text):
            self.text_before_cursor = text

    completions = list(completer.get_completions(Doc("review @app"), None))
    texts = [completion.text for completion in completions]
    assert "src/app.py" in texts
    completions = list(completer.get_completions(Doc("review @notexist"), None))
    assert completions == []
    completions = list(completer.get_completions(Doc("plain text"), None))
    assert completions == []


def test_completer_ignores_at_with_spaces(tmp_path):
    from eurus.tui.commands import COMMANDS

    root = make_files(tmp_path)
    index = build_index(root)
    completer = WorkspaceCompleter(COMMANDS, lambda: index)

    class Doc:
        def __init__(self, text):
            self.text_before_cursor = text

    completions = list(completer.get_completions(Doc("email someone@example"), None))
    assert completions == []


def test_shell_composer_uses_workspace_completer(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    make_files(tmp_path)
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    assert isinstance(ui.buffer.completer, WorkspaceCompleter)
    index = ui._ensure_file_index(force=True)
    assert "src/app.py" in index.files


def test_shell_palette_contains_workspace_actions(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    make_files(tmp_path)
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    items = ui._palette_items()
    labels = [item.label for item in items]
    assert "Open File" in labels
    assert "Toggle Explorer" in labels
    assert "Focus Mode" in labels
    assert "Save Editor" in labels
    payloads = [item.payload for item in items if item.action == "workspace-action"]
    assert "quick-open" in payloads
    assert "preset:workspace" in payloads


def test_shell_palette_workspace_actions_perform(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    make_files(tmp_path)
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    ui.run_workspace_action("toggle-explorer")
    assert not store.explorer_visible
    ui.run_workspace_action("toggle-explorer")
    ui.run_workspace_action("editor-save")
    editor = ui._open_editor("src/app.py")
    editor.buffer.insert_text("z")
    ui.run_workspace_action("editor-save")
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8").startswith("z")
    editor.buffer.goto(0, 0)
    ui._editor_insert(editor, "w")
    ui.run_workspace_action("open-diff")
    assert ui.tabs.active.kind == "diff"
    ui.run_workspace_action("close-tab")
    assert ui.tabs.active.kind != "diff"
    ui.run_workspace_action("close-tab")
    assert ui.tabs.active.kind == "chat"


def test_shell_quick_open_opens_file(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    make_files(tmp_path)
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)

    async def go():
        task = asyncio.create_task(ui._quick_open())
        await asyncio.sleep(0.05)
        ui.search_buffer.text = "notes"
        ui._overlay_enter()
        return await task

    asyncio.run(go())
    assert ui.tabs.active.kind == "editor"
    assert ui.tabs.active.path == "docs/notes.md"
