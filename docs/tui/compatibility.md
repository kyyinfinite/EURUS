# EURUS Workspace Terminal Compatibility

## Selection logic

`eurus tui` picks an interface in this order:

1. Not a tty -> inline Rich interface (unchanged from the baseline).
2. `EURUS_LAYOUT` forces inline/fullscreen as before.
3. Fullscreen and `EURUS_WORKSPACE=off` -> classic fullscreen interface.
4. Fullscreen and at least 100 columns -> advanced workspace
   (`EURUS_WORKSPACE=on` forces it regardless of width, `off` disables).

## Tested environments

The test suite drives the real prompt_toolkit Application through a pipe
input and a Vt100 output, feeding raw ANSI/SGR sequences (arrows, function
keys, Alt-combos, mouse presses, wheel and drag) and asserting on the
rendered frames and the resulting UI state. These tests run headless in CI
and exercise the same code paths a terminal would.

## Platform notes

**Linux/macOS terminals** (xterm, VTE, kitty, Alacritty, iTerm2, Windows
Terminal): full workspace with SGR mouse, truecolor when advertised
(`COLORTERM`), 256-color and basic fallbacks through the existing capability
detector.

**tmux/screen**: `Recolor` already skips OSC recoloring there; the workspace
itself runs normally, including mouse when tmux mouse mode is on.

**Termux (Android)**: detected via `eurus.termux.is_termux`. Typical Termux
windows are under 100 columns, so the default is the classic fullscreen
interface; turn the workspace on explicitly with `EURUS_WORKSPACE=on` in a
wide session (tablet/landscape or external keyboard). Termux-clipboard-set is
probed for system clipboard copies, with OSC52 as the first attempt. The
ASCII glyph set (`/glyphs ascii` or a non-unicode locale) keeps every panel
character pure ASCII: `> v * + x o - |`.

**Non-unicode / dumb terminals**: color level `none` disables styling;
`panel_glyphs` switches to ASCII markers; box-drawing characters are replaced
by `-` and `|`.

## Input protocol behavior

- ESC alone waits up to `Application.timeoutlen` (0.3 s in the workspace) so
  Alt-combos (`ESC` + key) resolve first - the same vi-style timeout the
  classic interface inherits from prompt_toolkit.
- Function keys use their terminal's native sequences (for example F2 is
  `ESC OQ` or `ESC [12~`); prompt_toolkit normalizes both.
- Mouse uses SGR encoding; terminals without mouse support simply never send
  events, and keyboard equivalents exist for every mouse action.
- Bracketed paste is handled by prompt_toolkit (newlines flatten in the
  composer, multi-line paste into the editor preserves them).

## Terminal restoration

The Application enables/disables mouse tracking and the alternate screen as
part of its lifecycle; the render tests assert the disable sequences are
emitted on exit. `Recolor.restore()` and `session.close()` run in `finally`
blocks, so SIGINT/SIGTERM/exceptions still restore the terminal.
