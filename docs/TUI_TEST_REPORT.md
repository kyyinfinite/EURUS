# EURUS TUI Reconstruction - Test Report

Date: 2026-10-28. Environment: Python 3.12.16, prompt_toolkit 3.0.52,
rich 14.3.3, pytest 9.0.2, ruff 0.17.3, mypy 2.4.0, Linux x86_64 (pty + pyte
0.8.2 harness for live captures).

## 1. Runtime verification (the questions the brief requires answered)

1. **Which executable was tested?** The installed console script `eurus-tui`
   (`/home/z/.venv/bin/eurus-tui`, entry point `eurus.cli:agent_main`), plus the
   `eurus` script for module imports.
2. **Which source directory did it load?** The reconstructed tree at
   `/home/z/my-project/work/eurus/src/eurus`, installed editable
   (`pip install -e .`), verified via `python -c "import eurus; print(eurus.__version__)"`
   -> `0.1.0`.
3. **Was fullscreen actually active?** Yes. The default run (no
   `EURUS_LAYOUT` set) entered the prompt_toolkit fullscreen `Application`
   (alternate screen, hidden cursor). `layout_mode({}, tty=True)` returns
   `fullscreen`; the pty harness confirms the app paints the full 130x32 grid.
4. **Was the screenshot captured from the real application?** Yes. Every PNG in
   `download/eurus-evidence/` was produced by spawning the real `eurus-tui`
   process inside a 130x32 pty (`TERM=xterm-256color`, `COLORTERM=truecolor`),
   feeding its raw output through the pyte terminal emulator, and rendering the
   resulting cell grid with Pillow. No mock renderer, no screenshots of source
   code, no static images displayed by the app.
5. **Which reference differences remain?** See section 5.

## 2. Automated test results

- Full suite: `python -m pytest tests/ -q` -> **1242 passed, 0 failed**
  (~150 s), including the updated TUI contracts:
  - `tests/test_tui_fullscreen.py` (19): transcript follow/scroll, dispatch,
    exit, confirm/ask settle, welcome->transcript replacement, palette open /
    close / search / dispatch / sample-fill, settings persistence, stream
    preview, status footer, raw option entry, transcript color retention, full
    fake-agent turn.
  - `tests/test_tui_palette.py` (12): filtering, clamping, width fitting,
    selection styling, empty state, line counts, detach, prefs roundtrip.
  - `tests/test_tui_pure.py`, `tests/test_tui_theme.py`,
    `tests/test_tui_input.py`, `tests/test_tui.py`,
    `tests/test_model_picker.py`: layout resolution, wordmark/tagline rendering,
    blend math, footer labels at all widths, provider/model menus without the
    legacy panels, inline startup at narrow widths.
- Lint: `ruff check src/ tests/` -> **0 findings**; `ruff format --check` ->
  clean.
- Types: `python -m mypy` (strict, 184 files) -> **Success: no issues**.

## 3. Visual verification matrix

| # | State | Evidence | Result |
|---|-------|----------|--------|
| 1 | Welcome + palette (launch default) | `01-welcome-palette.png` | Matches reference: header, wordmark+tagline, centered palette with search box and the 7 reference commands, copper selected row, floating composer, three-zone footer |
| 2 | Composer focused (palette closed) | `02-composer-focused.png` | Clean canvas, copper-accented composer, placeholder + hints |
| 3 | Palette search | `03-palette-search.png` | "sw" filters to Switch Model, selection highlight intact |
| 4 | Model selector (Ctrl+P) | `04-model-selector.png` | Overlay-only selector, `provider>` prompt, active provider marked and preselected; legacy bordered pages gone |
| 5 | Settings (Ctrl+O) | `05-settings.png` | Theme/Glyphs/Markdown/Stream preview with live values, copper selection |
| 6 | Active conversation (/status) | `06-conversation.png` | Transcript renders under the header with composer + footer intact |
| 7 | Attach (Alt+A) | `07-attach.png` | Workspace file list with folder icons; inserts `@path` into the composer |
| 8 | Inline fallback (EURUS_LAYOUT=inline) | `08-inline-fallback.png` | Compact wordmark welcome, no banner art, no SESSION panel, copper composer |

Comparison artifacts against the reference (normalized to 1536x1024):
- `compare-side-by-side.png` - reference above, reconstruction below.
- `compare-overlay-blend.png` - 50% alpha blend.
- `compare-diff-heat.png` / `compare-mismatch-map.png` - difference heatmap and
  >25%-delta map. Mean absolute pixel difference: **0.0526** (5.3%); pixels
  differing by more than 25%: **6.35%**, concentrated in glyph antialiasing and
  the deviations listed below.

## 4. Functional verification (engine integration)

- Slash commands, model/provider discovery flows, approvals (`confirm`),
  cancellation (Ctrl+C), streaming (StreamView polled by the UI), sessions,
  prefs persistence (`ui.json`), themes (`EURUS_THEME`, `eurus theme use`),
  glyph sets (`EURUS_GLYPHS`, `/glyphs`), recolor/OSC title, and Termux export
  path are unchanged and covered by the suite; the UI only consumes them.
- Verified live in the pty: `/status` output, `/model` overlay with active
  marker, settings toggles, attach insertion, palette search + selection
  dispatching `/clear`, Esc close, and exit through the session.

## 5. Remaining visual differences (and why)

1. **Font metrics.** The mockup uses a designer pixel font (~11.8 px advance,
   variable weight). The terminal renders one fixed-width font per cell, so the
   wordmark is 3 grid rows tall (5 px bitmaps need 2.5 rows minimum) versus the
   mockup's ~2.4-row mark, and letterforms are cleaner (no distressed notches).
2. **Search box height.** The inset palette search box needs 3 grid rows
   (border/input/border); the mockup draws it ~1.2 rows tall. Palette items
   therefore start ~1 row lower, while palette top, width (54/130 = 41.5% vs
   41.4%) and horizontal centering match.
3. **Composer height.** 4 grid rows (border, input, hint row, border) versus the
   mockup's ~2.8; the mockup merges the hint row into the box bottom padding.
   Width (86/130 = 66.2% vs 66.7%) and position (rows 24..27) match.
4. **Item description density.** The mockup packs ~8.2 px per glyph; at
   11.8 px/cell the longest description just fits (24 chars) while settings
   labels clip at 12 characters with an ellipsis.
5. **Icon glyphs.** The default `unicode` set uses monochrome symbols (covered
   by DejaVu Sans Mono and Sarasa Mono SC); the captures use the `nerd` set
   (`EURUS_GLYPHS=nerd`) which reproduces the mockup's icon shapes when a Nerd
   Font is installed (`eurus font install`).
6. **Shortcut labels.** Ctrl+M, Ctrl+, and Ctrl+H are displayed as in the
   reference, but Enter/F1/Ctrl+O aliases perform those actions because VT100
   terminals cannot reliably bind those combos (Ctrl+M == Enter, Ctrl+H ==
   Backspace, Ctrl+, is not a control code).
7. **Window chrome.** The mockup's macOS title bar and traffic lights are outside
   the terminal's renderable area and intentionally not reproduced.
