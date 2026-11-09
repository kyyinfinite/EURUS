from pathlib import Path

from eurus.tui.workspace.editor import CONFLICT_EXTERNAL, EditorSession
from eurus.tui.workspace.editor_buffer import EditorBuffer


def test_buffer_insert_and_multiline():
    buffer = EditorBuffer.from_text("hello world")
    buffer.goto(0, 5)
    buffer.insert_text(",\nbrave")
    assert buffer.to_text() == "hello,\nbrave world"
    assert buffer.cursor == (1, 5)


def test_buffer_backspace_and_delete():
    buffer = EditorBuffer.from_text("ab\ncd")
    buffer.goto(0, 1)
    buffer.backspace()
    assert buffer.to_text() == "b\ncd"
    buffer.goto(1, 0)
    buffer.backspace()
    assert buffer.to_text() == "bcd"
    buffer.goto(0, 1)
    buffer.delete_forward()
    assert buffer.to_text() == "bd"


def test_buffer_undo_redo_restores_state():
    buffer = EditorBuffer.from_text("one")
    buffer.goto(0, 3)
    buffer.insert_text(" two")
    buffer.insert_text(" three")
    assert buffer.to_text() == "one two three"
    assert buffer.undo()
    assert buffer.to_text() == "one two"
    assert buffer.undo()
    assert buffer.to_text() == "one"
    assert not buffer.undo()
    assert buffer.redo()
    assert buffer.to_text() == "one two"
    assert buffer.redo()
    assert buffer.to_text() == "one two three"
    assert not buffer.redo()


def test_buffer_undo_multiline_insert():
    buffer = EditorBuffer.from_text("a\nb")
    buffer.goto(1, 0)
    buffer.insert_text("x\ny\n")
    assert buffer.to_text() == "a\nx\ny\nb"
    assert buffer.undo()
    assert buffer.to_text() == "a\nb"
    assert buffer.redo()
    assert buffer.to_text() == "a\nx\ny\nb"


def test_buffer_selection_and_delete():
    buffer = EditorBuffer.from_text("alpha\nbeta\ngamma")
    buffer.goto(0, 2)
    buffer.move(1, 1, extend=True)
    assert buffer.selected_text() == "pha\nbet"
    buffer.delete_selection()
    assert buffer.to_text() == "ala\ngamma"
    assert buffer.anchor is None


def test_buffer_select_all_and_copy_text():
    buffer = EditorBuffer.from_text("line1\nline2")
    buffer.select_all()
    assert buffer.selected_text() == "line1\nline2"
    buffer.delete_selection()
    assert buffer.to_text() == ""


def test_buffer_find_and_replace():
    buffer = EditorBuffer.from_text("cat hat bat")
    matches = buffer.find_matches("at")
    assert len(matches) == 3
    buffer.goto(0, 0)
    match = buffer.find_next("hat")
    assert match is not None and buffer.cursor == (0, 4)
    assert buffer.replace_next("hat", "log")
    assert buffer.to_text() == "cat log bat"
    buffer.goto(0, 0)
    assert buffer.replace_all("at", "op") == 2
    assert buffer.to_text() == "cop log bop"


def test_buffer_word_movement():
    buffer = EditorBuffer.from_text("foo bar baz")
    buffer.goto(0, 0)
    buffer.move_word(1)
    assert buffer.col == 4
    buffer.move_word(1)
    assert buffer.col == 8
    buffer.move_word(-1)
    assert buffer.col == 4


def test_buffer_cursor_clamping():
    buffer = EditorBuffer.from_text("ab\ncd")
    buffer.goto(10, 10)
    assert buffer.cursor == (1, 2)
    buffer.move(5, 5)
    assert buffer.cursor == (1, 2)


def make_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_editor_session_open_and_dirty(tmp_path):
    make_file(tmp_path, "a.py", "x = 1\n")
    session = EditorSession.open(tmp_path, "a.py")
    assert session.loaded_ok and not session.readonly
    assert not session.is_dirty()
    session.buffer.insert_text("x = 2\n")
    assert session.is_dirty()


def test_editor_save_persists_atomically(tmp_path):
    make_file(tmp_path, "a.py", "x = 1\n")
    session = EditorSession.open(tmp_path, "a.py")
    session.buffer.goto(0, 0)
    session.buffer.insert_text("# header\n")
    assert session.save()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "# header\nx = 1\n"
    assert not session.is_dirty()
    assert "saved" in session.message


def test_editor_save_preserves_file_mode(tmp_path):
    import os
    import stat

    path = make_file(tmp_path, "script.sh", "echo hi\n")
    os.chmod(path, 0o755)
    session = EditorSession.open(tmp_path, "script.sh")
    session.buffer.insert_text("#!/bin/sh\n")
    assert session.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_editor_discard_restores_original(tmp_path):
    make_file(tmp_path, "a.py", "original\n")
    session = EditorSession.open(tmp_path, "a.py")
    session.buffer.insert_text("junk\n")
    assert session.is_dirty()
    assert session.discard()
    assert session.buffer.to_text() == "original\n"
    assert not session.is_dirty()


