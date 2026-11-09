from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eurus.office.models.presentation import DESIGN_DIRECTIONS

FONT_SANS_STACK = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
FONT_SERIF_STACK = "'Georgia', 'Times New Roman', serif"
FONT_MONO_STACK = "'Consolas', 'SFMono-Regular', monospace"


@dataclass(frozen=True, slots=True)
class DesignTokens:
    direction: str
    background: str
    surface: str
    surface_alt: str
    ink: str
    muted: str
    accent: str
    accent_soft: str
    accent_contrast: str
    border: str
    display_font: str
    body_font: str
    title_size_pt: float = 34.0
    subtitle_size_pt: float = 16.0
    body_size_pt: float = 14.0
    kicker_size_pt: float = 11.0
    footer_size_pt: float = 9.0
    radius_px: int = 14

    def css_vars(self) -> str:
        return (
            f"--bg:{self.background};--surface:{self.surface};--surface-alt:{self.surface_alt};"
            f"--ink:{self.ink};--muted:{self.muted};--accent:{self.accent};"
            f"--accent-soft:{self.accent_soft};--accent-contrast:{self.accent_contrast};"
            f"--border:{self.border};--radius:{self.radius_px}px"
        )


_EDITORIAL = DesignTokens(
    direction="editorial",
    background="#F7F5F0",
    surface="#FFFFFF",
    surface_alt="#EFEBE2",
    ink="#1F2937",
    muted="#6B7280",
    accent="#9A3412",
    accent_soft="#FDE8DD",
    accent_contrast="#FFFFFF",
    border="#E2DDD2",
    display_font=FONT_SERIF_STACK,
    body_font=FONT_SANS_STACK,
)

_CORPORATE = DesignTokens(
    direction="corporate",
    background="#FFFFFF",
    surface="#F4F6FA",
    surface_alt="#E9EEF6",
    ink="#0F2540",
    muted="#5A6B84",
    accent="#1D5FAD",
    accent_soft="#DCE9F9",
    accent_contrast="#FFFFFF",
    border="#D8E0EC",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_MINIMAL = DesignTokens(
    direction="minimal",
    background="#FFFFFF",
    surface="#FAFAFA",
    surface_alt="#F0F0F0",
    ink="#111111",
    muted="#777777",
    accent="#111111",
    accent_soft="#E8E8E8",
    accent_contrast="#FFFFFF",
    border="#E5E5E5",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_EDUCATIONAL = DesignTokens(
    direction="educational",
    background="#FFFDF7",
    surface="#FFFFFF",
    surface_alt="#F3F7EC",
    ink="#233326",
    muted="#66766B",
    accent="#2E7D32",
    accent_soft="#E3F2E1",
    accent_contrast="#FFFFFF",
    border="#DFE8DA",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_SCIENTIFIC = DesignTokens(
    direction="scientific",
    background="#FBFBFD",
    surface="#FFFFFF",
    surface_alt="#EEF2F8",
    ink="#1A2332",
    muted="#5C6B80",
    accent="#3B5BDB",
    accent_soft="#E3EAFB",
    accent_contrast="#FFFFFF",
    border="#DBE2EE",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_TECHNICAL = DesignTokens(
    direction="technical",
    background="#0F172A",
    surface="#16213A",
    surface_alt="#1E2B47",
    ink="#E6EDF7",
    muted="#93A3BC",
    accent="#38BDF8",
    accent_soft="#123A52",
    accent_contrast="#06202E",
    border="#27375A",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_HISTORICAL = DesignTokens(
    direction="historical",
    background="#F6F1E7",
    surface="#FDFBF5",
    surface_alt="#EFE7D6",
    ink="#3D2E1E",
    muted="#8A7A62",
    accent="#7C5A2E",
    accent_soft="#EBDCC3",
    accent_contrast="#FFFFFF",
    border="#E0D5BF",
    display_font=FONT_SERIF_STACK,
    body_font=FONT_SERIF_STACK,
)

_DATA_DRIVEN = DesignTokens(
    direction="data_driven",
    background="#0B1220",
    surface="#101A2E",
    surface_alt="#182442",
    ink="#E8F0FE",
    muted="#8FA3C4",
    accent="#22D3A5",
    accent_soft="#123A34",
    accent_contrast="#04211C",
    border="#233457",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_MODERN_KEYNOTE = DesignTokens(
    direction="modern_keynote",
    background="#0A0A0F",
    surface="#14141C",
    surface_alt="#1D1D28",
    ink="#F5F5FA",
    muted="#9C9CB0",
    accent="#7C5CFF",
    accent_soft="#241D45",
    accent_contrast="#FFFFFF",
    border="#2A2A3A",
    display_font=FONT_SANS_STACK,
    body_font=FONT_SANS_STACK,
)

_TOKENS: dict[str, DesignTokens] = {
    tokens.direction: tokens
    for tokens in (
        _EDITORIAL,
        _CORPORATE,
        _MINIMAL,
        _EDUCATIONAL,
        _SCIENTIFIC,
        _TECHNICAL,
        _HISTORICAL,
        _DATA_DRIVEN,
        _MODERN_KEYNOTE,
    )
}

DARK_DIRECTIONS = frozenset({"technical", "data_driven", "modern_keynote"})


def tokens_for_direction(direction: str) -> DesignTokens:
    if direction in _TOKENS:
        return _TOKENS[direction]
    return _EDITORIAL


def is_dark(direction: str) -> bool:
    return direction in DARK_DIRECTIONS


def css_for_tokens(tokens: DesignTokens) -> str:
    return f":root {{{tokens.css_vars()};}}"


def token_summary(direction: str) -> dict[str, Any]:
    tokens = tokens_for_direction(direction)
    return tokens_to_dict(tokens)


def tokens_to_dict(tokens: DesignTokens) -> dict[str, Any]:
    return {
        "direction": tokens.direction,
        "background": tokens.background,
        "surface": tokens.surface,
        "ink": tokens.ink,
        "muted": tokens.muted,
        "accent": tokens.accent,
        "accent_soft": tokens.accent_soft,
        "border": tokens.border,
        "display_font": tokens.display_font,
        "body_font": tokens.body_font,
    }


def valid_direction(direction: str) -> bool:
    return direction in DESIGN_DIRECTIONS
