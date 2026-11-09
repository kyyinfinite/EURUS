# EURUS TUI Architecture Audit

Phase 0 baseline audit of the EURUS terminal interface, recorded before any
workspace implementation started. All statements reference the actual source
tree as extracted from `eurus.zip`.

## 1. Runtime and package layout

```
src/eurus/tui/
    app.py                  entrypoint; inline vs fullscreen selection
    fullscreen.py           FullscreenUI (1204 lines, prompt_toolkit Application)
    fullscreen_renderer.py  FullscreenRenderer + TranscriptSink attach
    renderer.py             TerminalRenderer (Rich, inline layout, event mapping)
    session.py              InteractiveSession (1283 lines, agent + commands)
    prompt.py               PromptBox base, SlashCompleter, styles
    palette.py              ListOverlay + PaletteItem list picker
    transcript.py           TranscriptSink (ANSI capture, 6000/5000 line cap)
    layout.py               cell-width helpers (clip, cell_len, cell_clip)
    theme.py                Theme dataclass + Glyphs + blend()
    themes.py               builtin themes, TOML user themes, persistence
    appearance.py           theme painting / preview / undo flows
    terminal.py             Recolor (OSC 10/11/12), layout_mode()
    state.py                AgentState enum
    prefs.py                ViewPrefs (sidebar, stream_preview, markdown)
    commands.py             COMMANDS tuple + resolve()/suggest()
    picker.py, info.py, brand.py, fonts.py, capabilities.py, redact.py
```

Entry path: `eurus.cli` -> `tui.app.run_tui` -> `run_interactive` ->
`InteractiveSession` -> `layout_mode()` picks `fullscreen` (prompt_toolkit)
or `inline` (Rich loop with PromptBox per turn).

## 2. Current capabilities

- Fullscreen single-column transcript with follow/scroll (PageUp/PageDown,
  Ctrl+Up/Down), welcome art, status footer, streaming indicator.
- Overlay pickers (palette, settings, theme, glyphs, attach, options) built
  on the reusable `ListOverlay`.
- Slash command set (28 commands) handled in `InteractiveSession._handlers`.
- Agent event consumption: `InteractiveSession._consume` maps `AgentEvent`
  kinds `step`, `text`, `text_reset`, `tool_call`, `tool_result`, `done`
  plus pass-through kinds to `renderer.activity()`; streaming text flows
  through `StreamView` (inline `Live`, fullscreen `stream_view`).
- Approval flow: `SessionScopedApprover` -> `InteractiveSession._approve`
  -> `picker.confirm()` or inline `[a/D]` prompt.
- Theme system: 12-color `Theme` + glyph sets (unicode/ascii/nerd) + code
  themes, persisted via `themes.py`; `Recolor` paints terminal colors.
- Capability detection: color level, unicode, width (`capabilities.py`).
- Inline fallback for non-tty environments; `layout_mode()` honors
  `EURUS_LAYOUT=auto|always|fullscreen|inline|never`.

## 3. Agent event model (integration surface)

`eurus.agent.events.AgentEvent` kinds: `step`, `text`, `tool_call`,
`tool_result`, `done`, `lesson`, `skill`, `plan`, `recovery`, `model_call`,
`text_reset`, `timing`, `content_block_*`, `media_input_analyzing`,
`audio_output_*`, `artifact`.

Currently only `text`, `tool_call`, `tool_result`, `done`, `lesson`, `skill`
are rendered; `plan`, `recovery`, `timing`, `artifact` fall through
`renderer.activity()` silently. Tool events carry `ToolCall`
(`id`, `name`, `arguments`) and `ToolResult` (`call_id`, `name`, `output`,
`is_error`, `error_code`).

## 4. Reusable foundations for the workspace

- `eurus.coding.paths`: `resolve_workspace_path` (traversal + symlink
  rejection), `read_text_at` (1 MB cap), `atomic_write_at` (fsync + rename,
  mode-preserving) - ready-made security layer for explorer/editor/diff.
