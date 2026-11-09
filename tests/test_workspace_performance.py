import time

from eurus.tui.session_ui import ANSI_CACHE_SIZE, parse_ansi_line
from eurus.tui.theme import Theme
from eurus.tui.workspace.diffmodel import DiffModel
from eurus.tui.workspace.editor_buffer import EditorBuffer
from eurus.tui.workspace.fileindex import build_index
from eurus.tui.workspace.views import panel_glyphs, status_glyph


def test_ansi_line_parse_is_cached():
    parse_ansi_line.cache_clear()
    parse_ansi_line("\x1b[38;2;208;138;106mhello\x1b[0m world")
    parse_ansi_line("\x1b[38;2;208;138;106mhello\x1b[0m world")
    parse_ansi_line("\x1b[38;2;208;138;106mhello\x1b[0m world")
    info = parse_ansi_line.cache_info()
    assert info.hits >= 2
    assert info.misses == 1
    assert info.maxsize == ANSI_CACHE_SIZE


def test_large_transcript_render_is_bounded(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    ui, store, session, sink = build_workspace(tmp_path, monkeypatch)
    for number in range(3000):
        sink.write(f"\x1b[32mline {number} of the transcript\x1b[0m\n")
    parse_ansi_line.cache_clear()
    started = time.monotonic()
    for _ in range(10):
        rendered = ui._render_transcript()
        assert rendered
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    info = parse_ansi_line.cache_info()
    assert info.hits > 0


def test_editor_render_slices_large_buffer(tmp_path, monkeypatch):
    from test_workspace_shell import build_workspace

    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(20000)), encoding="utf-8")
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    editor = ui._open_editor("big.py")
    assert len(editor.buffer.lines) == 20000
    started = time.monotonic()
    rendered = ui._render_editor("big.py")
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    text = "".join(part[1] for part in rendered)
    assert "line 0" in text
    assert "\n" in text
    assert len(text.split("\n")) < 100


def test_diff_large_file_computes_quickly():
    old = "\n".join(f"line {i}" for i in range(4000)) + "\n"
    new_lines = [f"line {i}" for i in range(4000)]
    new_lines[100] = "changed"
    new_lines[3900] = "changed too"
    new = "\n".join(new_lines) + "\n"
    started = time.monotonic()
    model = DiffModel.build(old, new)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert len(model.hunks) == 2


def test_file_index_is_bounded(tmp_path):
    for directory in range(10):
        folder = tmp_path / f"dir{directory}"
        folder.mkdir()
        for number in range(50):
            (folder / f"file{number}.txt").write_text("x", encoding="utf-8")
    started = time.monotonic()
    index = build_index(tmp_path)
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert len(index.files) == 500


def test_editor_undo_stays_bounded():
    buffer = EditorBuffer.from_text("seed\n")
    for number in range(500):
        buffer.insert_text(f"chunk{number} ")
        if len(buffer.undo_stack) > 150:
            raise AssertionError("undo stack grew unbounded")
    assert len(buffer.undo_stack) <= 101
    assert buffer.undo()


def test_ascii_glyphs_for_non_unicode_themes():
    theme = Theme.for_unicode(False)
    glyphs = panel_glyphs(theme)
    assert glyphs.rule == "-"
    assert glyphs.splitter == "|"
    assert glyphs.collapsed == ">"
    for glyph in (
        glyphs.collapsed,
        glyphs.expanded,
        glyphs.running,
        glyphs.ok,
        glyphs.fail,
        glyphs.pending,
    ):
        assert glyph.isascii()
    assert status_glyph(glyphs, "running").isascii()
    assert status_glyph(glyphs, "error").isascii()


def test_unicode_glyphs_for_unicode_themes():
    glyphs = panel_glyphs(Theme.for_unicode(True))
    assert glyphs.rule == "─"
    assert status_glyph(glyphs, "ok") == "✓"
