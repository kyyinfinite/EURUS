# EURUS Implementation Audit (M0)

Date: 2026-10-31
Baseline version: 0.1.0 (src/eurus/__init__.py)
Scope: full source inspection of `src/eurus`, `tests`, `docs`, `pyproject.toml`.
Every finding below cites the inspected file and line. No finding is speculative.

## 1. Environment and tool baseline

| Check | Result |
| --- | --- |
| Python | 3.12.16 (pyproject requires >= 3.11) |
| pytest collection | 2035 tests |
| pytest run | 2034 passed, 1 skipped, 0 failed (after installing optional `aiogram` dev extra; without it `tests/test_bootstrap_integrations.py::test_telegram_check_result_with_token` fails because `inspect_telegram` reports `aiogram` MISSING, `src/eurus/bootstrap/integrations/telegram.py:56-59`) |
| ruff | 3 pre-existing errors (unused `fastapi.Depends` import and quote-style issues in `src/eurus/gateway/web/app.py`), 5 files unformatted |
| mypy (strict, files=src) | clean: "no issues found in 279 source files" |
| docker | NOT available in this environment; Docker-dependent behavior is testable only via mocks |
| Existing git history | present, branch `main` |

## 2. Current architecture (verified)

### 2.1 Composition

- CLI entry `eurus` -> `src/eurus/cli` (click). TUI entry `eurus-tui` -> `src/eurus/tui`.
- `src/eurus/agent/factory.py::build_agent` (L33-174) wires: workspace root resolution (L51), `ToolContext` (L52-56), `default_registry` (L58-67), browser tools (L68-70), sandbox selection host/container (L71-85), skill/delegation/team tools (L86-93), `LessonStore`/`PromptMemory`/`MemoryManager` on `paths.home_dir() / "sessions.sqlite3"` (L96, L137, L140), verification and planning services (L155-173).
- `src/eurus/agent/loop.py::Agent` executes the tool loop through `ToolExecutionBoundary` (`src/eurus/tools/boundary.py`).
- Runtime stores: `src/eurus/storage/database.py` (pooled `Database`), `runs.py`, `events.py`, `jobs.py`, `tool_attempts.py`, `migrations.py` (LATEST_SCHEMA_VERSION = 5).
- Scheduling: `src/eurus/scheduler.py::ScheduleStore` (kinds: `once`, `interval` only) and `src/eurus/automation/worker.py::AutomationWorkerPool` (asyncio workers over `JobStore` leases).
- Gateway: `src/eurus/gateway/web` (FastAPI) and `src/eurus/gateway/telegram` (aiogram).
- Bootstrap subsystem: `src/eurus/bootstrap/` (doctor/wizard/startup integration, its own state store under `<home>/bootstrap`).

### 2.2 Path resolution today (the core M1 problem)

`src/eurus/paths.py` is 34 lines: `home_dir()` honors `EURUS_HOME` (L12-17), `user_config_path()` (L20-21), `project_config_path()` (L24-29), `ensure_private_dir()` (L32-34, `mkdir(mode=0o700)` only, **no chmod of pre-existing dirs**).

There is no central home object. Call sites append literal subpaths to `paths.home_dir()`. Verified inventory:

