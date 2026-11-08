# EURUS Workspace User Guide

The advanced workspace launches with `eurus tui` whenever the terminal is at
least 100 columns wide and `EURUS_WORKSPACE` is not `off`. Narrow terminals,
Termux phones and `EURUS_WORKSPACE=off` fall back to the classic fullscreen
interface; everything in this guide that concerns chat, slash commands and the
palette applies there too.

## The four panels

- **Explorer** (left): the workspace file tree. Directories expand with Enter
  or Right, collapse with Left. Typing filters the whole tree (budgeted
  search); Backspace edits the filter; Esc clears it or returns to the
  composer. F5 rescans. Files marked `●` have unsaved edits. A `recent`
  section appears at the top while the tree is fully collapsed.
- **Main workspace** (center): tabs for Chat, editors, diffs and previews.
  Alt+Left/Right cycles tabs; the palette's Close Tab closes the active one.
- **Activity** (right): the live agent feed - thinking, tool cards with
  durations and output previews, file reads/edits, approvals, errors and turn
  summaries. Enter expands the selected item's detail; the feed follows the
  newest event until you navigate up.
- **Composer** (bottom): the prompt. Enter sends, Ctrl+J or Alt+Enter inserts
  a newline, trailing backslash continues a line. `@` starts file-reference
  completion; `/` starts command completion. Tab cycles completions.

## Layouts

F8 cycles presets: Focus (chat only), Workspace (all panels), Coding (same
panels, editor-oriented). F2 toggles the explorer, F3 toggles the activity
panel. Alt+Minus / Alt+Equal resize the focused side panel; dragging the
splitters with the mouse also works.

## Editing files

Open a file from the explorer (double-click, or Enter on the selection), from
the palette's Open File, or by typing `@file` in the composer and asking the
agent about it. Editable text files open in the editor:

- The block cursor shows your position; the status line shows path, cursor
  position and MODIFIED/SAVED/CONFLICT state.
- Ctrl+S saves (atomic write, file mode preserved). Ctrl+Z/Ctrl+Y undo/redo.
- Ctrl+F opens the find bar (Enter next match, Tab switches to replace,
  Ctrl+A replaces all, Esc closes).
- Shift+arrows select; Alt+C copies (OSC52 to the system clipboard when the
  terminal supports it, otherwise the session clipboard); Alt+V pastes.
- Alt+D discards all changes after a y/n confirmation.
- If the file changed on disk while editing, saving is blocked and the status
  shows CONFLICT: press `o` to overwrite or `r` to reload.

Binary and >1 MB files open read-only as previews.

## Reviewing changes

With a modified editor open, press G then C (or use the palette's Review
Editor Changes) to open a diff tab comparing the saved file with your buffer.

- `n`/`p` move between hunks, `a` accepts the hunk, `x` rejects it, `A`/`X`
  accept/reject all, `u` toggles unified/side-by-side, `c` collapses context,
  `<`/`>` scroll horizontally, arrows scroll vertically.
- `y` applies the review: accepted hunks stay, rejected hunks revert, and the
  result lands back in the editor buffer. Ctrl+S then persists it.
- File-backed diffs (from agent edits) re-validate the on-disk content hash
  before applying and refuse to write when the file changed underneath.

## Working with the agent

Everything from the classic interface still works: slash commands, the
palette (Ctrl+K), theme switching (Ctrl+T), model switching (Ctrl+P),
sessions, approvals. Approvals now also appear as cards in the activity feed.
The chat transcript, streaming indicator and status footer behave exactly as
before.

## Palette

Ctrl+K opens the palette: type to filter, Up/Down navigate, Enter selects.
Besides the classic entries it lists workspace actions - Open File, panel
toggles, layout presets, Save Editor, Review Editor Changes, Apply Diff
Review and Close Tab.
