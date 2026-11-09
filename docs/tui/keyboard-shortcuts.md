# EURUS Workspace Keyboard Shortcuts

Keys are listed per context. Global keys work anywhere; composer keys apply
when the prompt has focus; panel keys apply when that panel is focused.

## Global

| Key | Action |
| --- | --- |
| Ctrl+K | Open the command palette |
| Ctrl+N | New session (`/clear`) |
| Ctrl+P | Switch model (`/model`) |
| Ctrl+W | Workspaces (`/workspace`) |
| Ctrl+O | Settings |
| Ctrl+T | Theme picker |
| F1 | Help (`/help`) |
| Alt+A | Attach overlay (insert `@path`) |
| Alt+1 | Focus the composer |
| Alt+2 | Focus the file explorer |
| Alt+3 | Focus the main workspace |
| Alt+4 | Focus the activity feed |
| F2 | Toggle the explorer panel |
| F3 | Toggle the activity panel |
| F8 | Cycle layout preset (focus/workspace/coding) |
| Alt+Left / Alt+Right | Previous / next workspace tab |
| Alt+Minus / Alt+Equal | Shrink / grow the focused side panel |
| Ctrl+C | Cancel the running task; twice to exit |
| Ctrl+D | Exit (empty composer) |
| PageUp / PageDown | Scroll the chat transcript |
| Ctrl+Up / Ctrl+Down | Fine scroll the chat transcript |

## Composer

| Key | Action |
| --- | --- |
| Enter | Send (or continue a trailing `\`) |
| Ctrl+J / Alt+Enter | Insert a newline |
| Tab | Cycle completions (`/` commands, `@` files) |
| Esc | Cancel an ask/confirm prompt |

## Explorer panel

| Key | Action |
| --- | --- |
| Up / Down | Move the selection |
| Right / Enter | Expand a directory or open a file |
| Left | Collapse a directory or jump to its parent |
| Home / End | First / last row |
| Backspace | Edit the filter |
| Printable keys | Incremental tree filter |
| F5 | Rescan the workspace |
| Esc | Clear the filter, then return to the composer |

## Editor tab (main workspace focused)

| Key | Action |
| --- | --- |
| Arrows / Home / End | Move the cursor (Shift+arrows select) |
| PageUp / PageDown | Page the cursor |
| Enter | Split the line |
| Backspace / Delete | Delete (selection first) |
| Tab | Insert four spaces |
| Printable keys | Insert text |
| Ctrl+S | Save (atomic) |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+F | Find in file |
| Ctrl+H | Find and replace |
| Alt+C | Copy selection (or line) to the clipboard |
| Alt+V | Paste from the clipboard |
| Alt+D | Discard all changes (y/n confirm) |
| G then C | Open the diff of unsaved changes |
| `o` / `r` | During a save conflict: overwrite / reload |
| `y` / `n` | During discard confirm: discard / cancel |

## Find/replace bar

| Key | Action |
| --- | --- |
| Enter | Next match (or replace next in the replace field) |
| Tab | Switch between find and replace fields |
| Ctrl+A | Replace all |
| Esc | Close the bar |

## Diff tab (main workspace focused)

| Key | Action |
| --- | --- |
| `n` / `p` | Next / previous hunk |
| `a` / `x` | Accept / reject the current hunk |
| `A` / `X` | Accept / reject all hunks |
| `u` | Toggle unified / side-by-side view |
| `c` | Collapse / expand context lines |
| `y` | Apply the review to the buffer or file |
| `<` / `>` | Scroll horizontally |
| Up / Down | Scroll vertically |

## Activity panel

| Key | Action |
| --- | --- |
| Up / Down | Move the selection (stops following) |
| Enter | Expand or collapse the item detail |
| Home / End | Jump to the oldest / newest event |
| Esc | Return to the composer |

## Mouse

Click a panel to focus it, a tab to switch, a file row to select it (click
again to open), a directory row to expand it. The wheel scrolls the panel
under the pointer. Drag a splitter to resize the adjacent panel. Double-click
a file in the explorer to open it.