| Subsystem | Literal | Sites |
| --- | --- | --- |
| Runtime/session DB | `sessions.sqlite3` | 45 across 16 files: `agent/factory.py:96,137,140`, `agent/capability_tools.py:19`, `cli/common.py:65,77`, `cli/run.py:97`, `cli/evals.py:250`, `cli/schedule.py` (8), `cli/gateway.py` (5), `cli/memory.py` (9), `cli/observability.py:29`, `cli/sessions.py` (5), `gateway/web/app.py:420`, `gateway/web/runtime.py:55,79`, `gateway/web/service.py:454`, `media.py:214`, `tui/session.py:105,106,938`, `bootstrap/health/checks.py:237` |
| MCP config | `mcp.json` | `mcp/runtime.py:30,51`, `cli/integrations.py` (5), `bootstrap/integrations/mcp.py:60` |
| MCP policy | `security/mcp-policies.json` | `mcp/policy.py:260-261` |
| Server access | `security/server-access.json` | `mcp/server/access.py:105-106` |
| Google auth | `auth/google/client.json`, `auth/google/accounts.json`, `auth/google/tokens`, `integrations/google-workspace.json` | `mcp/auth/credentials.py:38,42`, `mcp/providers/google_workspace/provider.py:174,178`, `mcp/providers/google_workspace/services.py:99` |
| Browser CDP profile | `browser/profile` | `browser/cdp_backend.py:349-358` (mkdir without mode), `cli/browser.py:74,145-148`, `bootstrap/integrations/browser.py:126` |
| Browser downloads | `workspace/"downloads"` or configured | `browser/manager.py:484-491` |
| Media | `media/` | `media.py:214-216` (mkdir **without mode**; raw `sqlite3.connect` at L220 **bypasses** `storage.sqlite.configure` WAL setup) |
| Skills | `skills/` (user root), `skills/quarantine` (drafts) | `skills/loader.py:57-65`, `learning/skills.py:77,83`, `skills/tools.py:34` |
| Undo/checkpoints | `undo/` | `coding/checkpoint.py:49-50` |
| UI prefs | `ui.json`, `tui.toml`, `themes/` | `tui/prefs.py:38-42` (mkdir no mode), `tui/tui_config.py:33-52`, `tui/themes.py:201,354-396` |
| Gateway credential | `gateway-web.json` | `gateway/web/auth.py:32,42` (correct 0700/0600) |
| Env file | `.env` | `cli/base.py:74-75`, `bootstrap/configuration/env_manager.py:172` |
| Prices | `prices.toml` | `runtime/pricing.py:128-141` |
| Bootstrap state | `bootstrap/` + `bootstrap/backups/` | `bootstrap/state/store.py:26-48` |
| Evals | `evals/learning-evals.sqlite3` | `cli/evals.py:120` |
| Logs | only if `logging.file` configured | `logs.py:117-128` (parent mkdir **without mode**); default config ships no log file |

Additional resolution inconsistencies (verified):

1. Direct `Path.home()` that ignores `EURUS_HOME`: `browser/backend.py:267` (Playwright cache probe), `bootstrap/integrations/browser.py:16`, `bootstrap/detection/capabilities.py:90`, `bootstrap/startup/systemd.py:15`, `bootstrap/startup/manager.py:148,159`, `bootstrap/startup/termux.py:50,78` (recomputes `home/".eurus"` instead of calling `paths.home_dir`), `bootstrap/startup/proot.py:96`, `bootstrap/detection/platform.py:95`, `bootstrap/detection/architecture.py:43`, `tui/layout.py:32` (display only). `tui/fonts.py:110-141` uses the OS home deliberately (system font directories).
2. Two competing `ensure_private_dir` implementations: `paths.py:32` (no chmod of existing) vs `bootstrap/configuration/env_manager.py:29-33` (mkdir + chmod 0700). Bootstrap modules import the latter; `learning/skills.py:11` imports the former.
3. Browser manager workspace divergence: `browser/manager.py:471` defaults `workspace=None` -> `Path.cwd()`. `browser/integration.py:44-46` returns a cached manager and **silently ignores a later `workspace=` argument**. `agent/factory.py:70` passes the workspace, but `tools/builtin.py:173-176` has already created the manager with `workspace=None`, so agent runs get `Path.cwd()`. `gateway/web/runtime.py:77` and `cli/integrations.py:41` also pass no workspace. Net effect: browser downloads default to the **process cwd**, not the agent workspace.
4. Playwright vs CDP profile divergence: `browser/playwright_backend.py:121-128` returns `None` (ephemeral profile) when unconfigured; CDP persists under `<home>/browser/profile`.
5. `mcp/server/tools.py:32` builds `ToolContext(workspace=Path.cwd())` for server mode.
6. `media.py:220` opens SQLite without WAL/busy-timeout configuration used everywhere else.
7. Many independent `Database`/store instances over the same `sessions.sqlite3` per process (e.g. `tui/session.py:105,106,938`; `gateway/web/app.py:420-423` builds 3). WAL mitigates; consolidation is a later objective.
8. `tests/conftest.py` has **no** autouse `EURUS_HOME` isolation; tests isolate via per-file `monkeypatch.setenv("EURUS_HOME", str(tmp_path))` or `CliRunner(env=...)`. Any helper resolving `paths.home_dir()` without isolation touches the real home.

