from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from eurus import paths
from eurus.coding.checkpoint import CheckpointStore
from eurus.termux import is_termux
from eurus.tui import fonts, themes
from eurus.tui.picker import parse_choice, theme_rows
from eurus.tui.theme import GLYPH_SETS, Theme

if TYPE_CHECKING:
    from eurus.tui.session import InteractiveSession

__all__ = [
    "font_command",
    "font_install",
    "glyphs_command",
    "paint",
    "preview_theme",
    "resolve_theme",
    "theme_command",
    "theme_menu",
    "theme_new",
    "theme_preview",
    "theme_switch",
    "theme_termux",
    "unicode_capabilities",
    "undo_command",
]


def unicode_capabilities(session: InteractiveSession) -> bool:
    from eurus.tui.renderer import capabilities_of

    return capabilities_of(session.renderer.console).unicode


def paint(session: InteractiveSession, theme: Theme) -> None:
    session.renderer.apply_theme(theme)
    if session.picker is not None:
        session.picker.apply_theme(theme)
    if session.recolor is not None:
        session.recolor.apply(theme)


def preview_theme(session: InteractiveSession, name: str) -> None:
    """Paint a theme by name without saving it (used by the live theme picker)."""
    try:
        theme = resolve_theme(session, name)
    except ValueError:
        return
    if theme is not None:
        paint(session, theme)


def resolve_theme(session: InteractiveSession, name: str) -> Theme | None:
    try:
        return themes.resolve(name, unicode_capabilities(session), glyph_set=themes.glyph_choice())
    except ValueError as exc:
        session.renderer.error(str(exc), "Available: " + ", ".join(themes.available()))
        return None


def theme_switch(session: InteractiveSession, name: str) -> None:
    theme = resolve_theme(session, name)
    if theme is None:
        return
    paint(session, theme)
    themes.save_active(name)
    session.renderer.system(f"Theme: {name}")
    wanted = themes.bound_font(name)
    override = themes.saved_font()
    target = fonts.detect_target()
    installed = target.kind == "termux" and target.path is not None and target.path.is_file()
    if wanted and wanted != override and not installed:
        session.renderer.system(
            f"This theme is bound to the {wanted} font: /font install {wanted} "
            "(never downloaded automatically)"
        )


async def theme_command(session: InteractiveSession, argument: str) -> None:
    head, _, rest = argument.strip().partition(" ")
    word, rest = head.lower(), rest.strip()
    if not word:
        await theme_menu(session)
    elif word == "list":
        current = themes.active_name().lower()
        rows = [
            ("*" if item == current else " ", f"{item}  {themes.describe_theme(item)}")
            for item in themes.available()
        ]
        session.renderer.table("Themes", rows)
    elif word == "preview":
        theme_preview(session, rest)
    elif word == "new":
        theme_new(session, rest)
    elif word == "path":
        session.renderer.system(str(themes.user_dir()))
    elif word == "termux":
        theme_termux(session, rest)
    else:
        theme_switch(session, themes.DEFAULT if word == "reset" else word)


async def theme_menu(session: InteractiveSession) -> None:
    """Interactive /theme picker page: list, choose, apply and save."""
    opener = getattr(session.picker, "open_theme", None)
    if callable(opener):
        opener()
        return
    while True:
        names = themes.available()
        current = themes.active_name().lower()
        describe = {name: themes.describe_theme(name, paths.home_dir()) for name in names}
        session.renderer.table("Themes", theme_rows(names, current, describe))
        answer = await session._ask_options("theme>", names, current)
        if answer is None:
            return
        choice = parse_choice(answer, len(names))
        if choice.kind == "quit":
            return
        if choice.kind == "select":
            theme_switch(session, names[choice.index])
            return
        matched = next((name for name in names if name.lower() == choice.text.lower()), None)
        if matched is None:
            session.renderer.error(f"Unknown theme or number: {choice.text}")
            continue
        theme_switch(session, matched)
        return


def theme_preview(session: InteractiveSession, name: str) -> None:
    key = name.lower() or themes.active_name().lower()
    theme = resolve_theme(session, key)
    if theme is not None:
        session.renderer.theme_preview(key, theme)


def theme_new(session: InteractiveSession, argument: str) -> None:
    parts = argument.split()
    if not parts:
        session.renderer.error("Usage: /theme new <name> [base]", "Example: /theme new ocean nord")
        return
    try:
        path = themes.scaffold(parts[0], parts[1] if len(parts) > 1 else themes.DEFAULT)
    except ValueError as exc:
        session.renderer.error(str(exc))
        return
    session.renderer.system(f"Created {path}")
    session.renderer.system(f"Edit it, then switch with /theme {parts[0].lower()}")


