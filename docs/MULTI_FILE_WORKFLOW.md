# Multi-File Workflow

The multi-file workflow adds safe, reviewable multi-file editing on top of the
single-file `apply_patch` boundary.

## Components

- **Shared path safety** (`src/eurus/coding/paths.py`) — one resolver enforces the
  workspace rules for every file tool: relative paths only, no `..` traversal, no
  symbolic links, and strict containment inside the workspace. The same module
  provides one atomic-write helper (temp file in the target directory plus
  `os.replace`, preserving the existing file mode) used by `apply_patch`,
  `edit_file`, `create_file`, the document tools, and patchsets.
- **Project index** (`src/eurus/coding/project_index.py`) — a lightweight Python
  project index (modules, imports, symbols) rendered into the agent's context so
  edits respect the real module structure.
- **Patchsets** (`src/eurus/coding/patchset.py`) — a set of full-file changes,
  each carrying the SHA-256 of the file content that was reviewed.
- **Preview** — `preview_patchset` renders a unified diff of the whole patchset
  without writing anything.
- **Apply** — `apply_patchset` writes every file only if its current content
  still matches the reviewed hash. Any drift aborts the whole patchset.
- **Guarded restore** — restore points capture file states before an apply and
  can roll back an approved patchset when a validation run fails.
- **Validation runner** (`src/eurus/coding/checks.py`) — `run_checks` executes an
  allowlisted set of check commands (pytest, mypy configuration, compile checks)
  inside the workspace and reports structured results.

## File tools

- `read_file` accepts optional `start_line` and `end_line` (1-based, inclusive)
  so large files can be read in slices. Every reply reports the total line count
  and marks truncated output with an explicit marker. The 1 MB cap and symlink
  rejection still apply.
- `edit_file` replaces an exact `old_string` with `new_string`. It fails when
  `old_string` is missing or matches more than once unless `replace_all` is
  true, and when both strings are identical. An optional `base_sha256` protects
  against stale writes. Mutating, approval-gated, `path_keys=("path",)`.
- `create_file` writes new files and creates parent directories inside the
  workspace only. It refuses to overwrite an existing file unless `overwrite`
  is true, and rejects symbolic links and paths outside the workspace. The 1 MB
  cap applies. Mutating, approval-gated, `path_keys=("path",)`.
- `apply_patch` keeps its `base_sha256`/`expected_content` freshness gate and
  now also declares `path_keys=("path",)` so concurrent patches to the same file
  serialize on the boundary lock.

## Security rules

- Every mutating file tool (`apply_patch`, `edit_file`, `create_file`,
  `document_create`, `document_replace`, `apply_patchset`) is marked
  `mutates=True`, `requires_approval=True`, and carries `path_keys` so the
  boundary serializes operations on the same target path.
- Files outside the workspace, symbolic links, and unreadable binary files are
  rejected by the shared resolver before any write happens.
- The freshness hash prevents applying a review of stale content.
- The validation runner only runs the configured allowlist; it is not a shell.

## Verification commands

```bash
eurus eval --task rename_across_files      # model-driven end-to-end check
python -m pytest tests/test_multi_file_workflow.py tests/test_patch_loop.py
```

Both suites exercise preview, apply, drift detection, rollback, and the
validation runner without touching files outside a temporary workspace.
