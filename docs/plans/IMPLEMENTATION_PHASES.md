# Implementation Phases

Status legend: DONE / IN PROGRESS / PLANNED. Each phase lists verified entry conditions, deliverables, and exit criteria. Dependency order is mandatory.

## M0 — Repository Audit — DONE (2026-10-31)

- Full source inspection recorded in `docs/plans/IMPLEMENTATION_AUDIT.md` (every finding cites file:line).
- Baseline: 2035 tests collected; 2034 passed / 1 skipped with the `aiogram` dev extra installed (without it, `test_telegram_check_result_with_token` fails on the missing optional package); mypy strict clean; ruff has 3 pre-existing errors in `gateway/web/app.py` and 5 unformatted files (pre-existing, unchanged by this program so far).
- Risk register established: `docs/plans/RISK_REGISTER.md`.

## M1 — Home & Workspace — DONE (2026-10-31)

Delivered:

- `src/eurus/home.py`: `HomeManager`, `HomeError`, `resolve_home()`, `home_manager()`; lazy 0700 directories, permission tightening of existing dirs, `system/` manifest and version markers.
- `src/eurus/workspace.py`: `WorkspaceManager`, `WorkspaceError`; default/project/run/automation/shared/artifact scopes, `resolve`/`resolve_strict`/`validate`/`validate_strict`, descriptor-relative `open_read`/`open_write` with `O_NOFOLLOW`, guarded `cleanup`, retention `sweep`.
- `paths.py` delegates to the home module (signatures preserved).
- Database relocation `sessions.sqlite3` -> `data/eurus.sqlite3` with locked, checkpointed, verified adoption; all 45 literal call sites migrated to `HomeManager.database_path()`.
- Permission fixes: `media.py` (0700 dirs, 0600 db, WAL via `storage.sqlite.connect`), `tui/prefs.py`, `browser/cdp_backend.py` profile dir (0700), `paths.ensure_private_dir` now tightens existing dirs and is the single implementation (bootstrap env-manager delegates to it).
- Browser workspace rebind (`BrowserManager.set_workspace`) fixing the cwd-download leak; `gateway/web/runtime.py` passes the configured workspace.
- `ToolContext.workspaces` optional field; factory attaches `WorkspaceManager`.
- Tests: `tests/test_home.py` (18), `tests/test_workspace.py` (24, including the M1 adversarial scenarios: user-ssh isolation, eurus-home isolation, cross-project and cross-run access rejection, scoped cleanup), `tests/test_browser_workspace.py` (5) - 47 new tests; two pre-seeded operator/verification tests updated to the canonical database path.

Exit criteria met: full suite green, mypy strict clean, new files ruff-clean, zero comments in new source files.

## M2 — Docker Execution — PLANNED

Entry: M1 merged. Scope:

- `ExecutionEnvelope` (immutable post-authorization): run id, workspace id, isolation backend, network profile, capabilities, resource budget, timeout, approval scope, audit correlation, cancellation token.
- ContainerSandbox extensions: `--user` (host uid:gid mapping or fixed non-root), seccomp profile, AppArmor where supported, container labels (`eurus.run_id`, `eurus.attempt`, `eurus.workspace_id`), image digest pinning + allowlist config, orphan-container reconciliation sweep, rootless-docker detection (document as untested until actually exercised).
- Secure Mode: fail-closed when Docker is unhealthy/misconfigured (structured error + security event; never fall back to host). Compatibility (host) mode stays explicit-only, warning on startup, distinguishable in audit records.
- Envelope enforcement in `ToolExecutionBoundary` (single entry point; tools cannot self-authorize).
- Environment limitation: this workspace has no Docker daemon; sandbox changes ship with mock-based tests plus a manually executed docker validation checklist.

## M3 — Network Security — PLANNED

Entry: M2 envelope exists. Scope: controlled egress proxy process, network profiles (`offline`, `web_read`, `api_access`, `package_install`, `restricted`), container network isolation enforced at the docker network level (not only in-process), redirect revalidation centralized, rate/connection/size limits, close audit finding S-4 (allowlisted-host DNS bypass) and S-7 (per-client opt-in). No global TLS interception; end-to-end TLS preserved; secret redaction in proxy logs.

## M4 — Unified Capability Security — PLANNED

Entry: envelope + network profiles. Scope: capability registry (`filesystem.*`, `shell.execute`, `network.*`, `browser.*`, `git.*`, `automation.*`, `secrets.use:<provider>`), scoped/expiring grants bound to run identity, approval bound to (run, tool, argument fingerprint, capability, scope, expiry, approver) reusing `action_fingerprint`, secret broker backed by `<home>/secrets`, high-risk operation classes requiring explicit approval.

## M5 — Automation Engine — PLANNED

Entry: M1 stores + M4 capabilities. Scope: workflow schema (versioned, validated), workflow/step/attempt tables via `storage/migrations.py`, trigger engine (manual/once/interval/cron/event/webhook/filesystem — cron needs a parser; no external broker), retry with exponential backoff + jitter, dead-letter handling, general idempotency keys, startup reconciliation sweep invoking `RecoveryManager`, worker off-loop SQLite calls (`asyncio.to_thread`), human approval checkpoints via ApprovalBroker. At-least-once semantics with idempotency; no exactly-once claims.

## M6 — Autonomous Capabilities — PLANNED

Scope: memory scopes (global/project/session/automation) with cross-project leakage tests, skill quarantine lifecycle (download -> quarantine -> manifest validation -> static inspection -> permission declaration -> approval -> installed), subagent execution identities with scoped capabilities/budgets/audit, untrusted-content handling for external documents.

## M7 — Hardening — PLANNED

Scope: the 20 required adversarial scenarios from the program brief (test matrix lives in `RISK_REGISTER.md`), failure injection (docker unavailable, proxy down, db failure, worker crash, audit failure), performance smoke, clean-install validation, operational runbooks.

## M8 — EURUS Stable 1.0 — PLANNED

Release candidate, feature freeze, final security review, migration guide, stable release. Version target 1.0.0.
