# Risk Register

Priorities: P0 (blocks stable 1.0), P1 (must fix before leaving the associated milestone), P2 (tracked). Status: OPEN / MITIGATED / CLOSED / ACCEPTED.

| ID | Sev | Risk | Evidence | Mitigation / Status |
| --- | --- | --- | --- | --- |
| R-01 | High | Browser downloads/screenshots land in process cwd instead of the agent workspace | audit S-1 (`browser/manager.py:471`, `integration.py:44-46`) | CLOSED M1: `BrowserManager.set_workspace` + rebind in `ensure_browser_tools`; `gateway/web/runtime.py` passes workspace; regression tests in `tests/test_browser_workspace.py` |
| R-02 | High | No authoritative home resolution; 45 ad-hoc DB paths; permission drift never corrected | audit section 2.2 | CLOSED M1: `HomeManager` single source; call sites migrated; `ensure_dir` tightens existing dirs |
| R-03 | High | Media store: world-readable dirs, no WAL, raw sqlite3 | audit S-3/S-9 | CLOSED M1: MediaStore uses home accessors, 0700/0600, `storage.sqlite.connect` |
| R-04 | High | Allowlisted browser domains bypass IP/DNS validation (SSRF/rebinding) | audit S-4 (`browser/security.py:267-279`) | OPEN, P1 for M3 (network policy engine will own DNS/IP validation for all profiles) |
| R-05 | High | Database relocation could split user data across old/new files | design section 4 | MITIGATED M1: locked single-shot adoption, legacy file preserved, all call sites use `database_path()`; residual: downgrade to 0.1.x reads the stale legacy file (ACCEPTED, documented in migration notes) |
| R-06 | Medium | Container runs as root; no digest pinning/allowlist/labels/orphan reconciliation | audit S-6 | OPEN, P1 for M2 |
| R-07 | Medium | Secure-mode downgrade paths: host backend reachable via config; `sandbox.network` flag dead for shell | audit 2.3, S-7 | OPEN, P1 for M2 (fail-closed secure mode; shell network policy via envelope) |
| R-08 | Medium | NetGuard enforcement is per-client opt-in; redirects capped only by callers | audit 2.4 | OPEN, P1 for M3 |
| R-09 | Medium | No DB backup / WAL-aware copy tooling | audit S-8 | PARTIALLY MITIGATED M1 (checkpoint+copy+verify used by adoption); full backup/retention subsystem scheduled M5/M7 |
| R-10 | Medium | Automation: pre-handler `side_effect_started`, `max_attempts=1`, no backoff/dead-letter; sync DB calls on event loop | audit 2.5 | OPEN, P1 for M5 |
| R-11 | Medium | Schedule/gateway/reminder tables live outside versioned migrations | audit S-12 | OPEN, P2 for M5 (fold into migration v6) |
| R-12 | Medium | Hardlink aliasing can bridge workspace-internal and external files | design 3.3 | MITIGATED M1 (`validate_strict` rejects nlink>1; container boundary in M2 closes it fully) |
| R-13 | Medium | Tests have no global EURUS_HOME isolation; a stray `home_dir()` call in a test touches the real home | audit 2.2 (8) | OPEN, P2 (adding autouse isolation would change many test expectations; scheduled M7 with a dedicated audit of affected tests) |
| R-14 | Low | Click-triggered browser downloads recorded but never saved | audit S-10 | OPEN, P2 (browser milestone follow-up) |
| R-15 | Low | Oversized downloads deleted only after full write to disk | audit S-11 | OPEN, P2 (streaming cap in M3 browser integration) |
| R-16 | Low | MCP stdio servers inherit process cwd; no workspace/identity propagation; mutating MCP calls serialize globally | audit 2.6 | OPEN, P1 for M2 (envelope carries cwd/workspace; `path_keys` derivation for MCP tools) |
| R-17 | Low | Multiple Database pools over one file per process (TUI opens 3) | audit 2.2 (7) | OPEN, P2 (consolidate on a single pool behind HomeManager in M5) |
| R-18 | Low | Pre-existing ruff errors / 5 unformatted files in `gateway/web/app.py` | audit section 1 | OPEN, P2 (fix in a dedicated cleanup pass, not mixed into feature changes) |
| R-19 | Low | AuditJournal has no rotation or tamper protection | audit 2.4 | OPEN, P2 for M4 |
| R-20 | Medium | Docker unavailable in the current engineering environment; docker-dependent code can only be mock-tested here | environment baseline | ACCEPTED for M2 development; a real-daemon validation checklist is part of M2 exit criteria |

## Required adversarial scenario coverage map (from the program brief)

Scenarios 01-07 map to M1/M2 workspace and home tests (01, 02, 03, 04, 05 delivered in `tests/test_workspace.py`; 06, 07 delivered as cross-scope isolation tests; host-side enforcement of 01-03 for containers is re-verified at M2 when mounts land). Scenarios 08-14 require M2/M3. Scenarios 15 requires M4. Scenarios 16-17 require M5. Scenarios 18 (secret redaction) has partial coverage today (`test_redaction.py`); full coverage lands with M4. Scenarios 19-20 map to browser/MCP milestones (19 partially covered by `tests/test_browser_workspace.py` in M1).
