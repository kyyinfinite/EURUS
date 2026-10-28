# Multi-File Workflow

The multi-file workflow adds safe, reviewable multi-file editing on top of the
single-file `apply_patch` boundary.

## Components

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

## Security rules

- Patchset apply is a mutating action and requires owner approval.
- Files outside the workspace, symbolic links, and unreadable binary files are
  rejected.
- The freshness hash prevents applying a review of stale content.
- The validation runner only runs the configured allowlist; it is not a shell.

## Verification commands

```bash
eurus eval --task rename_across_files      # model-driven end-to-end check
python -m pytest tests/test_multi_file_workflow.py tests/test_patch_loop.py
```

Both suites exercise preview, apply, drift detection, rollback, and the
validation runner without touching files outside a temporary workspace.
