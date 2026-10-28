from __future__ import annotations

from dataclasses import dataclass

from eurus.tui.state import AgentState


@dataclass(frozen=True, slots=True)
class Glyphs:
    prompt: str
    cont: str
    dot: str
    rule: str
    arrow: str
    ok: str
    fail: str
    ellipsis: str
    frames: str
    bar: str
    spark: str
    chat: str = ""
    folder: str = ""
    gear: str = ""
    palette: str = ""
    help: str = ""
    search: str = ""
    attach: str = ""
    shield: str = ""
    chevron: str = ""
    model: str = ""
    enter: str = ""
    ascii: bool = False


UNICODE_GLYPHS = Glyphs(
    "›",
    "…",
    "●",
    "─",
    "▸",
    "✓",
    "✗",
    "…",
    "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "▎",
    "✦",
    "▭",
    "▤",
    "⚙",
    "◐",
    "?",
    "⌕",
    "",
    "◈",
    "⌄",
    "◉",
    "↵",
)
ASCII_GLYPHS = Glyphs(
    ">",
    ".",
    "*",
    "-",
    ">",
    "+",
    "x",
    "...",
    "|/-\\",
    "|",
    "*",
    ">",
    "#",
    "*",
    "*",
    "?",
    ">",
    "",
    "!",
    "v",
    "*",
    ">",
    True,
)
NERD_GLYPHS = Glyphs(
    "❯",
    "…",
    "\uf111",
    "─",
    "\uf054",
    "\uf00c",
    "\uf00d",
    "…",
    "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "▎",
    "\uf0e7",
    "\uf075",
    "\uf07b",
    "\uf013",
    "\uf043",
    "\uf059",
    "\uf002",
    "\uf0c1",
    "\uf132",
    "\uf078",
    "\uf2db",
    "\u23ce",
)

GLYPH_SETS: dict[str, Glyphs] = {
    "unicode": UNICODE_GLYPHS,
    "ascii": ASCII_GLYPHS,
    "nerd": NERD_GLYPHS,
}
GLYPH_KEYS = (
    "prompt",
    "cont",
    "dot",
    "rule",
    "arrow",
    "ok",
    "fail",
    "ellipsis",
    "frames",
    "bar",
    "spark",
    "chat",
    "folder",
    "gear",
    "palette",
    "help",
    "search",
    "attach",
    "shield",
    "chevron",
    "model",
    "enter",
)
DEFAULT_CODE_THEME = "gruvbox-dark"


@dataclass(frozen=True, slots=True)
class Theme:
    teal: str = "#4fb3bf"
    steel: str = "#f0e3c8"
    copper: str = "#e69c6b"
    amber: str = "#e0a45c"
    green: str = "#71c288"
    yellow: str = "#e3c26b"
    red: str = "#e07a7a"
    text: str = "#ece9e6"
    dim: str = "#8b8e95"
    border: str = "#3b4048"
    surface: str = "#0b0f17"
    background: str = "#080a0e"
    glyphs: Glyphs = UNICODE_GLYPHS
    code_theme: str = DEFAULT_CODE_THEME

    @classmethod
    def for_unicode(cls, unicode: bool) -> Theme:
        return cls(glyphs=UNICODE_GLYPHS if unicode else ASCII_GLYPHS)

    @property
    def unicode(self) -> bool:
        return not self.glyphs.ascii

    def state_style(self, state: AgentState) -> str:
        return {
            AgentState.IDLE: self.copper,
            AgentState.THINKING: self.copper,
            AgentState.RUNNING: self.steel,
            AgentState.APPROVAL: self.amber,
            AgentState.ERROR: self.red,
        }[state]

    @property
    def selected_text(self) -> str:
        return "#1a120b"

    @property
    def selected_row(self) -> str:
        return blend(self.copper, "#ffffff", 0.06)

    @property
    def rule(self) -> str:
        return blend(self.border, self.background, 0.55)

    @property
    def header_bar(self) -> str:
        return blend(self.background, self.text, 0.03)


def hex_rgb(value: str) -> tuple[int, int, int]:
    digits = value.lstrip("#")
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def blend(left: str, right: str, ratio: float) -> str:
    low = hex_rgb(left)
    high = hex_rgb(right)
    return "#{:02x}{:02x}{:02x}".format(
        *(round(a + (b - a) * ratio) for a, b in zip(low, high, strict=True))
    )
