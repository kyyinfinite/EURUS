from __future__ import annotations

from dataclasses import dataclass

from eurus.office.models.task import OfficeFormat


@dataclass(frozen=True, slots=True)
class DocumentTheme:
    style: str
    heading_font: str
    body_font: str
    heading_color: str
    body_color: str
    accent_color: str
    muted_color: str
    table_header_fill: str
    table_header_color: str
    table_band_fill: str
    callout_fill: str
    callout_warning_fill: str
    callout_success_fill: str
    callout_risk_fill: str
    cover_fill: str
    cover_text: str
    cover_muted: str
    base_size_pt: float = 11.0
    line_spacing: float = 1.15

    @property
    def callout_fill_for_variant(self) -> dict[str, str]:
        return {
            "info": self.callout_fill,
            "warning": self.callout_warning_fill,
            "success": self.callout_success_fill,
            "risk": self.callout_risk_fill,
        }


_ACADEMIC = DocumentTheme(
    style="academic",
    heading_font="Georgia",
    body_font="Cambria",
    heading_color="1F3352",
    body_color="1A1A1A",
    accent_color="1F3352",
    muted_color="595959",
    table_header_fill="1F3352",
    table_header_color="FFFFFF",
    table_band_fill="EDF1F7",
    callout_fill="E8EEF7",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="1F3352",
    cover_text="FFFFFF",
    cover_muted="C9D4E6",
)

_EDITORIAL = DocumentTheme(
    style="editorial",
    heading_font="Georgia",
    body_font="Aptos",
    heading_color="5C2E0E",
    body_color="241C14",
    accent_color="9A3412",
    muted_color="7A6F61",
    table_header_fill="5C2E0E",
    table_header_color="FFFFFF",
    table_band_fill="F7EFE6",
    callout_fill="FDE8DD",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="3B1D08",
    cover_text="FBF6EF",
    cover_muted="E4CDB4",
)

_BUSINESS = DocumentTheme(
    style="business",
    heading_font="Aptos",
    body_font="Aptos",
    heading_color="0F2540",
    body_color="1F2937",
    accent_color="1D5FAD",
    muted_color="5A6B84",
    table_header_fill="0F2540",
    table_header_color="FFFFFF",
    table_band_fill="EEF3FA",
    callout_fill="DCE9F9",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="0F2540",
    cover_text="FFFFFF",
    cover_muted="B9CBDF",
)

_TECHNICAL = DocumentTheme(
    style="technical",
    heading_font="Aptos",
    body_font="Aptos",
    heading_color="123A52",
    body_color="17202B",
    accent_color="0E7490",
    muted_color="5A6B80",
    table_header_fill="123A52",
    table_header_color="FFFFFF",
    table_band_fill="E9F2F6",
    callout_fill="E0F2F7",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="0B2B3A",
    cover_text="E8F6FC",
    cover_muted="9CC3D5",
)

_RESEARCH = DocumentTheme(
    style="research",
    heading_font="Georgia",
    body_font="Cambria",
    heading_color="2E2A4F",
    body_color="1C1B22",
    accent_color="4C4585",
    muted_color="5F5C6E",
    table_header_fill="2E2A4F",
    table_header_color="FFFFFF",
    table_band_fill="EFEFF7",
    callout_fill="E8E7F5",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="2E2A4F",
    cover_text="FFFFFF",
    cover_muted="C6C3E0",
)

_EDUCATIONAL = DocumentTheme(
    style="educational",
    heading_font="Aptos",
    body_font="Aptos",
    heading_color="1F5226",
    body_color="22301F",
    accent_color="2E7D32",
    muted_color="5F7261",
    table_header_fill="1F5226",
    table_header_color="FFFFFF",
    table_band_fill="EDF5EB",
    callout_fill="E3F2E1",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="1F5226",
    cover_text="FFFFFF",
    cover_muted="BFDCC0",
)

_PROPOSAL = DocumentTheme(
    style="proposal",
    heading_font="Aptos",
    body_font="Aptos",
    heading_color="312A4E",
    body_color="21202B",
    accent_color="5B4BA6",
    muted_color="625E77",
    table_header_fill="312A4E",
    table_header_color="FFFFFF",
    table_band_fill="F0EEF9",
    callout_fill="E9E6F7",
    callout_warning_fill="FBF3DC",
    callout_success_fill="E7F3E8",
    callout_risk_fill="F9E4E2",
    cover_fill="312A4E",
    cover_text="FFFFFF",
    cover_muted="C9C2E8",
)

_THEMES: dict[str, DocumentTheme] = {
    theme.style: theme
    for theme in (_ACADEMIC, _EDITORIAL, _BUSINESS, _TECHNICAL, _RESEARCH, _EDUCATIONAL, _PROPOSAL)
}


def theme_for_style(style: str) -> DocumentTheme:
    if style in _THEMES:
        return _THEMES[style]
    return _BUSINESS


def default_style_for_format(fmt: OfficeFormat) -> str:
    if fmt is OfficeFormat.PPTX:
        return "editorial"
    return "business"


def document_styles() -> tuple[str, ...]:
    return tuple(_THEMES)
