# EURUS Advanced TUI Architecture

This document describes the workspace architecture as implemented. The audit
that preceded it lives in `architecture-audit.md`; the reference study is in
`reference-analysis.md`.

## Topology

```
eurus tui (cli) 
  └─ tui.app.run_interactive
       ├─ EURUS_WORKSPACE=off or narrow terminal -> tui.fullscreen (classic)
       └─ otherwise -> tui.workspace.shell.run_workspace (advanced)
```

`workspace_mode()` (`EURUS_WORKSPACE=auto|on|off`, threshold 100 columns,
requires a tty) selects between the advanced workspace and the classic
fullscreen interface. The inline (non-tty) path is untouched.

## Module map

```
src/eurus/tui/
    session_ui.py        SessionUI base: modal asks, overlays, composer,
                         transcript view, dispatch, key bindings (shared by
                         classic and workspace UIs)
    fullscreen.py        FullscreenUI(SessionUI): classic single-column layout
    workspace/
        store.py         WorkspaceStore: focus, preset, panel visibility and
                         sizes, change notification (version counter)
        geometry.py      PanelFrame/PanelLayout computation with minimums,
                         clamping, splitter columns, overlap-free allocation
        shell.py         WorkspaceApp(SessionUI): multi-panel Application,
                         panel renderers, keymaps, mouse routing, quick open,
                         palette integration, diff/editor wiring
        views.py         TabRegistry, WorkspaceTab, header/tab-bar/footer
                         renderers, PanelGlyphs (unicode/ascii)
        explorer.py      FileExplorer: lazy tree, expansion, search with time
                         and entry budgets, recent files, dirty marks
        editor_buffer.py EditorBuffer: delta-based edits, cursor, selection,
                         undo/redo (bounded), find/replace
        editor.py        EditorSession: load/save/discard, external-change
                         detection via sha256, conflict resolution
        diffmodel.py     DiffModel/DiffHunk: SequenceMatcher hunks, context,
                         per-hunk decisions, reviewed_text() composition
        diffsession.py   DiffSession: unified/side-by-side state, buffer and
                         file targets, hash-validated apply
        diffview.py      render_diff: unified and side-by-side renderers
        activity.py      ActivityFeed: AgentEvent -> semantic items (thinking,
                         tool cards, file events, approvals, done), nesting,
                         history cap
        preview.py       FilePreview/LoadedFile: safe read, binary and 1 MB
                         guards, line slices
        syntax.py        Pygments adapter with theme-mapped token classes
        fileindex.py     Bounded workspace file index + fuzzy scoring
        completer.py     WorkspaceCompleter: slash + @file completions
        clipboard.py     OSC52 copy with graceful degradation
        mouse.py         Mouse handler factories (click, double-click, wheel,
                         splitter drag)
```

## Separation of concerns

- **Application lifecycle**: `run_workspace` owns attach/teardown and terminal
  restoration; `SessionUI` owns modal interaction state.
- **Layout**: `geometry.py` computes panel frames from the store; prompt_toolkit
  containers consume them as dynamic dimensions.
- **Panels and components**: each panel model is a plain dataclass module with
  no prompt_toolkit imports (`store`, `explorer`, `editor_buffer`, `diffmodel`,
  `activity`, `fileindex`) so they are unit-testable without a terminal.
- **Rendering**: `shell.py` renders panels from models; `views.py` holds shared
  fragment builders; `diffview.py` renders diffs.
- **State**: `WorkspaceStore` is the only mutable UI-wide state; every mutation
  notifies subscribers (the app invalidates).
- **Input**: app-level keys live in `SessionUI._keys` plus workspace additions;
  panel-local keys attach to the focused control; mouse handlers attach to
  fragments.

## Agent integration (adapter only)

`InteractiveSession` gained one optional hook, `activity_sink`, invoked from
`_consume`, `_background_event` and `_approve`. The runtime, gateway, tools,
approval logic and memory are untouched. When no sink is attached (classic UI,
tests) behavior is bit-identical to the baseline. The workspace sets
`session.activity_sink = ui.activity` in `run_workspace`.

File operations reuse the audited security layer:
`eurus.coding.paths.read_text_at` and `atomic_write_at` (traversal rejection,
symlink refusal, 1 MB cap, fsync + atomic rename, mode preservation).

## Key binding model

Bindings are registered so that the LAST eager match wins (prompt_toolkit
semantics). Panel-local `Keys.Any` handlers are registered FIRST so specific
keys override them. ESC is non-eager in panels with `Application.timeoutlen`
lowered to 0.3 s so Alt-prefixed sequences resolve before panel ESC fires.

## Performance design

- Transcript lines are ANSI-parsed once and cached (`parse_ansi_line`,
  LRU 8192).
- All long lists (explorer rows, activity items, editor lines, diff rows,
  transcript) render only the visible slice with selection-driven scroll.
- File index and explorer search carry entry and time budgets.
- Undo is delta-based (bounded at 100 entries), never full-buffer snapshots.

## Themes

The workspace adds the `charcoal` builtin theme matching the design direction
(background #111315, surface #191D20, border #30363B, accent #83A7D8). When the
user has not chosen a theme (active name is `eurus`), the workspace applies
`charcoal`; any explicit user choice is respected. All panel glyphs come from
`PanelGlyphs` with a pure-ASCII fallback set for non-unicode terminals.
