# EURUS TUI Reconstruction Specification

Source of truth: `file_0000000080508211a80bb543ccb4a119.png` (1536 x 1024 reference mockup).
All measurements below were sampled from the image with PIL (scripts/analyze_ref.py).
Terminal grid basis: 130 columns x 32 rows (cell ~= 11.8 x 32 px at reference scale; content top aligns to the mockup canvas below its 48 px window title bar).

## 1. Measured palette

| Token | Hex (sampled) | Where sampled |
|---|---|---|
| background | #080a0e | main canvas (200,200) (200,890) |
| header bar | #0d0f15 | (700,71) |
| surface (overlay fill) | #0b0f17 | palette interior (760,445) |
| surface-raised (search field) | #0e1116 | (760,373) |
| composer fill | #0c0e14 | (760,800) |
| border | #3b4048 | palette edge x=451 (#3b3f47), x=1086 (#41444c) |
| rule (faint divider) | #262a31 | composer top border y=752 |
| copper accent | #e79c69 | selected row scan y=403..425, accent bar #ec9d6f |
| copper deep | #d78a5f | star glyph (600,218) |
| text | #ece9e6 | item name (540,463) #eeebe9 |
| cream (wordmark) | #f2e3c7 | wordmark fill y=191..246 #f5e4c6 |
| dim | #8b8e95 | description text scan #87898c..#8a8f96 |
| muted-on-copper (selected text) | #271c16 | selected row text y=422 |
| green (safe mode) | #71c288 | footer right |
| state colors | amber #e0a45c, red #e07a7a, yellow #e3c26b, teal #4fb3bf, steel #f0e3c8 | theme semantics |

## 2. Frame geometry (130 x 32 grid)

| Zone | Reference px | Grid rows/cols |
|---|---|---|
| Header bar | y 48..97 | row 0 (bar) + row 1 (rule) |
| Wordmark | y 185..247, x 573..967 (+ star from 578) | rows 4..6, centered (5 px bitmaps, half-block rendered) |
| Tagline + rules | y 272..287 | row 9, centered |
| Command palette | x 451..1087, y 338..700 | width 54 cols, rows 10..22, horizontally centered |
| Search field | x 467..1070, y 356..392 | palette rows 1..3 (inset box) |
| Items | first row y 402..444, pitch ~39.4 px | 7 item rows, one grid row each |
| Composer | x 256..1281, y 752..843 | width 86 cols, rows 24..27, centered; left copper accent x 256..261 |
| Footer rule | y 930..932 | row 30 (two spacer rows 28..29 after the composer) |
| Footer | y 932..992 | row 31 (flush bottom; the mockup keeps ~40 px window margin) |

Horizontal centers: palette center x=769 (image center 768) -> centered column; composer center x=768.5 -> centered.

## 3. Component specification

### Header (row 0)
- Left: `>_` (copper, bold) + `EURUS` (bold cream) + `│` (border) + `New Session` (dim; becomes `N messages` after history).
- Right: `EURUS · AI Agent Terminal` (dim) + `✦` (copper).
- Bar background #0d0f15 across full width; rule row of `─` at #262a31.

### Welcome
- Wordmark: pixel star (copper) + `EURUS` letters built from 5x5 px bitmaps rendered with half blocks (cream), centered on rows 5..7.
- Tagline: `────  T H I N K  /  A N A L Y Z E  /  E X E C U T E  ────` dim, centered on row 9.
- No ASCII banner art, no session panel, no bordered boxes on the welcome canvas.

### Command palette (centered float, width 54)
- Outer rounded border (#3b4048) on surface fill #0b0f17.
- Search inset box: rounded border, magnifier icon + `Search commands...` placeholder (dim), focused input cream.
- Flat item list (no category headers), exactly the reference set first:
  1. Chat / Start a new conversation / ↵ (selected)
  2. New Session / Create a new session / Ctrl+N
  3. Switch Model / Change AI model / Ctrl+M
  4. Workspaces / Manage workspaces / Ctrl+W
  5. Settings / Open settings / Ctrl+,
  6. Theme / Change terminal theme / Ctrl+T
  7. Help / Show help and shortcuts / Ctrl+H
- Remaining slash commands stay listed after the reference set (search still filters all).
- Item anatomy: `icon name ... description ... shortcut-right`.
- Selected row: full-width copper background, dark text, arrow marker.
- No footer hint row inside the palette (reference has none).

### Composer (centered, width 80)
- Row 1: `╭───...───╮` top border, left corner copper.
- Row 2: copper accent column `▌` + `❯` copper + placeholder `Ask anything, explore ideas, or work through a task...` (dim) or live text (cream) with cursor.
- Row 3: right-aligned hints `Attach │ Ctrl+Enter Send` (key words emphasized).
- Row 4: `╰───...───╯` bottom border, left corner copper.
- The composer floats in the middle-lower canvas; it is not attached under the footer.

### Footer (row 29 rule + row 30 status)
- Left: `● Ready` (state color) -- state-driven.
- Center: `⚙ Model qwen3.8-27b ⌄ │ ▤ Workspace ~/eurus ⌄` (dim labels, cream values, chevrons).
- Right: `✓ Safe Mode` (green; maps permission mode ask/auto-edit/yolo) + `EURUS v0.1.0` (dim).

## 4. Color style map (prompt_toolkit classes)

- `header`, `rule`, `title`, `glyph`, `dim`, `input`, `placeholder`, `accent`
- `palette-border`, `palette-icon`, `palette-item`, `palette-desc`, `palette-key`
- `palette-selected` = bg copper fg #1a120b (also applied to desc/icon/key of the row)
- `composer-accent` = bg copper (full-height left column)
- `footer-state`, `footer-value`, `footer-mode`

## 5. Interaction contract (all real)

| Action | Binding |
|---|---|
| Open/close palette | Ctrl+K / Esc |
| Navigate items | Up/Down/Tab, Enter select, PgUp/PgDn |
| New session | Ctrl+N (also palette item) |
| Switch model | Ctrl+P alias + palette item (label Ctrl+M per reference; Ctrl+M == Enter at VT100 so the alias guarantees function) |
| Workspaces | Ctrl+W |
| Settings | Ctrl+O alias + palette item (label Ctrl+, per reference; Ctrl+, is not a VT100 control code) |
| Theme | Ctrl+T |
| Help | F1 alias + palette item (label Ctrl+H per reference; Ctrl+H == Backspace at VT100) |
| Attach | Alt+A opens workspace file list, inserts `@path` into the composer |
| Send | Enter (and Ctrl+Enter where the terminal distinguishes it) |
| Newline | Ctrl+J or trailing backslash |
| Cancel | Ctrl+C (contextual), Esc |
| Scroll transcript | PgUp/PgDn, Ctrl+Up/Down |

## 6. Deliberate deviations (terminal constraints)

1. Palette search box is 3 grid rows (top/input/bottom border) because a bordered inset needs a full border; the mockup's 38 px box cannot be drawn thinner.
2. Icon shapes for the default `unicode` glyph set use monochrome symbols covered by DejaVu Sans Mono and Sarasa Mono SC; the `nerd` set uses true Nerd Font codepoints (`eurus font install`).
3. Ctrl+M / Ctrl+, / Ctrl+H are displayed per the reference but reliable aliases are bound, because those combos collide with Enter / non-control / Backspace in VT100.
4. The reference mockup is a designer rendering at ~12.8x32 px cells; positions are matched in grid terms (row/col), not pixels.
