# Home and Workspace Architecture (M1)

Status: implemented as of 0.2.0. This document describes actual behavior, verified by tests in `tests/test_home.py` and `tests/test_workspace.py`.

## 1. Goals

1. One authoritative source for every EURUS home path (`~/.eurus`, overridable with `EURUS_HOME`).
2. A dedicated WorkspaceManager instead of hardcoded workspace paths scattered across tools.
3. Restrictive, validated filesystem permissions.
4. Safe, reversible migration of the runtime database into the target layout.

## 2. HomeManager (`src/eurus/home.py`)

### 2.1 Resolution

- Root resolution order: explicit `root=` argument (tests/embeddings) -> `EURUS_HOME` environment variable (stripped, `expanduser`-ed) -> `Path.home() / ".eurus"`.
- `eurus.paths.home_dir()` delegates to `eurus.home.resolve_home()`, so the legacy function and the manager can never disagree. `eurus.paths` also re-exports `HomeManager` for convenience.
- A process-wide default manager is available via `eurus.home.home_manager()` and `eurus.paths.home_manager()`. Construction performs no I/O; all I/O is explicit.

### 2.2 Canonical locations

Home-relative locations that users already populate keep their existing names (backward compatibility). New target-structure directories are added under `data/` and `system/`.

| Accessor | Path | Notes |
| --- | --- | --- |
| `config_path` | `config.toml` | unchanged |
| `database_path()` | `data/eurus.sqlite3` | migrated from legacy `sessions.sqlite3` (section 4) |
| `data_dir` / `backups_dir` | `data/`, `data/backups/` | backups populated by later milestones |
| `secrets_dir` | `secrets/` | 0700, created lazily (secret broker lands in M4) |
| `skills_dir` / `skills_quarantine_dir` / `skills_manifests_dir` | `skills/`, `skills/quarantine/`, `skills/manifests/` | `skills/` remains the installed-root used by `skills/loader.py` |
| `browser_profile_dir` / `browser_downloads_dir` / `browser_cache_dir` | `browser/profile`, `browser/downloads`, `browser/cache` | CDP profile default |
| `media_dir` | `media/` | MediaStore root |
| `logs_dir` | `logs/` | lazy; default config still writes no log file |
| `undo_dir` | `undo/` | CheckpointStore |
| `evals_dir` | `evals/` | learning eval results |
| `themes_dir`, `bootstrap_dir`, `auth_dir`, `security_dir`, `workspace_dir` | `themes/`, `bootstrap/`, `auth/`, `security/`, `workspace/` | |
| `mcp_config_path`, `gateway_credentials_path`, `env_file`, `ui_prefs_path`, `tui_config_path`, `prices_path` | root files | unchanged names |
| `system_dir`, `version_file`, `manifest_path` | `system/`, `system/version`, `system/manifest.json` | runtime-owned, never agent-accessible |

The `memory/` and `automation/` trees from the target layout are **not created** in M1: memory currently persists in the SQLite database (tables `memories`, `memories_fts`) and automation state in `eurus_jobs`/`eurus_schedules`. Creating unused directories would violate the lazy-creation rule. They are introduced by M5/M6 when real data exists.

### 2.3 Directory creation and permission enforcement

- `ensure_dir(path, mode=0o700)`: creates with `mkdir(parents=True, mode=...)`, then **validates and tightens existing permissions** (`chmod` to the target mode when group/other bits are set). Loosening never happens.
- `ensure_layout()`: creates the home root, `data/`, and `system/` (all 0700), writes `system/version` and `system/manifest.json` (`{"version", "created_at", "home_layout"}`), and tightens permissions of the known directories that already exist. Entry points call this at startup; per-accessor calls (`database_path` etc.) only ensure their own directory.
- Failures surface as `HomeError` (a `EurusError` subclass). There are no silent fallbacks: if the home cannot be created or secured, the operation raises.

## 3. WorkspaceManager (`src/eurus/workspace.py`)

### 3.1 Scopes

