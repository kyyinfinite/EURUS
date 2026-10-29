from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from eurus.tui import fonts, themes, tui_config


class TestTomlRoundTrip:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        choices = tui_config.TuiChoices(theme="midnight", font="hack-nerd", glyphs="nerd")
        assert tui_config.write_choices(choices, tmp_path)
        assert tui_config.read_choices(tmp_path) == choices

    def test_empty_values_are_omitted(self, tmp_path: Path) -> None:
        assert tui_config.write_choices(tui_config.TuiChoices(theme="paper"), tmp_path)
        raw = tomllib.loads((tmp_path / "tui.toml").read_text(encoding="utf-8"))
        assert raw == {"tui": {"theme": "paper"}}
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices(theme="paper")

    def test_special_characters_are_escaped(self, tmp_path: Path) -> None:
        tricky = 'back\\slash "quote" tab\tctl\x01'
        assert tui_config.write_choices(tui_config.TuiChoices(theme=tricky), tmp_path)
        assert tui_config.read_choices(tmp_path).theme == tricky

    def test_broken_or_missing_file_reads_as_defaults(self, tmp_path: Path) -> None:
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices()
        (tmp_path / "tui.toml").write_text("[tui\nbroken", encoding="utf-8")
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices()
        (tmp_path / "tui.toml").write_text("[other]\ntheme = 'x'\n", encoding="utf-8")
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices()

    def test_update_key_preserves_other_keys(self, tmp_path: Path) -> None:
        tui_config.write_choices(
            tui_config.TuiChoices(theme="mono", font="hack-nerd", glyphs="ascii"), tmp_path
        )
        assert tui_config.update_key(tmp_path, "glyphs", "nerd")
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices(
            theme="mono", font="hack-nerd", glyphs="nerd"
        )

    def test_update_key_clears_empty_value(self, tmp_path: Path) -> None:
        tui_config.write_choices(tui_config.TuiChoices(theme="mono", glyphs="nerd"), tmp_path)
        assert tui_config.update_key(tmp_path, "glyphs", "")
        assert tui_config.read_choices(tmp_path) == tui_config.TuiChoices(theme="mono")


class TestLegacyMigration:
    def test_legacy_files_migrate_into_tui_toml_and_are_removed(self, tmp_path: Path) -> None:
        (tmp_path / "theme").write_text("midnight\n", encoding="utf-8")
        (tmp_path / "glyphs").write_text("nerd\n", encoding="utf-8")
        assert tui_config.migrate_legacy_files(tmp_path) is True
        stored = tui_config.read_choices(tmp_path)
        assert stored.theme == "midnight" and stored.glyphs == "nerd"
        assert not (tmp_path / "theme").exists()
        assert not (tmp_path / "glyphs").exists()

    def test_migration_runs_only_once(self, tmp_path: Path) -> None:
        (tmp_path / "theme").write_text("nord\n", encoding="utf-8")
        assert tui_config.migrate_legacy_files(tmp_path) is True
        assert tui_config.migrate_legacy_files(tmp_path) is False
        (tmp_path / "theme").write_text("paper\n", encoding="utf-8")
        assert tui_config.read_choices(tmp_path).theme == "nord"

    def test_no_legacy_files_means_no_migration(self, tmp_path: Path) -> None:
        assert tui_config.migrate_legacy_files(tmp_path) is False
        assert not (tmp_path / "tui.toml").exists()

    def test_existing_tui_toml_is_never_overwritten(self, tmp_path: Path) -> None:
        tui_config.write_choices(tui_config.TuiChoices(theme="mono"), tmp_path)
        (tmp_path / "theme").write_text("paper\n", encoding="utf-8")
        assert tui_config.migrate_legacy_files(tmp_path) is False
        assert tui_config.read_choices(tmp_path).theme == "mono"
        assert (tmp_path / "theme").read_text(encoding="utf-8").strip() == "paper"

    def test_glyph_choice_uses_migrated_value(self, tmp_path: Path) -> None:
        (tmp_path / "glyphs").write_text("ascii\n", encoding="utf-8")
        assert themes.glyph_choice({}, tmp_path) == "ascii"
        assert tui_config.read_choices(tmp_path).glyphs == "ascii"
        assert not (tmp_path / "glyphs").exists()

    def test_active_name_uses_migrated_value(self, tmp_path: Path) -> None:
        (tmp_path / "theme").write_text("dracula\n", encoding="utf-8")
        assert themes.active_name({}, tmp_path) == "dracula"
        assert not (tmp_path / "theme").exists()

    def test_env_overrides_keep_priority(self, tmp_path: Path) -> None:
        themes.save_active("midnight", tmp_path)
        themes.save_glyphs("nerd", tmp_path)
        assert themes.active_name({"EURUS_THEME": "paper"}, tmp_path) == "paper"
        assert themes.glyph_choice({"EURUS_GLYPHS": "ascii"}, tmp_path) == "ascii"
        assert themes.active_name({}, tmp_path) == "midnight"
        assert themes.glyph_choice({}, tmp_path) == "nerd"


