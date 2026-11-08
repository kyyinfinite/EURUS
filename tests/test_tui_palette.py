import json

from prompt_toolkit.buffer import Buffer

from eurus.tui import prefs
from eurus.tui.layout import cell_clip, cell_len
from eurus.tui.palette import ListOverlay, PaletteItem, header_item
from eurus.tui.prefs import ViewPrefs
from eurus.tui.theme import ASCII_GLYPHS, Theme


def overlay_with(items, query=""):
    buffer = Buffer()
    overlay = ListOverlay(items, Theme(), "TEST", buffer=buffer)
    buffer.text = query
    return overlay


def test_cell_len_counts_wide_characters():
    assert cell_len("abc") == 3
    assert cell_len("您好") == 4
    assert cell_len("a您b") == 4


def test_cell_clip_respects_wide_cells_and_ellipsis():
    assert cell_clip("hello", 10) == "hello"
    assert cell_clip("hello world", 8, "...") == "hello..."
    assert cell_clip("您好世界", 5, "...") == "您..."
    assert cell_clip("anything", 0) == ""


def test_overlay_filters_by_tokens_and_skips_headers():
    items = [
        header_item("Session"),
        PaletteItem("New Session", "clear history", action="command", payload="/clear"),
        PaletteItem("Switch Model", "pick model", action="command", payload="/model"),
    ]
    overlay = overlay_with(items, "new")
    assert [item.label for item in overlay.matches()] == ["New Session"]
    overlay.buffer.text = "model pick"
    assert [item.label for item in overlay.matches()] == ["Switch Model"]
    overlay.buffer.text = "zzz"
    assert overlay.matches() == []
    overlay.buffer.text = ""
    assert len(overlay.matches()) == 3


def test_overlay_selection_moves_and_clamps():
    items = [PaletteItem("a"), PaletteItem("b"), PaletteItem("c")]
    overlay = overlay_with(items)
    assert overlay.selected() is not None and overlay.selected().label == "a"
    overlay.move(1)
    assert overlay.selected().label == "b"
    overlay.move(99)
    assert overlay.selected().label == "c"
    overlay.move(-99)
    assert overlay.selected().label == "a"


def test_overlay_window_scrolls_to_selection():
    items = [PaletteItem(f"item{i}") for i in range(30)]
    overlay = overlay_with(items)
    overlay.max_rows = 5
    overlay.index = 20
    window = overlay.window()
    assert len(window) == 5
    assert overlay.selected() in window


def test_overlay_render_fits_width_and_marks_selection():
    items = [
        header_item("Commands"),
        PaletteItem("/model", "Switch provider and model", "Ctrl+P"),
        PaletteItem("/clear", "Clear conversation and screen", "Ctrl+N"),
    ]
    for width in (30, 50, 70, 100):
        overlay = overlay_with(items)
        overlay.index = 1
        text = "".join(part[1] for part in overlay.render(width))
        lines = text.split("\n")
        assert all(cell_len(line) <= width for line in lines)
        assert "COMMANDS" in lines[0]
        assert "/model" in text and "Ctrl+P" in text
    overlay = overlay_with(items)
    overlay.index = 1
    rendered = overlay.render(70)
    selected_styles = [style for style, _ in rendered if "palette-selected" in style]
    assert selected_styles


def test_overlay_render_empty_state():
    overlay = overlay_with([PaletteItem("/model")], "zzz")
    text = "".join(part[1] for part in overlay.render(60))
    assert "No matches for 'zzz'" in text
    assert overlay.selected() is None


def test_overlay_line_count_matches_render():
    items = [header_item("Group"), PaletteItem("a"), PaletteItem("b")]
    overlay = overlay_with(items)
    rendered = overlay.render(70)
    newlines = sum(1 for _, text in rendered if text == "\n")
    assert overlay.line_count() == newlines


def test_overlay_detaches_from_buffer():
    buffer = Buffer()
    overlay = ListOverlay([PaletteItem("x")], Theme(), buffer=buffer)
    before = len(buffer.on_text_changed._handlers)
    overlay.detach()
    assert len(buffer.on_text_changed._handlers) == before - 1
    assert overlay.query == ""


def test_prefs_roundtrip(tmp_path):
    target = tmp_path / "ui.json"
    prefs.save(ViewPrefs(sidebar=False, markdown=False), tmp_path)
    assert target.is_file()
    loaded = prefs.load(home=tmp_path)
    assert loaded is not None
    assert loaded.sidebar is False
    assert loaded.markdown is False
    assert loaded.stream_preview is True


def test_prefs_ignores_invalid_files(tmp_path):
    (tmp_path / "ui.json").write_text("{not json", encoding="utf-8")
    loaded = prefs.load(home=tmp_path)
    assert loaded == ViewPrefs()
    (tmp_path / "ui.json").write_text(json.dumps({"sidebar": "yes", "junk": 1}), encoding="utf-8")
    loaded = prefs.load(home=tmp_path)
    assert loaded.sidebar is True


def test_ascii_theme_overlay_uses_ascii_ellipsis():
    theme = Theme(glyphs=ASCII_GLYPHS)
    overlay = ListOverlay([PaletteItem("a" * 40)], theme)
    text = "".join(part[1] for part in overlay.render(24))
    assert "..." in text