| Scope | Location | Lifetime |
| --- | --- | --- |
| `default_workspace()` | the configured root (`config.permissions.workspace`, resolved) | user-managed, never cleaned by EURUS |
| `project_workspace(project_id)` | `<home>/workspace/projects/<project_id>` | explicit create/cleanup |
| `run_workspace(run_id)` | `<home>/workspace/temp/runs/<run_id>` | ephemeral, swept by retention |
| `automation_workspace(execution_id)` | `<home>/workspace/automation/<execution_id>` | cleaned after automation runs |
| `shared_workspace()` | `<home>/workspace/shared` | explicit sharing only |
| `artifacts_dir()` | `<home>/workspace/artifacts` | automation artifacts |

Scope identifiers must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (no leading dot, so `..` and hidden traversal names are impossible). `WorkspaceError` is raised otherwise.

The user's default workspace stays where it is configured today. Only managed scopes live under `<home>/workspace/`; existing repositories are never moved (project registration and git worktrees arrive with later milestones).

### 3.2 Path validation

Two resolution strategies exist because they serve different threat models:

- `resolve(relative)` — containment check on the fully resolved path (matches the historical shell-tool semantics: symlinked components are followed, then the result must remain inside the root). Used for user-facing convenience paths where symlinks are legitimate user data (e.g. `node_modules` junctions).
- `resolve_strict(relative)` — per-component walk with `os.lstat`; **any** symlink component (including the final one) raises `WorkspaceError`. Used for security-sensitive paths. Modeled on `coding/paths.py::resolve_workspace_path`.
- `validate(path)` — an absolute or relative candidate must resolve inside the root; absolute escapes are rejected.
- All resolvers reject: absolute candidates passed to `resolve*`, `..` components, NUL bytes, and empty components other than `.`.
- `validate_strict` applies full per-component symlink rejection to relative paths; absolute candidates are resolve-containment checked with hardlink detection on the final component (the strict walk for absolute paths operates on already-resolved components).

### 3.3 TOCTOU mitigation

Path-based validation alone is racy. `open_read(relative)` and `open_write(relative, *, exclusive=False, mode=0o600)` perform a descriptor-relative walk: each directory component is opened `O_NOFOLLOW|O_DIRECTORY` and the final component is opened `O_NOFOLLOW` with `dir_fd`, so a component swapped to a symlink between validation and use cannot redirect the operation. `open_write` defaults to `O_CREAT|O_EXCL` (idempotent writes must delete first through the manager). These helpers are the required primitives for future tools that write untrusted paths; existing tools migrate onto them as they are touched.

Known limitation (documented, not hidden): hardlinked files inside the workspace that alias files outside it cannot be distinguished by path inspection. The manager rejects `st_nlink > 1` regular files in `validate_strict`, and `open_*` primitives prevent symlink races; host-level protection for hardlinks comes with the M2 container boundary.

### 3.4 Lifecycle

- `create(scope_path)` — ensures the directory (0700) and returns it.
- `cleanup(workspace_id)` — removes a managed scope tree after verifying the real path is still the expected directory directly under its scope parent (renames or symlink swaps abort the cleanup).
- `sweep(max_age_seconds, now=None)` — retention for `temp/runs/` and `workspace/automation/`; returns the number of trees removed. The default workspace and `projects/` are never swept.

### 3.5 Integration points (M1)

- `agent/factory.py` builds `WorkspaceManager(root)` and attaches it to `ToolContext.workspaces` (new optional field in `tools/base.py`); existing `ToolContext` constructions remain valid. `open_write` creates missing intermediate directories (0700) with `mkdirat` under the walked descriptor, so no symlink can be swapped into the path mid-creation.
- `browser/manager.py` gains `set_workspace()`; `browser/integration.py` rebinds the cached manager's workspace instead of silently ignoring it, fixing the cwd-download leak (audit S-1).
- `gateway/web/runtime.py` passes the configured workspace into browser tool setup.
- `tools/shell.py` keeps its resolve-based containment (symlink-tolerant) but now sources the root from the same `ToolContext.workspace` produced by the factory's WorkspaceManager; strict resolution is available to tools via `context.workspaces`.

## 4. Database migration design

Legacy layout: `<home>/sessions.sqlite3` (+ `-wal`/`-shm`). Target: `<home>/data/eurus.sqlite3`.

`HomeManager.database_path()` performs a one-time, locked adoption:

