# EURUS Advanced TUI Implementation Roadmap

Execution order is fixed: each phase lands with tests and quality gates
before the next starts. Modules live in a new `eurus.tui.workspace` package
so the classic fullscreen interface remains intact as a fallback.

## Phase 0 - Repository audit and baseline (done)

Audit docs written (`architecture-audit.md`, `reference-analysis.md`,
this roadmap, `acceptance-criteria.md`). Baseline recorded:
3889 passed / 1 skipped, ruff clean, mypy strict clean.

## Phase 1 - TUI foundation and architecture

New package `src/eurus/tui/workspace/`:

- `store.py`: `WorkspaceStore` - single observable UI state (focus target,
  panel visibility, sizes, layout preset, tabs, dirty markers).
- `geometry.py`: `PanelGeometry` computation from terminal size + preset +
  user sizes; column/row allocation with minimums; narrow fallback.
- `focus.py`: focus targets and traversal; integration with prompt_toolkit
  focus (`FocusTarget` enum, `focusable()` helpers).
- `renderer_base.py`: panel renderer protocol - a panel renders itself into
  a `Window` region given its geometry, theme and store slice.

Wiring: `fullscreen.py` gains a workspace entry (`run_workspace`) selected
by `workspace_mode()` when the terminal is wide enough; classic fullscreen
remains the fallback. The session is untouched except for an optional
activity bridge (Phase 6 completes it).

## Phase 2 - Advanced workspace layout

- `shell.py`: the workspace Application - header, tab bar, main area
  (VSplit of panels), composer, footer.
- Panels: file explorer (placeholder body first, real model in Phase 3),
  main workspace (chat transcript first; editor/diff tabs later), agent
  activity (feed model in Phase 6).
- Layout presets: `focus` (chat only), `workspace` (explorer + chat +
  activity), `coding` (explorer + editor + activity). Preset switch keeps
  the live session (`F8` cycles).
- Collapsible panels (`F2` explorer, `F3` activity), resizable splitters
  (Alt+, / Alt+. adjust, mouse drag in Phase 8), focus indicators, safe
  fallback under 100 columns to classic fullscreen.

## Phase 3 - Advanced file explorer

- `explorer.py`: lazy tree model rooted at the session workspace; expansion
  state; sorted dirs-first entries; ignore rules (`.git`, `__pycache__`,
  `node_modules`, ...); size/mtime metadata; modified-file markers from the
  editor/diff stores; search filter; recent files list.
- Security: every path passes `resolve_workspace_path`; symlinks skipped;
  permission errors surfaced as tree nodes; binary/large files guarded by
  `MAX_FILE_BYTES`.
- Interactions: keyboard (Up/Down/Enter/Left/Right/typing-to-search) and
  mouse (click select, double-click open, wheel scroll).

## Phase 4 - Integrated code editor

- `editor_buffer.py`: line-based editable buffer with cursor, selection,
  insertion/deletion, undo/redo (command stack), find/replace, line
  numbering, dirty tracking, original-content snapshot, external-change
  detection (mtime+hash), atomic save via `atomic_write_at`, discard.
- `editor.py`: the editor panel - keymap (arrows, Home/End, PageUp/PageDown,
  Ctrl+S save, Ctrl+F find, Ctrl+R replace...), large-file guard, tab
  integration (open file -> tab), dirty marker in tab + explorer.
- `syntax.py`: Pygments lexer adapter (Python, JS/TS, JSON, TOML, Markdown,
  shell, Go, Rust, ...) with theme-aware token colors and a plain fallback.

## Phase 5 - Advanced diff engine

- `diffmodel.py`: `compute_diff` using `difflib.SequenceMatcher` (autojunk
  off) -> `DiffHunk` list with old/new line ranges; context expansion;
  hunk accept/reject producing reviewed content; per-hunk state; stats.
- `diffview.py`: unified view (old line numbers, new line numbers, +/-,
  colored backgrounds) and side-by-side view (two synchronized columns);
  hunk navigation (n/p), context collapse, horizontal scroll.
- Review controls: accept/reject per hunk and per file; results applied
  only after re-validating the file state (hash check) and writing
  atomically; integrated into tabs and into tool `edit`/`write` events.

## Phase 6 - Agent activity and event visualization

- `activity.py`: `ActivityFeed` consuming `AgentEvent` through a
  `WorkspaceBridge` attached to the session (adapter only - the runtime is
  untouched). Items: thinking, tool call cards (name, args summary, status,
  duration), tool results (output preview, error state), file read/write/
  edit detection (tool name + path argument), diff preview handoff,
  approvals (pending/allowed/denied), errors, completion summary with
  usage; nested sub-agent items via `stage`/`step` fields.
- Expand/collapse per item, auto-follow toggle, history cap.
- The bridge reuses `InteractiveSession._consume` as the single tap-in
  point; classic UI behavior is unchanged when no bridge is attached.

## Phase 7 - Advanced composer and command palette

- `fileindex.py`: cached workspace file index (bounded walk, ignore rules)
  for `@` completion and quick open; fuzzy subsequence matching.
- Composer: `@` completions, slash completions (existing), history
  (existing InMemoryHistory), streaming guard, contextual hint bar.
- Command palette: extend existing `ListOverlay` palette with workspace
  commands (layout presets, panel toggles, open file, editor save/discard,
  diff review) - dispatched to real actions.

## Phase 8 - Mouse, navigation and interaction polish

- Mouse routing: click-to-focus panels, click file entries, click tabs,
  wheel scrolling per panel, splitter drag, editor click-to-cursor.
- Keyboard parity for every mouse action; clipboard copy helper
  (pbcopy/clip/wl-copy/xclip/termux-api) with silent failure; mouse
  auto-disabled when the terminal does not report SGR support; terminal
  state restored on exit (prompt_toolkit finalizer + tests).

## Phase 9 - Visual polish and performance

- Cached ANSI parsing per transcript line (parse once, reuse fragments).
- Per-panel render caching keyed on (version, width); geometry memoization.
- Charcoal "workspace" builtin theme aligned with the design direction.
- Empty/loading/error/streaming states for every panel; flicker-free
  resize handling.

## Phase 10 - Final integration, security and release validation

- Independent final audit of architecture, module boundaries, no duplicate
  systems, no circular imports, backward compatibility.
- Security review: path restrictions, atomic writes, external-change
  detection, clipboard, escape-sequence safety, input sanitization,
  approval integrity.
- Full gates: `ruff check`, `ruff format --check`, `mypy src` (strict),
  `pytest -q` plus targeted workspace tests (layout, focus, keys, mouse,
  explorer, editor, save/discard, diff, hunks, activity, autocomplete,
  resize, session integration).
- Final docs: `architecture.md`, `user-guide.md`, `keyboard-shortcuts.md`,
  `compatibility.md`, `implementation-report.md`.

## Constraints applied throughout

- Zero comments in every created or modified source file.
- No new runtime dependencies (prompt_toolkit, Rich, Pygments already
  present; `difflib` is stdlib).
- The agent runtime, gateway, tools, approvals and existing commands are
  consumed, never duplicated.
- Existing tests must keep passing; regressions are fixed, not suppressed.
