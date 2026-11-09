from eurus.tui.workspace.diffmodel import DiffModel
from eurus.tui.workspace.diffsession import DiffSession
from eurus.tui.workspace.diffview import render_diff


def test_diff_detects_identical_content():
    model = DiffModel.build("same\ntext\n", "same\ntext\n")
    assert model.identical
    assert not model.hunks
    assert model.stats.total == 0


def test_diff_simple_modification():
    old = "def send_message():\n    sendMessage('')\n"
    new = "def send_message():\n    console.log('hallo world')\n"
    model = DiffModel.build(old, new)
    assert not model.identical
    assert len(model.hunks) == 1
    hunk = model.hunks[0]
    assert hunk.removed == 1
    assert hunk.added == 1
    assert model.stats.removed == 1
    assert model.stats.added == 1
    unified = hunk.unified()
    kinds = [(line.kind, line.text) for line in unified]
    assert ("removed", "    sendMessage('')") in kinds
    assert ("added", "    console.log('hallo world')") in kinds


def test_diff_insertion_and_deletion():
    old = "a\nb\nd\n"
    new = "a\nb\nc\nd\n"
    model = DiffModel.build(old, new)
    assert model.stats.added == 1
    assert model.stats.removed == 0
    assert model.reviewed_text() == new

    model2 = DiffModel.build(new, old)
    assert model2.stats.removed == 1
    assert model2.reviewed_text() == old


def test_diff_multiple_hunks():
    old = "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
    new_lines = [f"line {i}" for i in range(1, 21)]
    new_lines[2] = "CHANGED 3"
    new_lines[16] = "CHANGED 17"
    new = "\n".join(new_lines) + "\n"
    model = DiffModel.build(old, new)
    assert len(model.hunks) == 2
    assert model.stats.added == 2


def test_diff_context_lines():
    old = "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
    new_lines = [f"line {i}" for i in range(1, 21)]
    new_lines[9] = "CHANGED 10"
    new = "\n".join(new_lines) + "\n"
    model = DiffModel.build(old, new)
    hunk = model.hunks[0]
    assert hunk.context_before == ["line 7", "line 8", "line 9"]
    assert hunk.context_after == ["line 11", "line 12", "line 13"]
    model.apply_context(0)
    assert hunk.context_before == []
    assert hunk.context_after == []


def test_diff_hunk_review_composes_content():
    old = "keep\none\ntwo\nkeep\nthree\nkeep\n"
    new = "keep\nONE\nTWO\nkeep\nTHREE\nkeep\n"
    model = DiffModel.build(old, new)
    assert len(model.hunks) >= 2
    model.accept_hunk(0)
    model.reject_hunk(1)
    reviewed = model.reviewed_text()
    lines = reviewed.split("\n")
    assert lines[1] == "ONE"
    assert lines[2] == "TWO"
    assert lines[4] == "three"


def test_diff_reject_all_restores_original():
    old = "one\ntwo\n"
    new = "uno\ndos\n"
    model = DiffModel.build(old, new)
    model.reject_all()
    assert model.reviewed_text() == old
    model.accept_all()
    assert model.reviewed_text() == new


def test_diff_pending_count_and_reset():
    model = DiffModel.build("a\n", "b\n")
    assert model.pending_count() == 1
    model.accept_hunk(0)
    assert model.pending_count() == 0
    model.reset_decisions()
    assert model.pending_count() == 1


def test_side_by_side_rows_are_synchronized():
    old = "same\nremoved\nsame2\n"
    new = "same\nadded\nsame2\n"
    model = DiffModel.build(old, new)
    hunk = model.hunks[0]
    rows = hunk.side_by_side()
    changed = [row for row in rows if row[0] == "changed"]
    assert changed
    left_text = changed[0][1].text
    right_text = changed[0][2].text
    assert left_text == "removed"
    assert right_text == "added"
    context_rows = [row for row in rows if row[0] == "context"]
    assert all(row[1].text == row[2].text for row in context_rows)


def test_diff_large_disjoint_content_terminates():
    old = "\n".join(f"old {i}" for i in range(1500)) + "\n"
    new = "\n".join(f"new {i}" for i in range(1500)) + "\n"
    model = DiffModel.build(old, new)
    assert model.stats.added == 1500
    assert model.stats.removed == 1500