1. If the target exists, return it (fast path: two stats).
2. If no legacy file exists, return the target (fresh install).
3. Take an exclusive `flock` on `<home>/data/.adopt.lock` (concurrent processes serialize).
4. Re-check the target under the lock (double-check).
5. Open the legacy database, run `PRAGMA wal_checkpoint(TRUNCATE)`, close it — the file content is now complete on disk without copying a live WAL.
6. Copy `sessions.sqlite3` to `data/.eurus.sqlite3.adopt-<pid>.tmp`, run `PRAGMA quick_check` on the copy, `chmod 0600`, `fsync`, then `os.replace` into place (atomic).
7. Write `data/ADOPTION.json` (`{"migrated_from", "adopted_at", "version"}`).
8. Return the target.

Properties:

- The legacy file is **never deleted or modified beyond the WAL checkpoint**, so downgrading to 0.1.x keeps working against the old file (documented split-brain risk in `RISK_REGISTER.md`).
- Adoption failure raises `HomeError` with the failing step; no silent fallback to the legacy path.
- All 45 former literal call sites now resolve through `database_path()`, so no process can straddle both files.

`ScheduleStore`, gateway, reminder, plugin and other stores that build their own connections continue to point at the same adopted file through the same accessor.

## 5. Directory ownership model

| Directory | Owner | Agent access |
| --- | --- | --- |
| `system/` | runtime | none (never mounted into containers; no tool exposes it) |
| `data/` | storage layer | through store APIs only |
| `secrets/` | secret manager (M4) | none today |
| `skills/` | skill manager/loader | read via loader; quarantine writes only through `SkillDraftStore` |
| `browser/` | browser backends | through browser tools |
| `media/`, `logs/`, `undo/`, `evals/`, `themes/`, `bootstrap/`, `auth/`, `security/` | owning subsystems | through their APIs |
| `workspace/` | WorkspaceManager | scoped read/write through manager validation |

The home root is 0700; only explicit `EURUS_HOME` sharing can widen it, and `ensure_dir` re-tightens drifted directories on every layout pass. Container mounts in M2 will expose workspace scopes only — never the home root.

## 6. Test coverage map

| Requirement | Test |
| --- | --- |
| `EURUS_HOME` override and default resolution | `tests/test_home.py::test_resolve_home_prefers_eurus_home_override`, `test_resolve_home_defaults_to_user_home`, `test_paths_home_dir_and_manager_agree` |
| Lazy creation, 0700 tightening of pre-existing open dirs | `tests/test_home.py::test_ensure_layout_creates_required_directories`, `test_ensure_layout_tightens_pre_existing_open_directories`, `test_ensure_layout_leaves_missing_optional_directories_absent` |
| Manifest/version writing | `tests/test_home.py::test_manifest_records_version_and_layout`, `test_manifest_preserves_created_at` |
| Legacy DB adoption (plain, with WAL sidecars, concurrent, corrupt-legacy failure) | `tests/test_home.py::test_database_adoption_*`, `test_concurrent_adoption_serializes_on_one_file`, `test_paths_database_path_matches_manager` |
| Traversal / absolute / NUL rejection | `tests/test_workspace.py::test_resolve_rejects_traversal_absolute_and_nul`, `test_open_rejects_traversal_and_root` |
| Symlink escape (root, intermediate, final component) | `tests/test_workspace.py::test_resolve_follows_symlinks_but_rejects_escape`, `test_resolve_strict_rejects_symlink_components`, `test_open_write_refuses_symlink_final_component` |
| Cross-scope access rejection and ID validation | `tests/test_workspace.py::test_scope_id_validation`, `test_scenario_cross_project_access_is_rejected`, `test_scenario_cross_run_access_is_rejected`, `test_scenario_agent_cannot_reach_eurus_home`, `test_scenario_agent_cannot_read_user_ssh` |
| fd-based open blocks symlink swap | `tests/test_workspace.py::test_open_write_refuses_symlink_final_component` |
| Hardlink strict rejection | `tests/test_workspace.py::test_validate_strict_rejects_hardlinks`, `test_open_write_non_exclusive_refuses_hardlink` |
| Cleanup safety and retention sweep | `tests/test_workspace.py::test_cleanup_*`, `test_sweep_*`, `test_scenario_cleanup_is_scoped` |
| Browser download root follows rebound workspace | `tests/test_browser_workspace.py` |
| ToolContext accepts WorkspaceManager (factory wiring) | `tests/test_home.py::test_tool_context_accepts_workspace_manager` |