def test_editor_detects_external_change_and_blocks_save(tmp_path):
    make_file(tmp_path, "a.py", "base\n")
    session = EditorSession.open(tmp_path, "a.py")
    session.buffer.insert_text("mine\n")
    make_file(tmp_path, "a.py", "base\nexternal edit\n")
    assert session.check_external_change()
    assert session.conflict == CONFLICT_EXTERNAL
    assert not session.save()
    assert session.overwrite()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "mine\nbase\n"


def test_editor_reload_resolves_conflict(tmp_path):
    make_file(tmp_path, "a.py", "base\n")
    session = EditorSession.open(tmp_path, "a.py")
    session.buffer.insert_text("local\n")
    make_file(tmp_path, "a.py", "base\nremote\n")
    session.check_external_change()
    assert session.reload_from_disk()
    assert session.buffer.to_text() == "base\nremote\n"
    assert session.conflict == ""
    assert not session.is_dirty()


def test_editor_readonly_for_binary_and_large(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "big.txt").write_text("y" * 1_100_000, encoding="utf-8")
    binary = EditorSession.open(tmp_path, "blob.bin")
    assert binary.readonly and not binary.loaded_ok
    large = EditorSession.open(tmp_path, "big.txt")
    assert large.readonly and "1 MB" in large.readonly_reason
    assert not binary.save()


def test_editor_rejects_paths_outside_workspace(tmp_path):
    session = EditorSession.open(tmp_path, "../outside.txt")
    assert session.readonly
    assert not session.loaded_ok


def test_editor_undo_after_save(tmp_path):
    make_file(tmp_path, "a.py", "one\n")
    session = EditorSession.open(tmp_path, "a.py")
    session.buffer.insert_text("two\n")
    session.save()
    session.buffer.undo()
    assert session.is_dirty()
    session.buffer.redo()
    session.save()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "two\none\n"


def test_shell_editor_flow(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    editor = ui._open_editor("app.py")
    assert ui.tabs.active.kind == "editor"
    assert ui.tabs.active.dirty is False
    editor.buffer.goto(1, 12)
    ui._editor_insert(editor, " + 41")
    assert editor.is_dirty()
    ui._sync_dirty("app.py")
    assert ui.tabs.find("app.py").dirty is True
    assert ui.explorer.is_dirty("app.py")
    editor.check_external_change()
    assert editor.save()
    ui._sync_dirty("app.py")
    assert ui.tabs.find("app.py").dirty is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == ("def main():\n    return 1 + 41\n")
    rendered = ui._render_workspace_panel()
    text = "".join(part[1] for part in rendered)
    assert "return 1 + 41" in text
    assert "SAVED" in text


def test_shell_editor_find_mode(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "notes.md").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    editor = ui._open_editor("notes.md")
    ui.find_buffer.text = "alpha"
    ui._find_next()
    assert editor.buffer.cursor == (2, 0)
    ui._find_next()
    assert editor.buffer.cursor == (0, 0)
    ui.replace_buffer.text = "gamma"
    assert ui._replace_current.__self__ is not None
    editor.buffer.goto(0, 0)
    ui._replace_every()
    assert editor.buffer.to_text() == "gamma\nbeta\ngamma\n"


def test_editor_key_feed_end_to_end(tmp_path, monkeypatch):
    import asyncio

    from test_workspace_render import build_running_app, teardown

    (tmp_path / "feed.py").write_text("value = 1\n", encoding="utf-8")
    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        pipe.send_text("\x1b")
        await asyncio.sleep(1.5)
        ui._open_editor("feed.py")
        await asyncio.sleep(0.5)
        pipe.send_text("value = 99")
        await asyncio.sleep(0.7)
        editor = ui._active_editor()
        assert editor is not None
        assert editor.is_dirty()
        pipe.send_text("\x13")
        await asyncio.sleep(0.4)
        assert (tmp_path / "feed.py").read_text(encoding="utf-8").startswith("value = 99")
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)


def test_editor_arrow_keys_and_undo_feed(tmp_path, monkeypatch):
    import asyncio

    from test_workspace_render import build_running_app, teardown

    (tmp_path / "arrows.py").write_text("alpha\nbeta\n", encoding="utf-8")
    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        pipe.send_text("\x1b")
        await asyncio.sleep(1.2)
        ui._open_editor("arrows.py")
        await asyncio.sleep(0.4)
        pipe.send_text("Z")
        await asyncio.sleep(0.4)
        editor = ui._active_editor()
        assert editor.buffer.to_text() == "Zalpha\nbeta\n"
        pipe.send_text("\x1b[B")
        await asyncio.sleep(0.3)
        pipe.send_text("\x1b[C")
        await asyncio.sleep(0.3)
        assert editor.buffer.cursor == (1, 2)
        pipe.send_text("Q")
        await asyncio.sleep(0.3)
        assert editor.buffer.to_text() == "Zalpha\nbeQta\n"
        pipe.send_text("\x1a")
        await asyncio.sleep(0.3)
        assert editor.buffer.to_text() == "Zalpha\nbeta\n"
        assert editor.buffer.undo()
        assert editor.buffer.to_text() == "alpha\nbeta\n"
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)