- `eurus.coding.changes.WorkspaceChangeManager` and
  `eurus.coding.checkpoint.CheckpointStore`: change recording and restore.
- `eurus.tui.palette.ListOverlay`: list UI already used by four pickers.
- `eurus.tui.layout`: wide-character-aware clipping utilities.
- `TranscriptSink`: ANSI capture with line caps, already battle-tested.
- `Recolor` + theme infra: full theming pipeline to inherit.

## 5. Gaps (what the workspace must add)

1. Single-column layout only; no panels, no focus model, no tabs.
2. No file explorer; the `attach` overlay lists only top-level entries.
3. No editor; no diff viewer. `propose_patch` output is plain text.
4. Agent activity is interleaved transcript text; no dedicated feed, no
   expand/collapse, no durations, no nesting.
5. No mouse support anywhere in the fullscreen app.
6. `@` file references are not completed (no fuzzy file index).
7. `fullscreen.py` mixes application, layout, rendering, input, overlays and
   command dispatch in one class; further growth there is unsustainable.

## 6. Rendering bottlenecks

- `_render_transcript` re-parses ANSI escape sequences for every visible
  line on every invalidate (`to_formatted_text(ANSI(line))`).
- No dirty-region or partial rendering; the whole screen redraws.
- Composer placeholder/status lambdas call `self._size()` (terminal ioctl)
  during layout - acceptable but must not be called per-cell in new panels.

## 7. State management issues

- UI state (mode, follow, top, interrupts, future) lives as loose
  `FullscreenUI` attributes; no single observable store.
- Session state (agent cache, usage, history) and UI state are interleaved
  through `session.picker` back-references.
- No panel focus concept; `mode` string doubles as focus + modal flag.

## 8. Keyboard binding inventory (must remain conflict-free)

Global: Ctrl+C (cancel/interrupt/double-exit), Ctrl+D (exit/empty),
Ctrl+K (palette), Ctrl+N (/clear), Ctrl+P (/model), Ctrl+W (/workspace),
Ctrl+O (settings), Ctrl+T (theme), F1 (help), Esc+A (attach).
Composer: Enter (send), Ctrl+J / Esc+Enter (newline), Tab (completion),
Backslash continuation. Scroll: PageUp/PageDown, Ctrl+Up/Down.
Overlays: Up/Down/PageUp/PageDown/Tab/Enter/Esc/Ctrl+C/Ctrl+D.
Confirm: y/a/n/d/Enter/Esc.

New workspace bindings must avoid these (Alt+digits, F2/F3/F8 and
panel-local keys while focus is outside the composer are free).

## 9. Terminal compatibility notes

- `Recolor.supported()` refuses tmux/screen/NO_COLOR; fullscreen still runs.
- ASCII glyph set exists for non-unicode terminals; Termux detected via
  `eurus.termux.is_termux()`.
- prompt_toolkit owns SIGWINCH handling; resize reflows the Application.
- Mouse: prompt_toolkit can enable SGR mouse tracking; must degrade on
  terminals without it and must always restore state on exit.

## 10. Baseline test results (recorded before changes)

- `pytest -q` (full suite): **3889 passed, 1 skipped** (~350 s).
- TUI-focused subset: 167 passed.
- `ruff check src/eurus tests`: clean.
- `ruff format --check src/eurus tests`: 711 files formatted, clean.
- `mypy src` (strict): clean, 433 source files.

## 11. Technical debt register

- DEBT-1: `fullscreen.py` and `session.py` are monoliths; growth risk.
- DEBT-2: ANSI re-parse per render (performance ceiling).
- DEBT-3: unrendered event kinds (`plan`, `recovery`, `timing`).
- DEBT-4: `attach` overlay does shallow directory listing only.
- DEBT-5: no tests exercise mouse or multi-panel geometry (nothing existed).