class TestThemeFontBinding:
    def test_every_builtin_theme_has_a_bound_font(self) -> None:
        for name in themes.BUILTIN:
            assert themes.bound_font(name) in fonts.CATALOG, name

    def test_user_theme_font_comes_from_toml(self, tmp_path: Path) -> None:
        folder = tmp_path / "themes"
        folder.mkdir()
        (folder / "ember.toml").write_text(
            '[meta]\nextends = "eurus"\n\n[font]\nname = "hack-nerd"\n', encoding="utf-8"
        )
        assert themes.bound_font("ember", tmp_path) == "hack-nerd"
        assert themes.font_for("ember", tmp_path) == "hack-nerd"

    def test_user_theme_without_font_binds_nothing(self, tmp_path: Path) -> None:
        folder = tmp_path / "themes"
        folder.mkdir()
        (folder / "plain.toml").write_text('[meta]\nextends = "eurus"\n', encoding="utf-8")
        assert themes.bound_font("plain", tmp_path) == ""

    def test_saved_font_round_trip_and_priority(self, tmp_path: Path) -> None:
        assert themes.saved_font(tmp_path) == ""
        themes.save_font("Fira-Code-Nerd", tmp_path)
        assert themes.saved_font(tmp_path) == "fira-code-nerd"
        tui_config.update_key(tmp_path, "font", "")
        assert themes.saved_font(tmp_path) == ""

    def test_switching_theme_suggests_bound_font_without_downloading(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from rich.console import Console

        from eurus.config import load_config
        from eurus.tui.renderer import TerminalRenderer
        from eurus.tui.session import InteractiveSession

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        monkeypatch.delenv("EURUS_THEME", raising=False)
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        console = Console(record=True, width=100, force_terminal=False)
        session = InteractiveSession(config, TerminalRenderer(console))
        session._theme_switch("tokyo-night")
        text = console.export_text()
        assert "cascadia-code-nerd" in text
        assert "downloaded automatically" in text
        assert tui_config.read_choices(tmp_path).theme == "tokyo-night"
        assert themes.saved_font(tmp_path) == ""

    def test_explicit_font_override_suppresses_suggestion(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from rich.console import Console

        from eurus.config import load_config
        from eurus.tui.renderer import TerminalRenderer
        from eurus.tui.session import InteractiveSession

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        monkeypatch.delenv("EURUS_THEME", raising=False)
        themes.save_font("cascadia-code-nerd", tmp_path)
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        console = Console(record=True, width=100, force_terminal=False)
        session = InteractiveSession(config, TerminalRenderer(console))
        session._theme_switch("tokyo-night")
        assert "cascadia-code-nerd" not in console.export_text()


class TestPickerLogic:
    def test_move_index_clamps(self) -> None:
        from eurus.tui.picker import move_index

        assert move_index(0, -1, 5) == 0
        assert move_index(2, 1, 5) == 3
        assert move_index(4, 1, 5) == 4
        assert move_index(0, 1, 0) == 0
        assert move_index(9, -3, 5) == 4

    def test_theme_rows_mark_active_and_describe(self) -> None:
        from eurus.tui.picker import theme_rows

        rows = theme_rows(
            ["eurus", "midnight", "paper"],
            "midnight",
            {"eurus": "default", "midnight": "cool", "paper": "light"},
        )
        assert rows == [
            ("  eurus", "default"),
            ("* midnight", "cool"),
            ("  paper", "light"),
        ]

    def test_theme_rows_use_describe_theme_for_custom_themes(self, tmp_path: Path) -> None:
        from eurus.tui.picker import theme_rows

        folder = tmp_path / "themes"
        folder.mkdir()
        (folder / "ember.toml").write_text(
            '[meta]\ndescription = "Warm reds"\nextends = "eurus"\n', encoding="utf-8"
        )
        names = themes.available(tmp_path)
        describe = {name: themes.describe_theme(name, tmp_path) for name in names}
        rows = theme_rows(names, "ember", describe)
        assert next(desc for marker, desc in rows if marker.endswith("ember")) == "Warm reds"
        assert any(marker.startswith("*") and marker.endswith("ember") for marker, _ in rows)


class TestBundledFonts:
    def test_bundled_dir_exists_with_placeholder(self) -> None:
        folder = fonts.bundled_dir()
        assert folder.is_dir()
        assert (folder / "README.md").is_file()
        assert (folder / ".gitkeep").is_file()

    def test_bundled_fonts_ignores_non_font_files(self) -> None:
        assert all(path.suffix.lower() in (".ttf", ".otf") for path in fonts.bundled_fonts())

    def test_install_prefers_bundled_file_over_catalog(self, tmp_path: Path, monkeypatch) -> None:
        bundled = fonts.bundled_dir() / "jetbrains-mono-nerd.ttf"
        bundled.write_bytes(fonts.MAGIC[0] + b"bundled-font-data")
        try:
            env = {"HOME": str(tmp_path), "TERMUX_VERSION": "1"}

            def fail(url, destination):  # download must never be reached
                raise AssertionError("network download must not happen for bundled fonts")

            result = fonts.install_source("jetbrains-mono-nerd", env, fail, lambda c: True)
            assert result.path == tmp_path / ".termux" / "font.ttf"
            assert result.path.read_bytes() == fonts.MAGIC[0] + b"bundled-font-data"
        finally:
            bundled.unlink()

    def test_install_falls_back_to_catalog_download(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "TERMUX_VERSION": "1"}
        TTF = fonts.MAGIC[0] + b"downloaded"

        def fake(url, destination):
            import zipfile

            with zipfile.ZipFile(destination, "w") as bundle:
                bundle.writestr("JetBrainsMonoNerdFontMono-Regular.ttf", TTF)

        result = fonts.install_source("jetbrains-mono-nerd", env, fake, lambda c: True)
        assert result.path.read_bytes() == TTF

    def test_find_bundled_matches_name_and_stem(self, tmp_path: Path, monkeypatch) -> None:
        def fake_bundled():
            return [tmp_path / "MyFont.ttf"]

        monkeypatch.setattr(fonts, "bundled_fonts", fake_bundled)
        assert fonts.find_bundled("myfont.ttf") == tmp_path / "MyFont.ttf"
        assert fonts.find_bundled("MyFont") == tmp_path / "MyFont.ttf"
        assert fonts.find_bundled("other") is None
        assert fonts.find_bundled("  ") is None


class TestFontCommand:
    def build_session(self, tmp_path: Path, monkeypatch):
        from rich.console import Console

        from eurus.config import load_config
        from eurus.tui.renderer import TerminalRenderer
        from eurus.tui.session import InteractiveSession

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        console = Console(record=True, width=120, force_terminal=False)
        return InteractiveSession(config, TerminalRenderer(console)), console

    def test_font_list_shows_bundled_before_catalog(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        bundled = fonts.bundled_dir() / "aa-bundled.ttf"
        bundled.write_bytes(fonts.MAGIC[0])
        try:
            session, console = self.build_session(tmp_path, monkeypatch)
            asyncio.run(session.submit("/font list"))
            text = console.export_text()
            bundled_at = text.index("aa-bundled.ttf")
            assert "bundled" in text
            catalog_at = text.index("jetbrains-mono-nerd")
            assert bundled_at < catalog_at
            assert "(download)" in text
        finally:
            bundled.unlink()

    def test_font_install_persists_choice_for_bundled(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        bundled = fonts.bundled_dir() / "zz-test.ttf"
        bundled.write_bytes(fonts.MAGIC[0] + b"zz")
        try:
            session, console = self.build_session(tmp_path, monkeypatch)
            asyncio.run(session.submit("/font install zz-test"))
            text = console.export_text()
            assert "bundled font zz-test.ttf" in text
            assert "Font preference saved: zz-test" in text
            assert themes.saved_font(tmp_path) == "zz-test"
        finally:
            bundled.unlink()
            (tmp_path / ".termux" / "font.ttf").unlink(missing_ok=True)

    def test_font_install_local_path_does_not_change_preference(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import asyncio

        session, console = self.build_session(tmp_path, monkeypatch)
        local = tmp_path / "local-font.ttf"
        local.write_bytes(fonts.MAGIC[0] + b"local")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("TERMUX_VERSION", "1")
        asyncio.run(session.submit(f"/font install {local}"))
        assert themes.saved_font(tmp_path) == ""


class TestThemePreviewAndRestore:
    def build_session(self, tmp_path: Path, monkeypatch):
        from rich.console import Console

        from eurus.config import load_config
        from eurus.tui.renderer import TerminalRenderer
        from eurus.tui.session import InteractiveSession

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        monkeypatch.delenv("EURUS_THEME", raising=False)
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        console = Console(record=True, width=120, force_terminal=False)
        return InteractiveSession(config, TerminalRenderer(console)), console

    def test_preview_paints_without_saving(self, tmp_path: Path, monkeypatch) -> None:
        session, _ = self.build_session(tmp_path, monkeypatch)
        before = session.renderer.theme
        session.preview_theme("paper")
        assert session.renderer.theme == themes.resolve("paper")
        assert tui_config.read_choices(tmp_path).theme == ""
        session.preview_theme("nope-not-a-theme")
        assert session.renderer.theme == themes.resolve("paper")
        session.paint_theme(before)
        assert session.renderer.theme == before

    def test_theme_menu_switches_and_saves(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        session, console = self.build_session(tmp_path, monkeypatch)
        session.picker = None

        async def fake_ask(label, options, active=""):
            return "paper"

        session._ask_options = fake_ask  # type: ignore[method-assign]
        asyncio.run(session._theme_menu())
        assert session.renderer.theme == themes.resolve("paper")
        assert tui_config.read_choices(tmp_path).theme == "paper"

    def test_theme_menu_quit_keeps_current_theme(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        session, _ = self.build_session(tmp_path, monkeypatch)
        session.picker = None
        themes.save_active("mono", tmp_path)
        before = session.renderer.theme

        async def fake_ask(label, options, active=""):
            return None

        session._ask_options = fake_ask  # type: ignore[method-assign]
        asyncio.run(session._theme_menu())
        assert tui_config.read_choices(tmp_path).theme == "mono"
        assert session.renderer.theme == before

    def test_theme_menu_unknown_answer_loops_then_quits(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        session, console = self.build_session(tmp_path, monkeypatch)
        session.picker = None
        answers = iter(["not-a-theme", "q"])

        async def fake_ask(label, options, active=""):
            return next(answers)

        session._ask_options = fake_ask  # type: ignore[method-assign]
        asyncio.run(session._theme_menu())
        assert "Unknown theme or number" in console.export_text()
        assert tui_config.read_choices(tmp_path).theme == ""

    def test_theme_menu_number_selects(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        session, _ = self.build_session(tmp_path, monkeypatch)
        session.picker = None
        target = themes.available().index("nord")

        async def fake_ask(label, options, active=""):
            return str(target + 1)

        session._ask_options = fake_ask  # type: ignore[method-assign]
        asyncio.run(session._theme_menu())
        assert tui_config.read_choices(tmp_path).theme == "nord"


@pytest.mark.parametrize("name", sorted(themes.THEME_FONTS))
def test_theme_font_keys_reference_catalog(name: str) -> None:
    assert name in themes.BUILTIN
    assert themes.THEME_FONTS[name] in fonts.CATALOG