def test_diff_session_review_to_buffer(tmp_path):
    from eurus.tui.workspace.editor_buffer import EditorBuffer

    editor = EditorBuffer.from_text("alpha\nbeta\n")
    editor.insert_text("gamma\n")
    session = DiffSession.for_buffer("rel.py", "rel.py", "alpha\nbeta\n", editor.to_text())
    assert not session.model.identical
    session.reject_current()
    assert session.apply_review_to_buffer(editor)
    assert editor.to_text() == "alpha\nbeta\n"


def test_diff_session_apply_to_file_validates_hash(tmp_path):
    target = tmp_path / "cfg.toml"
    target.write_text("value = 1\n", encoding="utf-8")
    session = DiffSession.for_file(tmp_path, "cfg.toml", "value = 1\n", "value = 2\n")
    assert session.apply_review_to_file()
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    stale = DiffSession.for_file(tmp_path, "cfg.toml", "value = 1\n", "value = 3\n")
    assert not stale.apply_review_to_file()
    assert "changed on disk" in stale.message


def test_diff_session_reject_writes_original(tmp_path):
    target = tmp_path / "cfg.toml"
    target.write_text("value = 1\n", encoding="utf-8")
    session = DiffSession.for_file(tmp_path, "cfg.toml", "value = 1\n", "value = 99\n")
    session.reject_current()
    assert session.apply_review_to_file()
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_diff_session_navigation_and_view_toggle():
    old = "a\nb\nc\nd\n"
    new = "A\nb\nC\nd\n"
    session = DiffSession.for_buffer("f", "f", old, new)
    assert session.current_hunk == 0
    session.next_hunk()
    assert session.current_hunk == 1
    session.next_hunk()
    assert session.current_hunk == 0
    session.previous_hunk()
    assert session.current_hunk == 1
    assert session.view == "unified"
    session.toggle_view()
    assert session.view == "side"
    session.toggle_collapse()
    assert session.model.hunks[0].context_before == []


def test_render_unified_and_side_by_side():
    old = "def send_message():\n    sendMessage('')\n"
    new = "def send_message():\n    console.log('hallo world')\n"
    session = DiffSession.for_buffer("app.js", "app.js", old, new)
    unified = render_diff(session, 100, 30)
    text = "".join(part[1] for part in unified)
    assert "-    sendMessage('')" in text
    assert "+    console.log('hallo world')" in text
    assert "+1" in text and "-2" in text
    session.toggle_view()
    side = render_diff(session, 100, 30)
    side_text = "".join(part[1] for part in side)
    assert "sendMessage" in side_text
    assert "console.log" in side_text


def test_render_identical_diff():
    session = DiffSession.for_buffer("a.txt", "a.txt", "same\n", "same\n")
    out = render_diff(session, 80, 20)
    text = "".join(part[1] for part in out)
    assert "no changes" in text


def test_shell_opens_buffer_diff_and_applies_review(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    editor = ui._open_editor("app.py")
    editor.buffer.goto(1, 0)
    ui._editor_insert(editor, "TWO\n")
    ui._open_buffer_diff("app.py")
    assert ui.tabs.active.kind == "diff"
    diff = ui._active_diff()
    assert diff is not None and not diff.model.identical
    diff.reject_current()
    ui._apply_diff_review()
    assert editor.buffer.to_text() == "one\ntwo\n"
    assert not editor.is_dirty()
    rendered = ui._render_workspace_panel()
    text = "".join(part[1] for part in rendered)
    assert "REVIEW" in text
    assert "accepted" in text or "pending" in text


def test_shell_diff_accept_keeps_changes(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    editor = ui._open_editor("app.py")
    editor.buffer.goto(1, 0)
    ui._editor_insert(editor, "TWO\n")
    ui._open_buffer_diff("app.py")
    diff = ui._active_diff()
    diff.accept_current()
    ui._apply_diff_review()
    assert editor.buffer.to_text() == "one\nTWO\ntwo\n"
    assert editor.is_dirty()