def theme_termux(session: InteractiveSession, name: str) -> None:
    if not is_termux():
        session.renderer.warning("/theme termux only works inside Termux.")
        return
    key = name.lower() or themes.active_name().lower()
    theme = resolve_theme(session, key)
    if theme is None:
        return
    path = fonts.user_home() / ".termux" / "colors.properties"
    try:
        themes.write_termux_colors(theme, path)
    except OSError as exc:
        session.renderer.error(f"Cannot write {path}", str(exc))
        return
    reloaded = fonts.run_command(["termux-reload-settings"])
    note = "Termux reloaded." if reloaded else "Run termux-reload-settings to apply."
    session.renderer.system(f"Saved Termux colors for {key}. {note}")


def glyphs_command(session: InteractiveSession, argument: str) -> None:
    choice = argument.strip().lower()
    if not choice:
        current = themes.glyph_choice() or "auto"
        rows = [
            (
                "*" if name == current else " ",
                f"{name}  {g.prompt} {g.dot} {g.arrow} {g.ok} {g.fail} {g.spark}",
            )
            for name, g in GLYPH_SETS.items()
        ]
        rows.append(("*" if current == "auto" else " ", "auto  follow the theme and terminal"))
        session.renderer.table("Glyphs", rows)
        return
    if choice != "auto" and choice not in GLYPH_SETS:
        session.renderer.error(f"Unknown glyph set: {choice}", "Use: unicode, ascii, nerd, auto")
        return
    themes.save_glyphs("" if choice == "auto" else choice)
    theme = resolve_theme(session, themes.active_name().lower())
    if theme is not None:
        paint(session, theme)
        session.renderer.system(f"Glyphs: {choice}")


async def font_command(session: InteractiveSession, argument: str) -> None:
    action, _, rest = argument.strip().partition(" ")
    action, rest = action.lower() or "list", rest.strip()
    target = fonts.detect_target()
    try:
        if action == "list":
            rows = []
            for path in fonts.bundled_fonts():
                rows.append(
                    (
                        path.name,
                        f"bundled, {max(path.stat().st_size // 1024, 1)} KB - install with "
                        f"/font install {path.stem}",
                    )
                )
            rows.extend(
                (spec.key, f"{spec.family} - {spec.note} (download)")
                for spec in fonts.CATALOG.values()
            )
            rows.append(("status", fonts.status(target)))
            session.renderer.table("Fonts", rows)
        elif action == "status":
            session.renderer.system(f"{target.kind}: {fonts.status(target)}")
        elif action == "reset":
            session.renderer.system(await asyncio.to_thread(fonts.reset, target))
        elif action == "install":
            await font_install(session, rest)
        else:
            session.renderer.error(
                f"Unknown font action: {action}", "Use: list, install, status, reset"
            )
    except fonts.FontError as exc:
        session.renderer.error(str(exc))


async def font_install(session: InteractiveSession, source: str) -> None:
    if not source:
        session.renderer.error("Usage: /font install <name|path>", "See /font list")
        return
    spec = fonts.CATALOG.get(source.lower())
    bundled = fonts.find_bundled(source)
    if spec is not None:
        session.renderer.system(f"Downloading {spec.family} from GitHub...")
    elif bundled is not None:
        session.renderer.system(f"Installing bundled font {bundled.name}...")
    result = await asyncio.to_thread(fonts.install_source, source)
    session.renderer.system(result.message)
    key = ""
    if spec is not None:
        key = spec.key
    elif bundled is not None:
        key = bundled.stem.lower()
    if key:
        themes.save_font(key)
        session.renderer.system(f"Font preference saved: {key}")
    if themes.glyph_choice() != "nerd":
        session.renderer.system("Use /glyphs nerd to turn on icon glyphs.")


def undo_command(session: InteractiveSession, argument: str) -> None:
    store = CheckpointStore()
    run_id = argument.strip()
    try:
        checkpoint = store.load(run_id) if run_id else store.latest(session.workspace)
    except ValueError:
        checkpoint = None
    if checkpoint is None:
        session.renderer.system(
            "No restorable checkpoint found. Checkpoints are recorded before approved "
            "file mutations in a run.",
        )
        return
    try:
        restored, refused = store.undo(checkpoint.run_id, session.workspace)
    except ValueError as exc:
        session.renderer.error(str(exc))
        return
    if restored:
        session.renderer.system(
            f"Undid run {checkpoint.run_id[:12]}: restored " + ", ".join(restored)
        )
    for item in refused:
        session.renderer.warning(f"Not restored: {item}")
    if not restored and not refused:
        session.renderer.system("The checkpoint has no restorable files.")
