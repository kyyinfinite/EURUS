# Engineering Report — EURUS Native MCP Platform

Deliverable: `eurus-native-mcp.zip` (complete repository).
Date: 2026-10-30. SDK under test: `mcp` 1.27.0 (pinned `>=1.27,<2`).

---

## 1. Original architecture findings

- EURUS is a provider-agnostic agent runtime (CLI, TUI, Telegram gateway,
  scheduler) with a central `ToolRegistry` (`src/eurus/tools/base.py`) and a
  hardened `ToolExecutionBoundary` (`src/eurus/tools/boundary.py`) providing
  approvals, write-ahead attempt records and mutation locks.
- MCP support was client-only and optional: `src/eurus/mcp/manager.py`
  (283 lines) with `MCPToolManager` — a single shared `AsyncExitStack`, all
  servers connected sequentially, any failure aborted and closed **every**
  server, no reconnection, no per-server state, `mcp>=1.9,<2` behind the
  `mcp` extra.
- Integration points loading `MCPToolManager` directly: `cli/common.py`,
  `cli/run.py`, `cli/schedule.py`, `cli/gateway.py`, `tui/session.py`,
  `bootstrap/integrations/mcp.py` (seven call sites).
- MCP tools were registered `mutates=True, requires_approval=True`
  (fail-closed for everything).
- No OAuth, no Google integration, no server mode, no policy engine.
- Tests: 1849 passed / 1 skipped; `ruff` clean; `mypy src` 34 pre-existing
  errors in 10 untouched bootstrap/CLI files (verified against the pristine
  tree — unchanged, see §13).

## 2. Implemented features

1. **MCP client, rebuilt** — independent per-server connection tasks, exit
   stacks and tool sets; states `disabled/disconnected/connecting/connected/
   failed/reauth_required`; concurrent connects; per-server timeouts
   (initialize, discovery, calls); hard watchdog; error isolation (a failed
   server never disconnects healthy ones); single-server reconnection with
   one automatic retry on tool calls; tool removal on disconnect; graceful
   shutdown; conflict-safe tool-name merging with per-server diagnostics.
2. **MCP server** — Eurus exposes its own registry over STDIO and Streamable
   HTTP (127.0.0.1:8765 default, bearer token, loopback-only auth opt-out,
   remote binds refused without a token), explicit tool allowlist,
   mutating/approval-gated tools denied by default, schema validation via
   the ToolRegistry, `McpError` surfaces for denials.
3. **Native Google Workspace provider** — all nine official services with
   verified endpoints/scopes, per-service enable/disable + timeouts, shared
   per-account authentication, live tool discovery (nothing hardcoded),
   per-service status/diagnostics, per-account failure isolation.
4. **Google OAuth 2.0** — Authorization Code flow with offline access, PKCE
   (S256), cryptographically random state with CSRF rejection, localhost
   browser callback, token exchange, automatic refresh with per-account
   concurrency coordination, refresh-token rotation, revocation, multi-account
   with switching, reauthentication detection (`ReauthenticationRequiredError`
   → server state `reauth_required`).
5. **Secure credential storage** — OS keyring first, then PBKDF2-HMAC-SHA256
   (600k iterations) + Fernet-encrypted files with a user passphrase; no
   silent plaintext fallback (refuses with instructions); 0600 files, 0700
   directories; tokens never in config files, logs or model context.
6. **Security policy engine** — `read_only` / `reversible_write` /
   `sensitive_mutation` / `destructive` / `unknown` classification; sensitive
   and destructive levels always require approval (not configurable);
   external servers keep the historical fail-closed default; user rules via
   `security/mcp-policies.json` and `eurus mcp permissions`.
7. **Untrusted-content guard** — every remote MCP result (including emails,
   Docs content, Drive files, calendar descriptions) is wrapped in an
   explicit untrusted-content envelope before reaching the model.
8. **Centralized runtime** — `MCPRuntime.build()` assembles external servers
   + Google services + policy into one manager; used by CLI, TUI, gateway
   and scheduler; Google problems never block startup.