### 2.3 Execution infrastructure

- `Sandbox` protocol: `execution/sandbox.py:34-37`, `ExecutionRequest` (frozen dataclass, L17-23) with sandbox-virtual `cwd="/workspace"` and `network_enabled` flag; `ExecutionResult` (L26-31).
- `ContainerSandbox` (L53-165): docker argv built as list, run via `asyncio.create_subprocess_exec`; flags verified present: `--rm`, `--network none` (bridge only if both `sandbox.network` AND `request.network_enabled`), `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, `--tmpfs /tmp:rw,noexec,nosuid,size=64m`, single RW bind mount of workspace, no socket mount, no privileged, no host PID/network namespaces. Timeout clamped to 3600s with cidfile-based kill on timeout/cancel.
- Verified gaps vs M2 requirements: **no `--user`** (image default = root for `python:3.12-alpine`), no seccomp/AppArmor profile, no image digest pinning or allowlist (`config.py:808,819-820` accepts any non-empty tag), no container labels carrying run identity, no orphan-container reconciliation, no rootless-docker detection.
- `HostSandbox` (`execution/host.py`): env allowlist scrub (`SAFE_ENV` L15-25) is the only restriction; no filesystem isolation; reachable through `config.sandbox.backend = "host"` with `required = false` (Termux path, `runtime_hint()` L40-50).
- `tools/shell.py:19-34`: relative-cwd validation, resolve-based containment, `/bin/sh -lc` inside container. `network_enabled` is never set on shell requests, so `sandbox.network = true` is currently dead for shell (fail-closed, but undocumented).
- `ToolExecutionBoundary` (`tools/boundary.py:80-607`): schema validation, risk classification, fail-closed approval gate (L310-362), write-ahead `AttemptStarted` for mutating tools, per-path mutation locks from `path_keys` (L181-195), uncertain-outcome reconciliation (L287-300), redacted error detail (L505,525). **It does not validate filesystem paths**; validation lives in individual tools (`tools/shell.py:20-24`, `coding/paths.py:13-30` with per-component symlink rejection, `browser/tools.py:278-298,379-393`).

### 2.4 Security subsystem

- `security/net_guard.py`: private/loopback/link-local/metadata blocking via `is_global` checks (L36-85), DNS resolution validation of **all** returned addresses, manual redirect revalidation (`MAX_REDIRECTS = 6`, callers enforce). `security/http_adapter.py` re-validates at connect time and pins validated IPs (DNS rebinding protection), Unix sockets always refused. Enforcement is **opt-in per client** built through `public_http_client()`; no system-wide egress policy exists yet.
- `browser/security.py`: URL syntax, scheme, host blocklists, metadata endpoints, DNS cache with single-flight. Verified hole: **explicitly allowlisted hosts short-circuit both literal-IP and DNS validation** (`security.py:267-268,278-279`), so an allowlisted domain resolving to a private/metadata IP is permitted.
- `security/approval.py`: `action_fingerprint` = SHA-256 of canonical `{name, arguments}` (L28-36). `SessionScopedApprover` (L87-150) reuses grants by fingerprint for a fixed allowlist of non-shell tools with TTL 3600s; `NEVER_SESSION_SCOPED` (L50-66) protects shell/browser/delegation/memory mutators.
- `gateway/approval.py::ApprovalBroker`: one-time tokens (token_hash PK), fingerprint persisted and displayed but decisions bound to the awaited call instance; fail-closed on timeout/persistence failure; lockout after 5 failed attempts.
- `security/journal.py::AuditJournal`: JSONL with `fsync`, file 0600; no rotation, no tamper protection.
- `security/redaction.py`: pattern + key-name based secret scrubbing, applied to boundary error details and approval summaries.

### 2.5 Storage and automation

- Single SQLite file `sessions.sqlite3` (WAL, `busy_timeout=10000`, `synchronous=NORMAL`); `Database` enforces 0700 parent / 0600 file (`storage/database.py:45-50`) and runs migrations in its constructor (L53-54). Versioned migrations v1-v5 in `storage/migrations.py` with forward-compat guard (L126-127). **No backup/restore, no wal_checkpoint anywhere.**
- Schedule tables (`eurus_schedules`, `eurus_schedule_runs`, `eurus_task_profiles`) are created ad hoc outside the versioned migration system (`scheduler.py:98-123`).
- `JobStore` (`storage/jobs.py`): leases with `lease_version` fencing (claim L161-200, heartbeat L202-220, finish L231-254), `side_effect_started` boundary, `recover_expired()` three-way split (L283-308) with **no backoff/jitter**, retries only via lease expiry, `max_attempts=1` hardcoded in `dispatch_due` (`scheduler.py:362`), no dead-letter table, no general idempotency key (only `UNIQUE(schedule_id, scheduled_at)`).
- `AutomationWorkerPool` (`automation/worker.py`): marks `side_effect_started` **immediately after claim, before the handler runs** (L45), so every crash mid-handler lands in `unknown_outcome` and is never retried; all JobStore calls run synchronously on the event loop.
- `RecoveryManager` (`runtime/resume.py:134-226`) + pure classification (`runtime/recovery.py`): optimistic-concurrency status transitions, uncertain tool-attempt escalation to `RECONCILE_UNKNOWN_OUTCOME`, resumption directives. No startup scan for interrupted runs; the schedule daemon never invokes it.
- Events accumulate without retention (`storage/events.py`).

### 2.6 Browser and MCP

- `BrowserManager.download()` (`browser/manager.py:633-726`): URL re-guarded, filename sanitized (`backend.py:303-309`), destination containment checked; **size limit enforced only after the download is fully written** (L623-630, L711-718). Click-triggered downloads are recorded in `session.pending_downloads` (L589-602) but **no code ever consumes them**.
- CDP download behavior targets `self._download_dir` with a `Path.cwd()` fallback (`cdp_backend.py:371`).
- MCP: config `mcp.json` in home (strict schema validation, https/loopback-only HTTP servers); stdio servers launched with configured env (replaces, not merges) and **no cwd**; tool calls flow through the standard boundary with approval and write-ahead attempts, wrapped as untrusted output; but **no workspace/identity propagation to servers** and MCP tools carry no `path_keys`, so all mutating MCP calls serialize on one global lock (`tools/boundary.py:193-194`).

### 2.7 Existing tests worth preserving

168 test files, 2035 tests. Notable coverage already present: approval security (`test_approval_security.py`), boundary lock cleanup, browser security (`test_browser_security.py`), gateway access policy, database durability, automation worker, durable tasks, bootstrap checks. Tests isolate EURUS_HOME per file (three styles verified). There is **no** dedicated test file for `paths.py` itself.

## 3. Security findings (prioritized)

| ID | Severity | Finding | Evidence |
| --- | --- | --- | --- |
| S-1 | High | Browser downloads/screenshots can land in process `cwd` (outside agent workspace) because manager is created without workspace and cached-manager ignores the workspace argument | `browser/manager.py:471`, `browser/integration.py:44-46`, `tools/builtin.py:173-176` |
| S-2 | High | No authoritative home/workspace layer: 45 ad-hoc DB path resolutions, `Path.cwd()` fallbacks in browser/MCP-server paths, no permission validation of existing directories | Section 2.2 |
| S-3 | High | Media store writes without restrictive permissions and without WAL configuration | `media.py:214-225` |
| S-4 | High | Allowlisted browser hosts bypass IP/DNS validation (SSRF/rebinding hole) | `browser/security.py:267-279` |
| S-5 | Medium | Pre-existing directories are never re-validated/tightened (`ensure_private_dir` only sets mode at creation; log/media/ui dirs use default umask) | `paths.py:32-34`, `logs.py:119`, `tui/prefs.py:40` |
| S-6 | Medium | Container runs as image-default root; no digest pinning/allowlist/labels/orphan reconciliation | `execution/sandbox.py:96-123` |
| S-7 | Medium | NetGuard/redirect caps are opt-in and caller-enforced; shell requests can never enable network even when configured | `security/net_guard.py`, `tools/shell.py:27-34` |
| S-8 | Medium | No SQLite backup/checkpoint tooling; live-WAL copies would be unsafe | grep over `src/` (no `backup`/`wal_checkpoint`) |
| S-9 | Low | `media.py` raw sqlite3 connect without WAL | `media.py:220` |
| S-10 | Low | Click-triggered browser downloads dead-end (recorded, never saved) | `browser/manager.py:589-602` |
| S-11 | Low | Oversized downloads deleted only after full write | `browser/manager.py:623-630,711-718` |
| S-12 | Low | Schedule tables outside versioned migrations; several stores bypass `Database` pool | `scheduler.py:98-123` |

## 4. Components worth preserving (do not rewrite)

- `ToolExecutionBoundary` approval/wa/reconciliation flow — already fail-closed and auditable.
- `ContainerSandbox` hardening set (read-only rootfs, cap-drop, no-new-privileges, tmpfs nosuid/noexec, pids/mem/cpu limits) — extend, not replace (M2).
- `security/net_guard.py` + `http_adapter.py` validation/IP-pinning design — becomes the core of the M3 egress proxy policy engine.
- `JobStore` lease fencing and `RecoveryManager` classification — extend into the M5 workflow engine.
- `storage/migrations.py` versioned migration pattern — extend for new tables.
- `coding/paths.py::resolve_workspace_path` per-component symlink rejection — reuse as the model for strict workspace resolution.
- `browser/security.py` UrlGuard/DnsCache — fix the allowlist hole, then reuse for the network policy engine.
- Provider abstraction, config/env override machinery (`config.py`), CLI/TUI/gateway wiring.

## 5. Migration sequence (dependency-ordered)

1. **M0 (this document)**: audit + baseline. Done.
2. **M1**: introduce `eurus.home.HomeManager` (single authoritative resolver, lazy dirs, permission enforcement, `system/` manifest, DB relocation with safe legacy adoption) and `eurus.workspace.WorkspaceManager` (scopes, strict no-follow resolution, fd-based open, cleanup/retention); migrate call sites; regression tests. Detailed design: `docs/architecture/HOME_AND_WORKSPACE.md`.
3. **M2**: ExecutionEnvelope + Docker hardening (user, seccomp, labels, digest pinning, orphan reconciliation, fail-closed secure mode).
4. **M3**: controlled egress proxy + network profiles; close S-4/S-7; browser integration.
5. **M4**: capability engine + secret broker + approval scoping on top of the envelope.
6. **M5**: workflow engine on JobStore/ScheduleStore/RecoveryManager (backoff, dead-letter, idempotency, start-up reconciliation).
7. **M6**: scoped memory, skill quarantine pipeline, subagent isolation.
8. **M7**: hardening/adversarial/failure-injection tests, clean-install validation.
9. **M8**: release candidate, freeze, stable 1.0.

## 6. Backward-compatibility decisions recorded at M0

- `paths.home_dir()`, `user_config_path()`, `project_config_path()` keep their signatures; they will delegate to the new home module.
- The runtime DB moves from `<home>/sessions.sqlite3` to `<home>/data/eurus.sqlite3` with a verified, locked, copy-once adoption of the legacy file; the legacy file is never deleted by the migration.
- File locations that users already populate (config.toml, mcp.json, ui.json, tui.toml, themes/, skills/, gateway-web.json, prices.toml, undo/) keep their existing home-relative locations; HomeManager becomes their single source of resolution instead of renaming them.
- `ToolContext` gains an optional `workspaces` field; existing constructors keep working.
- No public interface is removed in M1; the browser manager gains an explicit `set_workspace` rebind instead of the current silent-ignore.
