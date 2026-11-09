# APInex CLI Reference Analysis

Analysis of `cli/src/index.mjs` (4867 lines) and `cli/src/security.mjs` as a
behavioral reference for the EURUS workspace. The JavaScript implementation
is deliberately NOT ported; the behaviors below are reimplemented natively
in Python on top of EURUS primitives.

## 1. Diff engine (`computeLineDiff`, lines 560-611)

Algorithm:

1. Split old/new content into lines.
2. Advance `start` while leading lines are equal (common prefix).
3. Retreat `endA`/`endB` while trailing lines are equal (common suffix).
4. If both middles are empty, everything matched.
5. Guard: if `midA.length * midB.length > 2_000_000`, fall back to
   delete-all + add-all (prevents quadratic blowup on huge disjoint files).
6. Otherwise run a Longest Common Subsequence dynamic program over the two
   middles and backtrack into `same`/`add`/`del` entries.

Behavior to adopt in EURUS:

- Prefix/suffix trimming before the quadratic core.
- Cell-count guard with wholesale add/del fallback.
- Output as ordered `same|add|del` line records - directly maps onto
  EURUS `difflib.SequenceMatcher` opcodes, which EURUS already depends on
  through the standard library. EURUS will use `SequenceMatcher`
  (with autojunk disabled for code) instead of hand-rolled LCS.

## 2. Diff rendering (`renderDiff`, lines 2239-2340)

- Per-tool cards: `write_file` (path, +N lines, 5-line preview with line
  numbers, "... (N more lines)"), `edit_file` (path, +A/-D stats, changed
  regions with 1 leading + 1 trailing context line, red/green background
  rows), `bash` (command, up to 8 output lines), `read_file` (path, line
  count), `grep_search` (query, match count, preview), `find_files`,
  `web_search`, `web_fetch`.
- Context policy: keep one same-line before and after each change cluster;
  if nothing changed structurally, render the first 10 diff rows.
- Rows are padded to the panel width so highlight backgrounds are solid.

EURUS equivalents: a `DiffModel` producing hunks with configurable context,
plus `render_diff` producing unified and side-by-side views. Tool cards
become activity-feed entries in the Agent Activity panel.

## 3. `@` file reference picker

- Typing `@` in the composer opens an inline list above the input.
- Filtering by substring over a workspace file list.
- Keys: Up/Down navigate, Tab or Enter insert `@path`, Esc cancels.
- Footer shows "Insert file reference (up/down navigate - Tab/Enter insert -
  Esc cancel)" plus "N more above/below" scroll indicators.

EURUS will implement this as a prompt_toolkit `Completer` on the existing
composer buffer (native completions menu, no custom footer state machine)
backed by a cached workspace file index with fuzzy matching.

## 4. Keyboard input handling

- Raw stdin in raw mode; manual escape-sequence dispatch.
- Modes: `chat`, `model-picker`, `streaming`, `permission-prompt`,
  `session-picker`, `topup-picker`, `subscription-picker`, `rpm-picker`.
- Word deletion via Alt+Backspace / Ctrl+W / Option+Delete.
- Bracketed paste handling with newline flattening and image-path paste
  detection.

EURUS equivalent: prompt_toolkit already provides emacs editing, bracketed
paste, and unicode input; EURUS adds workspace modes through focus targets
instead of modal strings.

## 5. Mouse interaction (SGR protocol, lines 3998-4060)

- Regex `ESC[<b;c;xM` parses SGR mouse packets: press/drag/release.
- Drag over rendered rows builds a selection; release copies the selected
  plain text to the clipboard (pbcopy / clip / wl-copy / xclip).
- Non-drag releases are click handling; all mouse bytes are stripped from
  the input stream so no leakage into the prompt.

EURUS equivalent: prompt_toolkit mouse support (SGR + X10) with click
routing to panels (focus, file selection, tab switching, splitter drag),
wheel scrolling, and defensive cleanup on exit.

## 6. Permission guard

- Destructive command patterns intercepted (rm, git reset/push --force,
  kill, drop table, ...).
- Interactive `[y]es / [n]o / [a]lways` confirmation while the screen
  shows the pending command context.

EURUS already has a stronger system (`SessionScopedApprover`,
`ApprovalRequest` with risk levels, grants, `/grants`); the workspace only
needs an approval card in the activity feed plus the existing confirm
flow - no reimplementation.

## 7. Terminal lifecycle

- Alternate screen buffer entry/exit; cursor restore; clean exit even on
  exceptions.

EURUS equivalent: prompt_toolkit Application manages this; EURUS adds
`Recolor` restore and `finally`-based cleanup in `run_fullscreen`, which is
preserved.

## 8. What EURUS will NOT take from the reference

- The 4867-line single-file architecture (EURUS keeps modular packages).
- Manual ANSI rendering (EURUS uses prompt_toolkit + Rich).
- Node-specific clipboard commands (EURUS ships a best-effort clipboard
  helper with graceful failure, per Phase 8 requirements).
- APInex visual styling (EURUS keeps its own theme identity; the charcoal
  palette from the design direction is applied through the existing Theme
  pipeline as a new builtin theme).

## 9. Security observations (`security.mjs`)

- Path safety: workspace-rooted resolution, rejects `..` and absolute paths
  (EURUS: `resolve_workspace_path` already stricter, also rejects symlinks).
- Sensitive-file detection for prompts (`.env`, keys) - EURUS redaction
  (`eurus.security.redaction`) already covers argument redaction.
- Command allow/deny lists for the permission guard - EURUS approval risk
  levels serve the same purpose.
