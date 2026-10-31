# M1 Implementation Report — Home & Workspace (0.2.0)

Date: 2026-10-31
Scope: M0 (repository audit) and M1 (central HomeManager + WorkspaceManager) of the EURUS Stable 1.0 program.

## What was delivered

### M0 — Repository audit

- Complete call-site and behavior audit recorded in `docs/plans/IMPLEMENTATION_AUDIT.md` (every finding cites file:line).
- Baseline established: 2035 tests collected; mypy strict clean; 3 pre-existing ruff errors and 5 pre-existing unformatted files (unchanged); Docker unavailable in this environment (recorded as risk R-20).
- Risk register: `docs/plans/RISK_REGISTER.md`. Phase plan: `docs/plans/IMPLEMENTATION_PHASES.md`.

### M1 — Home & Workspace

New modules (zero comments, fully typed, mypy-strict clean):

- `src/eurus/home.py` — `HomeManager`, `HomeError`, `resolve_home()`, `home_manager()`. Lazy 0700 directories, permission tightening of existing dirs, `system/version` + `system/manifest.json`, and one-time locked adoption of the legacy database.
- `src/eurus/workspace.py` — `WorkspaceManager`, `WorkspaceError`. Default/project/run/automation/shared/artifact scopes; `resolve` (resolve-containment, symlink-tolerant, matches historical shell semantics) and `resolve_strict` (per-component lstat walk, no symlink components); `validate`/`validate_strict` (absolute containment, hardlink rejection); descriptor-relative `open_read`/`open_write` with `O_NOFOLLOW` walks and `mkdirat`-based intermediate creation; guarded `cleanup`; retention `sweep`.

Migrated call sites:

- `paths.py` now delegates to the home module; `home_dir`/`user_config_path`/`project_config_path` signatures unchanged; added `database_path()` and `home_manager()`; `ensure_private_dir` now tightens existing directories (bootstrap's duplicate implementation delegates to it).
- All 45 `paths.home_dir() / "sessions.sqlite3"` literals replaced with `paths.database_path()` across `agent/`, `cli/`, `tui/`, `gateway/`, `media.py`, `bootstrap/health`.
- Database relocated to `<home>/data/eurus.sqlite3` with WAL-checkpointed, integrity-verified, flock-serialized adoption; legacy file preserved; `data/ADOPTION.json` records the migration. `bootstrap/health/checks.py` resolves the path without side effects (read-only check contract preserved and tested).
- Permission fixes: `media.py` (0700 dirs, 0600 db, WAL via `storage.sqlite.connect`/`managed`), `tui/prefs.py`, `browser/cdp_backend.py` profile dir, `cli/browser.py` login profile.
- Browser workspace leak (audit S-1) fixed: `BrowserManager.set_workspace` + cached-manager rebind in `browser/integration.py`; `gateway/web/runtime.py` passes the configured workspace; downloads now land in the agent workspace instead of process cwd.
- `ToolContext` gained optional `workspaces: WorkspaceManager | None`; `agent/factory.py` wires it for every agent.
- Single-source accessors adopted for skills quarantine, undo dir, evals dir, browser profile, media root.

Tests: 47 new (`tests/test_home.py` 18, `tests/test_workspace.py` 24, `tests/test_browser_workspace.py` 5), including the M1 adversarial scenarios from the program brief (01 agent-cannot-read-~/.ssh, 02-03 eurus-home isolation, 04 traversal, 05 symlink escape, 06 cross-project, 07 cross-run, plus adoption/WAL/concurrency/corruption cases). Two pre-existing tests updated to the canonical database path (`test_operator_controls.py`, `test_verification_wiring.py`); no security check was weakened.

Documentation: `docs/architecture/HOME_AND_WORKSPACE.md` (design + ownership + coverage map), `docs/plans/IMPLEMENTATION_AUDIT.md`, `docs/plans/IMPLEMENTATION_PHASES.md`, `docs/plans/RISK_REGISTER.md`, `docs/ARCHITECTURE.md` updated with the 0.2.0 layout section, legacy-path references corrected in `README`-adjacent docs (`MEMORY.md`, `SECURITY.md`, `docs/MEMORY.md`, `docs/REMINDERS.md`, `docs/WEB_GATEWAY.md`).

## Quality gates (final)

| Gate | Result |
| --- | --- |
| `python -m compileall src/eurus` | OK |
| `pytest` (full suite) | 2081 passed, 1 skipped (baseline 2034 passed + 47 new) |
| `mypy src/eurus` (strict) | clean, 281 files |
| `ruff check src/eurus tests` | 3 errors — all pre-existing baseline findings (`gateway/web/app.py` F401/B008, `media.py` UP017); new code contributes zero |
| `ruff format` | parity with baseline (5 pre-existing files); new code fully formatted |
| Zero-comments policy | verified by token scan on all 6 new files (0 comments, 0 docstring-substitutes) |
| Docker-dependent gates | NOT RUN — no Docker daemon in this environment (risk R-20); M2 will require a real-daemon validation pass |

## Migration behavior

1. First access after upgrade: `database_path()` creates `<home>/data/` (0700), takes an exclusive lock, checkpoints the legacy DB WAL, copies, runs `PRAGMA quick_check` on the copy, atomically renames it to `data/eurus.sqlite3` (0600), writes `data/ADOPTION.json`.
2. Legacy `sessions.sqlite3` is left untouched for rollback to 0.1.x.
3. Corrupt legacy databases raise `HomeError` with the failing step — no silent fallback.
4. `ensure_layout()` tightens 0700 on the home root, `data/`, `system/`, and any known directory that already exists with looser permissions.

## Known residual risks (see RISK_REGISTER.md)

- R-05: a process that upgraded mid-flight and still holds the legacy file open could write to the old file until restart (documented; adoption is designed to run single-process).
- R-12: hardlink aliasing is rejected by strict validation; full containment moves to the M2 container boundary.
- R-13: tests still isolate `EURUS_HOME` per file rather than globally.

## Exact next milestone

M2 — Docker Execution: ExecutionEnvelope, container hardening (non-root user, seccomp, labels, digest pinning, orphan reconciliation), fail-closed Secure Mode, envelope enforcement in `ToolExecutionBoundary`.
