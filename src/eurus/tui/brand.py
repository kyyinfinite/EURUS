from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from eurus.tui.theme import Theme

NAME = "EURUS"
SUBTITLE = "AI AGENT TERMINAL"
TAGLINE = "THINK / ANALYZE / EXECUTE"
RULE_COLS = 7
GAP_COLS = 1
STAR_GAP = 2

BITMAPS: dict[str, tuple[str, ...]] = {
    "*": ("..#..", ".###.", "#####", ".###.", "..#.."),
    "E": ("#####", "#....", "####.", "#....", "#####"),
    "U": ("#...#", "#...#", "#...#", "#...#", ".###."),
    "R": ("####.", "#...#", "####.", "#..#.", "#...#"),
    "S": (".####", "#....", ".###.", "....#", "####."),
}

WORD: tuple[tuple[str, str], ...] = (
    ("*", "copper"),
    ("E", "steel"),
    ("U", "steel"),
    ("R", "steel"),
    ("U", "steel"),
    ("S", "steel"),
)


def spaced(text: str) -> str:
    return "   ".join(" ".join(word) for word in text.split())


def centered(text: Text, width: int) -> Text:
    pad = max((width - text.cell_len) // 2, 0)
    if not pad:
        return text
    return Text(" " * pad) + text


def _pixel_grid() -> tuple[list[list[str]], list[list[str | None]]]:
    chars: list[list[str]] = []
    colors: list[list[str | None]] = []
    color: str | None

    for position, (key, color) in enumerate(WORD):
        bitmap = BITMAPS[key]
        while len(chars) < len(bitmap):
            chars.append([])
            colors.append([])
        if position:
            gap = STAR_GAP if position == 1 else GAP_COLS
            for char_row in chars:
                char_row.extend([" "] * gap)
            for color_row in colors:
                color_row.extend([None] * gap)
        for index, pattern in enumerate(bitmap):
            for char in pattern:
                chars[index].append("#" if char == "#" else " ")
                colors[index].append(color if char == "#" else None)
    for char_row in chars:
        char_row.extend([" "] * 2)
    for color_row in colors:
        color_row.extend([None] * 2)
    return chars, colors


def wordmark_rows(theme: Theme, color: bool = True) -> list[Text]:
    chars, colors = _pixel_grid()
    style_of = {"copper": theme.copper, "steel": theme.steel}
    rows: list[Text] = []
    for top in range(0, len(chars), 2):
        bottom = top + 1 if top + 1 < len(chars) else None
        bottom_chars = chars[bottom] if bottom is not None else None
        bottom_colors = colors[bottom] if bottom is not None else None
        row = _wordmark_line(
            chars[top],
            bottom_chars,
            colors[top],
            bottom_colors,
            style_of,
            color,
        )
        rows.append(row)
    return rows


def _wordmark_line(
    top: list[str],
    bottom: list[str] | None,
    top_colors: list[str | None],
    bottom_colors: list[str | None] | None,
    style_of: dict[str, str],
    color: bool,
) -> Text:
    row = Text()
    run_char = ""
    run_blank = True
    run_hue: str | None = None
    for column in range(len(top)):
        up = top[column] == "#"
        down = bottom is not None and bottom[column] == "#"
        glyph = "█" if up and down else "▀" if up else "▄" if down else " "
        hue = top_colors[column] or (bottom_colors[column] if bottom_colors is not None else None)
        blank = glyph == " "
        if blank != run_blank or (not blank and hue != run_hue):
            if run_char:
                if run_blank or run_hue is None or not color:
                    row.append(run_char)
                else:
                    row.append(run_char, style=f"bold {style_of[run_hue]}")
            run_char = ""
            run_blank = blank
            run_hue = hue if not blank else None
        run_char += glyph
    if run_char:
        if run_blank or run_hue is None or not color:
            row.append(run_char)
        else:
            row.append(run_char, style=f"bold {style_of[run_hue]}")
    return row


def tagline_text(width: int, theme: Theme, color: bool = True) -> Text:
    rule = theme.glyphs.rule * RULE_COLS
    text = TAGLINE if width < 46 else TAGLINE
    line = Text()
    line.append(rule, style=theme.rule if color else "")
    line.append("  ", style=theme.dim if color else "")
    line.append(text, style=theme.dim if color else "")
    line.append("  ", style=theme.dim if color else "")
    line.append(rule, style=theme.rule if color else "")
    return line


def subtitle_text(width: int, theme: Theme, color: bool = True) -> Text:
    text = spaced(SUBTITLE) if width >= len(spaced(SUBTITLE)) else SUBTITLE
    return Text(text, style=theme.dim if color else "")


def welcome(width: int, height: int, theme: Theme, color: bool = True) -> RenderableType:
    room = height if height > 0 else 999
    lead = max(min(2, room - 6), 0)
    rows: list[RenderableType] = [Text() for _ in range(lead)]
    rows.extend(centered(row, width) for row in wordmark_rows(theme, color))
    rows.append(Text())
    rows.append(centered(tagline_text(width, theme, color), width))
    return Group(*rows)


def header(
    version: str, width: int, height: int, theme: Theme, color: bool = True
) -> RenderableType:
    name = Text(NAME, style=f"bold {theme.steel}" if color else "")
    if version:
        name.append(f"  v{version}", style=theme.dim if color else "")
    return Group(
        *(centered(row, width) for row in wordmark_rows(theme, color)),
        Text(),
        centered(tagline_text(width, theme, color), width),
        Text(),
        centered(name, width),
        Text(),
        centered(subtitle_text(width, theme, color), width),
    )
