# EURUS Advanced TUI Acceptance Criteria

Each phase must satisfy its criteria before the next phase starts. All
verification is through tests or direct runtime validation, never
assertion-only.

## Phase 0 - Audit

- [x] All relevant EURUS TUI modules inspected and documented.
- [x] APInex reference behaviors (diff, picker, mouse, keys) understood.
- [x] Baseline test results recorded (3889 passed / 1 skipped).
- [x] Audit documents written under `docs/tui/`.

## Phase 1 - ALL MET - Foundation

- [x] New workspace package imports cleanly with no circular imports
      (test: import every module, run `mypy src` strict).
- [x] `WorkspaceStore` exposes focus, visibility, sizes, preset, tabs with
      change notification (test: mutate -> observers notified).
- [x] Geometry computation adapts to terminal dimensions and clamps user
      sizes to safe ranges (test: parametrized sizes).
- [x] Existing TUI still starts; classic fullscreen unchanged; existing
      TUI tests pass unmodified.

## Phase 2 - ALL MET - Workspace layout

- [x] All four regions render: explorer, main workspace, activity, composer.
- [x] Focus moves between panels; focused panel is visually indicated.
- [x] Panels collapse (F2/F3) and restore without overlap.
- [x] Splitters resize with keyboard; geometry clamped to minimums.
- [x] Layout presets (focus/workspace/coding) switch without losing the
      session; F8 cycles them.
- [x] Terminals narrower than the workspace threshold fall back to the
      classic fullscreen layout (test: `workspace_mode()` matrix).
- [x] No overlapping components at any tested size (test: geometry suite).

## Phase 3 - ALL MET - File explorer

- [x] Real workspace files appear; directory expand/collapse works.
- [x] Selection opens a file into a workspace tab.
- [x] Search/filter returns actual results from the tree.
- [x] Paths outside the workspace and symlinks are rejected
      (test: traversal attempts raise / are skipped).
- [x] Permission errors are shown, not crashed on.
- [x] Modified files show dirty markers wired to the editor state.
- [x] Keyboard and mouse navigation both work.

## Phase 4 - ALL MET - Code editor

- [x] Files open from explorer/tabs; content editable; cursor moves.
- [x] Syntax highlighting renders for common languages; plain fallback for
      unknown/binary-ish content.
- [x] Undo/redo restores prior states; dirty marker appears/disappears.
- [x] Save persists real changes atomically (test: file content on disk,
      mode preserved); discard restores original content.
- [x] External modification detected before save (test: change file behind
      the editor's back; save refuses and surfaces the conflict).
- [x] Find/replace works across the buffer.
- [x] Large files are guarded (open refuses beyond the byte cap with a
      clear message).

## Phase 5 - ALL MET - Diff engine

- [x] Diffs reflect real file states (tests with crafted old/new content).
- [x] Unified and side-by-side views both render with line numbers.
- [x] Added/removed/context lines are accurate; stats correct.
- [x] Hunk navigation moves between hunks; context collapse works.
- [x] Accept/reject per hunk produces the correct resulting content
      (test: partial accepts compose the right buffer).
- [x] Stale-file protection: applying review results re-validates the
      original content hash; mismatch refuses to write.
- [x] Writes are atomic; unrelated code is never overwritten.

## Phase 6 - ALL MET - Agent activity

- [x] Real `AgentEvent` streams (from `AgentRuntime.run_agent` fixtures)
      produce feed items in order.
- [x] Tool cards show name, argument summary, status, duration.
- [x] File read/write/edit events surface file paths.
- [x] Approval requests appear and resolve (allowed/denied).
- [x] Sub-agent activity stays distinguishable (nested/indented).
- [x] Errors are prominent; completion summaries show usage and duration.
- [x] Expand/collapse and scrolling work; long tasks do not block
      rendering (events processed off the render path).
- [x] Classic UI path is unaffected when no bridge is attached.

## Phase 7 - ALL MET - Composer and palette

- [x] `@` completion lists real workspace files with fuzzy filtering.
- [x] Slash completion unchanged from classic behavior.
- [x] Palette contains workspace commands; every entry performs a real
      action (toggle panel, switch preset, open file, save editor, ...).
- [x] Existing slash commands remain functional end-to-end.
- [x] Shortcuts never leak into text input (filter design + tests).

## Phase 8 - ALL MET - Mouse and interaction

- [x] Click focuses panels; clicks select files and tabs; wheel scrolls
      the focused scrollable panel; splitter drag resizes.
- [x] Every mouse action has a keyboard equivalent.
- [x] Clipboard copy works or fails silently with a notice.
- [x] Mouse input is fully restored on exit (test: application finalizer
      runs; no leaked escape sequences in captured output).
- [x] Termux / non-SGR terminals degrade to keyboard-only cleanly.

## Phase 9 - ALL MET - Polish and performance

- [x] No visible layout corruption or flicker in scripted interactions.
- [x] Transcript lines are ANSI-parsed once and cached (test: parse count
      under repeated renders).
- [x] Large transcript/file/diff interactions stay responsive (bounded
      windows, cached windows).
- [x] Resize reflows without errors at boundary sizes.

## Phase 1 - ALL MET0 - Release validation

- [x] `ruff check src/eurus tests` clean; `ruff format --check` clean.
- [x] `mypy src` strict clean.
- [x] `pytest -q` full suite passes with no regressions vs baseline count.
- [x] Targeted workspace tests cover: layouts, focus, keyboard, mouse,
      explorer, editor buffer, save/discard, diff generation, hunk review,
      event rendering, autocomplete, resize, session integration.
- [x] Zero comments in all new/modified source files (automated check).
- [x] Final documentation set complete under `docs/tui/`.