9. **CLI** — all legacy commands preserved; new `auth google *`, `mcp google
   *`, `mcp server *`, `mcp doctor`, `mcp tools`, `mcp permissions`.
10. **MCP as a core dependency** — `mcp>=1.27,<2` moved to core requirements
    (same major version as the existing implementation; no v2 migration);
    `[mcp]` extra retained; new `[google]` extra for keyring + cryptography.

## 3. New files

**Source (22):**
`src/eurus/mcp/auth/{__init__,oauth,google,callback,token_store,accounts,credentials}.py`,
`src/eurus/mcp/providers/{__init__,google_workspace/{__init__,provider,catalog,scopes,endpoints,services}.py}`,
`src/eurus/mcp/policy.py`, `src/eurus/mcp/runtime.py`,
`src/eurus/mcp/server/{__init__,runtime,tools,access,transport}.py`,
`src/eurus/cli/auth.py`.

**Tests (11):** `tests/conftest.py` (mock MCP-HTTP server, mock Google OAuth
server with PKCE verification, stdio mock helper), `tests/mcp_stdio_mock.py`,
`tests/test_mcp_lifecycle.py`, `tests/test_mcp_policy.py`,
`tests/test_mcp_server.py`, `tests/test_mcp_runtime.py`,
`tests/test_google_oauth.py`, `tests/test_google_token_store.py`,
`tests/test_google_manager.py`, `tests/test_google_workspace.py`,
`tests/test_cli_auth_google.py`.

**Docs (5):** `docs/GOOGLE_OAUTH.md`, `docs/GOOGLE_WORKSPACE.md`,
`docs/MCP_SERVER.md`, `docs/MCP_SECURITY.md`, `docs/TROUBLESHOOTING.md`.

## 4. Modified files

`pyproject.toml`; `src/eurus/errors.py` (new `ReauthenticationRequiredError`);
`src/eurus/mcp/manager.py` (full lifecycle refactor, public API preserved);
`src/eurus/mcp/__init__.py`; `src/eurus/bootstrap/dependencies/catalog.py`
(mcp → core, new google feature); `src/eurus/cli/{__init__,common,run,
schedule,gateway,integrations}.py`; `src/eurus/tui/session.py`;
`tests/test_mcp.py`; `docs/MCP.md` (rewritten); `README.md`; `.env.example`.

## 5. Dependency changes

- Core: added `mcp>=1.27,<2` (was optional `[mcp]` extra; same 1.x line as
  the original `>=1.9,<2` pin — no SDK migration).
- Extras: `[mcp]` retained (now redundant); new `[google]` =
  `keyring>=25` + `cryptography>=42`; `[dev]` gains both for test coverage.
- Bootstrap catalog updated accordingly (`mcp` now reported as core-ready;
  `google` feature installable through the wizard). Old install commands and
  dependency-resolution behavior preserved.

## 6. OAuth architecture

```
eurus auth google login
  -> OAuthClient (env EURUS_GOOGLE_CLIENT_ID/SECRET or auth/google/client.json)
  -> CallbackServer on 127.0.0.1 (dynamic port; explicit redirect URI honored)
  -> accounts.google.com/o/oauth2/v2/auth  (PKCE S256 + random state + offline)
  -> browser consent -> localhost callback (state validated, single-use, 10min TTL)
  -> oauth2.googleapis.com/token (code exchange)
  -> openidconnect userinfo (account email)
  -> CredentialStore (keyring | encrypted file) + GoogleAccountManager

TokenManager.get_access_token(account)
  -> valid? return : per-account lock -> single refresh -> persist (+rotation)
  -> invalid_grant -> ReauthenticationRequiredError -> server state reauth_required
```

GoogleBearerAuth (an `httpx.Auth`) injects a fresh token into every MCP HTTP
request, so refreshes are transparent; a failing account affects only its own
servers. Logout revokes at `oauth2.googleapis.com/revoke` and deletes local
material.

## 7. Google Workspace service status

Implemented and tested against mock MCP + mock OAuth servers; **live Google
integration not verified** (no Developer Preview credentials in the test
environment). Endpoints and scopes verified against Google's official
configuration guide. All nine services are built in and disabled by default;
only enabled services' scopes are requested at login.

| Service | Endpoint | Scopes | Notes |
| --- | --- | --- | --- |
| Gmail | gmailmcp.googleapis.com/mcp/v1 | 2 | |
| Drive | drivemcp.googleapis.com/mcp/v1 | 2 | |
| Docs | docsmcp.googleapis.com/mcp/v1 | 4 | needs `docsmcp` API enabled |
| Sheets | sheetsmcp.googleapis.com/mcp/v1 | 4 | needs `sheetsmcp` API enabled |
| Slides | slidesmcp.googleapis.com/mcp/v1 | 4 | needs `slidesmcp` API enabled |
| Calendar | calendarmcp.googleapis.com/mcp/v1 | 3 | |
| Chat | chatmcp.googleapis.com/mcp/v1 | 6 | Workspace account + Chat app required |
| People | people.googleapis.com/mcp/v1 | 3 | |
| Classroom | classroommcp.googleapis.com/mcp/v1 | 7 | Education account required |

Requires external Google Cloud configuration: project, per-service MCP API
enablement, OAuth consent screen, OAuth client with the **"Use this client
for an AI-powered agent"** option, and Developer Preview enrollment —
documented in `docs/GOOGLE_OAUTH.md` / `docs/GOOGLE_WORKSPACE.md`.

## 8. MCP client status

**Implemented and tested** (mock stdio + streamable-HTTP servers, real SDK):
discovery, schema validation, registration, concurrent connections, partial
failures, per-server reconnection, timeouts, session cleanup, error
isolation, tool-conflict skipping, backward-compatible `mcp.json` format
(including `{ "servers": [] }`), loopback `http://` allowance, auth-provider
hook (used by Google), untrusted-output wrapping. Google service connections
share the identical code path with mock-server test coverage; live Google
connections not exercised (see §7).

## 9. MCP server status

**Implemented and tested**: STDIO (subprocess round-trip test) and
Streamable HTTP (in-process uvicorn + real MCP client test); bearer
authentication (401 without/with wrong token); allowlist; mutating-tool
denial; unknown-tool and invalid-argument errors; `McpError` payloads;
graceful shutdown; loopback default; remote-bind refusal without token;
one-time generated tokens for loopback HTTP.

## 10. Security implementation

- Approval boundary untouched and mandatory for all MCP mutations; sensitive
  and destructive classifications cannot be relaxed.
- External-server tools default to approval-required (unchanged behavior).
- Untrusted-content envelope on all remote MCP output.
- Keyring/encrypted-file credential storage; PBKDF2 600k; 0600/0700 perms;
  CSRF state validation; PKCE; no tokens in configs/logs/model context; MCP
  server never exposes credentials.
- `eurus mcp add-http` still never writes credentials into `mcp.json`.

## 11–13. Tests

Executed (final run): `pytest` (full suite) — **1960 passed, 1 skipped**
(baseline 1849 passed + 1 skipped; +111 new tests, 0 regressions).

New/changed test files and counts:
`test_mcp_lifecycle.py` 11, `test_mcp_policy.py` 12, `test_mcp_server.py` 13,
`test_mcp_runtime.py` 9, `test_google_oauth.py` 11, `test_google_token_store.py`
16, `test_google_manager.py` 11, `test_google_workspace.py` 11,
`test_cli_auth_google.py` 16, `test_mcp.py` 9 (two updated for the new
loopback-HTTP and untrusted-envelope behaviors).

Coverage highlights: service catalog/endpoint validation; OAuth state
validation (forged/expired/reused), PKCE end-to-end (mock rejects wrong
verifier); token storage encryption (wrong passphrase rejected, no plaintext
on disk, permissions); refresh, rotation, revocation, concurrency
coordination; multi-account login/switch/logout; policy enforcement
(including non-relaxable levels); tool discovery and schema validation;
connection lifecycle, partial failures, reconnection, concurrent servers,
timeouts, session cleanup; MCP server stdio + HTTP, auth, allowlist,
permission enforcement, invalid arguments; CLI commands with isolated
`EURUS_HOME`; regression checks for every legacy `mcp` command.

`ruff check .` — **clean**. `mypy src` — **34 errors, all pre-existing** in
10 untouched files (`bootstrap/*`, `cli/bootstrap.py`); the modified tree
introduces **zero** new mypy errors (verified by diffing error lists against
the pristine tree).

Tests failed during development and were fixed: none remain. No test was
skipped except the pre-existing Chromium-availability browser test (passes
when Chromium is present).

## 14. Known limitations

- **Live Google integration not verified.** No Developer Preview account,
  OAuth client or enabled Google APIs were available in the test
  environment; all Google flows are covered by mock servers that implement
  Google's documented protocol (PKCE-verified token endpoint, userinfo,
  revoke). Live behavior may additionally depend on Google-side preview
  enrollment and per-account availability.
- The MCP SDK's Streamable HTTP client requires TLS outside loopback;
  `mcp.json` therefore only accepts `https://` (loopback excepted).
- Remote MCP servers with OAuth-protected endpoints need headers supplied
  by the user in `mcp.json` (unchanged); only Google endpoints get native
  OAuth.
- The policy engine classifies Google tools by name heuristics derived from
  Google's documented tool families; users can override per server/pattern,
  and unmatched tools always fail closed.
- The built-in MCP server exposes registry tools only (not prompts or
  resources), by design.
- stdio servers inherit the documented SDK behavior for environment merging.
- Pre-existing repo issues left untouched: 34 mypy errors in bootstrap/CLI
  bootstrap modules; four broken `BOOTSTRAP_*.md` links in README.md
  (present in the original).

## 15. Installation

```bash
unzip eurus-native-mcp.zip && cd eurus
python -m pip install -e .            # MCP client + server included
python -m pip install -e ".[google]"  # optional: keyring + cryptography
eurus --help
```

Verified: the wheel builds from package metadata (`pip wheel .`) and
contains all 20 new `eurus/mcp/**` modules plus `py.typed`.

## 16. Google OAuth setup (summary)

1. Google Cloud project; enable the Workspace MCP services you need
   (`gcloud services enable gmailmcp.googleapis.com … --project=PROJECT_ID`).
2. Google Auth Platform: consent screen (Internal or External + test users)
   and the scopes for your enabled services.
3. Create an OAuth client of type **Desktop app** and enable **"Use this
   client for an AI-powered agent"** (required by Google's Workspace MCP
   servers).
4. `export EURUS_GOOGLE_CLIENT_ID=… EURUS_GOOGLE_CLIENT_SECRET=…`
   (or `~/.eurus/auth/google/client.json`, mode 600).
5. Optional passphrase storage: `export EURUS_GOOGLE_TOKEN_PASSPHRASE=…`
   (required on systems without a keyring, e.g. Termux/headless).
6. `eurus mcp google enable gmail && eurus auth google login`.

Full guides: `docs/GOOGLE_OAUTH.md`, `docs/GOOGLE_WORKSPACE.md`,
`docs/MCP_SERVER.md`, `docs/MCP_SECURITY.md`, `docs/TROUBLESHOOTING.md`,
`docs/MCP.md`.

---

### Status legend

- **Implemented and tested** — covered by the automated suite above.
- **Implemented but not live-tested** — Google Workspace connections against
  real Google endpoints (mock-verified only); OS-keyring storage on
  macOS/Windows (Linux without a keyring daemon is covered by the encrypted
  backend and the refusing backend tests).
- **Not implemented** — nothing from the requested scope.
- **Requires external Google Cloud configuration** — everything in §7
  (project, APIs, consent screen, AI-powered-agent OAuth client, Developer
  Preview enrollment).
