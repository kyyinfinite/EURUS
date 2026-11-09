# Changelog

## Unreleased — Advanced Interactive Terminal Workspace

### Workspace TUI

- Explorer double-click now requires both clicks on the same row and uses an
  injectable clock, which removes a timing-dependent test failure on slow CI runners.
- `eurus tui` now opens a multi-panel workspace when the terminal is at least
  100 columns wide: file explorer, tabbed main workspace (chat, editor, diff,
  preview), agent activity feed, composer and a contextual footer. Narrow
  terminals, Termux-sized windows and `EURUS_WORKSPACE=off` keep the classic
  fullscreen interface; `EURUS_WORKSPACE=on` forces the workspace.
- Layout presets (Focus, Workspace, Coding) switch live with F8; panels
  collapse with F2/F3 and resize with Alt+Minus/Alt+Equal or by dragging the
  splitters with the mouse.
- File explorer with lazy tree expansion, incremental search, recent files,
  dirty markers and permission-error reporting; every path is resolved through
  the existing workspace security layer (traversal, symlink and size guards).
- Integrated terminal editor: syntax highlighting, line numbers, selection,
  delta-based undo/redo, find/replace bar, atomic saves with mode
  preservation, external-change conflict detection with overwrite/reload,
  confirmed discard, and read-only previews for binary and >1 MB files.
- Diff engine with unified and side-by-side views, dual line numbers, hunk
  navigation, per-hunk and whole-file accept/reject, and hash-validated apply
  to the editor buffer or directly to the file.
- Agent activity feed rendering real agent events: thinking indicators, tool
  cards with durations and redacted argument summaries, file read/edit paths,
  approvals, errors, lessons, plans and turn summaries with token usage; fed
  through a single optional adapter hook that leaves the agent runtime
  untouched.
- Composer `@` file-reference completion backed by a budgeted fuzzy workspace
  index, quick-open in the command palette, and workspace actions (panel
  toggles, presets, save, review, close tab).
- SGR mouse support: click-to-focus panels, click tabs and file rows,
  double-click to open, wheel scrolling, splitter dragging, OSC52 clipboard
  copy with graceful degradation; full keyboard equivalents everywhere.
- New `charcoal` builtin theme matching the workspace design; applied only
  when no explicit theme is configured.
- Transcript lines are ANSI-parsed once and cached; all long lists render only
  their visible slice.
- The classic fullscreen interface, inline interface, slash commands, themes,
  sessions, approvals and the agent runtime are unchanged; the full baseline
  test suite passes without modification.

## 0.4.1 — Bugfix release

### Test-suite and reliability fixes

- The test suite no longer exhausts the process file-descriptor limit. Every
  `Database` holds its pooled SQLite connections (three descriptors each in WAL
  mode) until garbage collection reclaims the object, so suites that create one
  database per test accumulated ~950 descriptors and failed late files with
  `OSError: [Errno 24] Too many open files`. `tests/conftest.py` now tracks live
  `Database` instances weakly and closes their pools after every test; the
  observed descriptor growth over the full run dropped from ~940 to a bounded
  maximum of 62.
- `websockets>=12` is now part of the `dev` extra. Clean `pip install
  -e ".[dev]"` environments previously failed the three realtime-voice tests
  with `ModuleNotFoundError` because the runtime dependency of the optional
  `browser-cdp` extra was missing from the development set.
- The last remaining `# type: ignore[assignment]` in the repository was removed
  the honest way: the realtime-bridge governance test now occupies the
  concurrency slot with a real `asyncio.Task` obtained from
  `asyncio.current_task()` instead of forcing an `object()` into the typed
  dictionary.

### Error messages

- The `ConfigError` raised when no model is configured ("no model configured")
  and the `AuthenticationError` raised when a provider API key environment
  variable is empty now state the concrete recovery step — run `eurus setup
  provider` — across all seven raise sites (provider chain resolution, provider
  base, STT, TTS, realtime voice, and the three generation clients). Related
  CLI and provider tests assert the hint.

### Dead code removal

- `src/eurus/media.py` was deleted. The legacy module was fully shadowed by the
  `eurus.media` package (import resolution prefers the package; verified
  empirically and by grep across `src/`, `tests/`, `scripts/`, and `docs/`),
  so it was unreachable at runtime and only risked confusion.
- `frontend/src/components/chat/media.tsx` was deleted after grep proof of zero
  imports; typecheck, tests, and the production build stay green.

### Documentation

- `docs/installation.md` now describes the source-checkout install correctly:
  build the frontend first (`cd frontend && npm ci && npm run build`) when the
  web UI should be embedded, then `python -m pip install ".[web-gateway]"`
  from the repository root — the previous snippet installed from PyPI instead
  of the local tree and ran npm outside `frontend/`. The Node.js requirement is
  now stated as `>= 22.19.0`, matching `frontend/package.json` engines and CI.
- Version metadata synchronized at `0.4.1`: package version, frontend
  `package.json` and `package-lock.json` (previously stale at `0.1.0`), README
  release badge, `SECURITY.md` supported-releases table, the
  `docs/known-limitations.md` title, and the artifact names in
  `docs/installation.md`.

## 0.4.0b1 — Beta stabilization release

### Packaging and release readiness

- Release wheels now bundle the built web UI. A custom Hatch build hook embeds
  `frontend/dist` into the wheel as `eurus/gateway/web/static` when the frontend has
  been built, and embeds it into source distributions as `frontend/dist`; builds
  without a frontend build still succeed and warn.
- The web gateway resolves its static directory through a documented chain:
  `EURUS_GATEWAY_STATIC` (absolute, or relative to the current directory), an
  explicitly passed directory, assets packaged inside the installed wheel, then
  `frontend/dist` in a source checkout. A configured directory without `index.html`
  is rejected with a warning instead of being served.
- Missing frontend assets are no longer silent: the gateway logs a warning, prints
  the limitation in the startup banner, reports `web_ui.available` from
  `GET /api/health`, and answers frontend routes with an actionable `503` page.
- Unknown `/api/*` routes now answer `404` JSON instead of falling through to the
  SPA fallback; nonexistent asset paths (with a file extension) answer `404` instead
  of `index.html`. Symlink and encoded `..` traversal out of the static directory
  stay blocked.
- Clean-installation behavior verified: the wheel installs with its declared
  dependencies in an isolated environment, the CLI works outside a source checkout,
  and the installed gateway serves the packaged web UI.
- `scripts/verify_release_install.py` automates the installed-wheel smoke test:
  packaged-UI resolution from outside the source tree, static asset content types,
  SPA fallback, unknown-route and traversal rejection, a full agent run that executes
  the `calculate` tool end to end, and log-redaction assertions.
- Release artifacts (wheel, sdist, full source zip) are accompanied by a
  `SHA256SUMS.txt`; `docs/installation.md` documents `sha256sum -c SHA256SUMS.txt`
  verification and wheel/source install paths.
- The `eurus.skills` package (loader, tools, builtin skills) was missing from the
  repository working tree while still imported by the agent, TUI, automation, and
  tests; restored so source checkouts match the packaged distribution.
- `docs/installation.md` now matches actual behavior: `eurus config init` writes
  only `config.toml` (the `.env` file is created by the user), and the gateway
  access token is described as a persistent credential instead of a one-time token.
- CI gains a documentation-links job, builds the frontend before packaging, asserts
  the wheel embeds the UI, and installs the wheel with the `web-gateway` extra in a
  clean venv.
- README mojibake (mis-decoded separators, box drawing, and heading emojis) repaired;
  the dead `RESEARCH_ENGINE.md` link removed; `scripts/check_docs_links.py` now
  covers `docs/` recursively and `benchmarks/`.

### Mission learning and progression stabilization

- Mission learning is fully integrated: `LearningCoordinator` accepts a
  `missions` subsystem and a validated `evaluation_retention_days`, the tick report
  carries `missions_processed`/`missions_failed`/`missions_skipped`/`evaluations_pruned`,
  and coordinator stats expose `mission_terminal`/`mission_learned`/`mission_pending`.
  `build_stack` wires `MissionLearning` and `MissionProgression` from configuration
  (`[learning] mission_learning`, `[agentic_engineering] autonomous_progression`),
  and the `AutonomyStack` exposes `missions`.
- The runtime supervisor gained a `missions` subsystem: report counters
  (`missions_ticked`, `mission_tasks_dispatched`, `missions_finalized`), per-subsystem
  backoff, async off-loop execution, and health snapshots; the autonomy service maps the
  specialists toggle to both `assignments` and `missions`.
- Mission surfaces: `GET /api/missions/{id}/learning`, `GET /api/missions/analytics`
  (declared before the `{mission_id}` route so the literal path wins), `422` responses
  when agentic engineering is disabled, and CLI `eurus missions learning` / `analytics`.
- Lesson retrieval now ranks candidates by deterministic term overlap with owner
  scoping, and `LessonStore.add` rotates the oldest lesson within its own scope
  (global vs per-owner) when the capacity is reached instead of silently refusing.
- `EvaluationEngine.prune(retention_days, batch_limit)` removes expired evaluations
  and their provenance in bounded, repeatable batches while preserving mission
  learning markers; retention is configurable via `[learning] evaluation_retention_days`
  (0 disables pruning).

### Artifact and MCP recovery stabilization

- `ArtifactStore.list_page` provides stable keyset pagination
  (`created_at DESC, artifact_id DESC`) with validated cursors, expiry filtering and
  clamped limits; `count_active` backs the gateway totals. The gateway exposes
  `GET /api/artifacts`, `GET /api/artifacts/{id}` and `GET /api/artifacts/{id}/content`
  with owner isolation, `422` for malformed cursors and `404` for unknown, expired,
  missing or tampered artifacts.
- MCP connections recover from transient failures: `connect` retries failed servers
  with bounded exponential backoff (cancellable, non-retryable auth failures exempt),
  `register_tools` before connect defers and flushes after discovery or reconnection
  without duplicate registrations, and `MCPToolManager.health_check()` reports
  `connected`/`disconnected`/`unreachable` per server and demotes dead sessions.

### Security and reliability hardening

- Browser URL validation rejects embedded credentials and out-of-range ports while
  preserving legitimate HTTP/HTTPS behavior; `browser_download` now requires owner
  approval; `WorkspaceManager` refuses to hand an allocated assignment to a second owner.
- Completed tool events now carry `duration_ms`, `is_error`, `error_code` and
  `uncertain`, including timeout-status outputs mapped through `OUTCOME_ERROR_CODES`.
- The durable runtime event stream no longer loses the final event when the engine
  task and the queue consumer complete in the same wait round (the previous race
  silently dropped the `done` event and its usage totals).
- The scheduled-run skill hook yields once after scheduling its draft task and uses
  `asyncio.timeout`, so the draft executes reliably on Python 3.11 instead of being
  cancelled with the caller's loop.
- CI installs LibreOffice and poppler for the office QA rendering contract, restores
  the frontend test job, and the test suite's git fixture sets an explicit commit
  identity for runners without one.

### Critical fixes

- JSON Schema validation now uses the mature `jsonschema` implementation (already a
  dependency of the `mcp` package). Unknown schema types, malformed schemas, and
  draft-04 constructs are rejected explicitly instead of being silently accepted;
  `pattern`, `minItems`/`maxItems`, `uniqueItems`, `minProperties`/`maxProperties`,
  `exclusiveMinimum`/`exclusiveMaximum`, `multipleOf`, `const`, and `additionalProperties`
  as a schema are all enforced. Unresolvable external `$ref` values fail closed.
- Write-ahead attempt recording and pre-mutation checkpoints now fail closed. If a
  required durable record cannot be persisted, or a checkpoint cannot be captured, the
  mutation is aborted before the tool runs; the abort is reported as a structured
  outcome with a `tool.aborted` boundary event. Read-only tools are unaffected.
- The browser now routes all Chromium egress through a local enforcing proxy. DNS
  resolution happens only inside the proxy, the validated address is the address
  actually connected to, and rebinding between lookups is refused with an HTTP 403 the
  agent can see. Redirects, subresources, and WebSocket upgrades re-enter the same
  policy. This closes the DNS-rebinding and TOCTOU gap between URL validation and the
  real network path.
- The tool contract gained an optional `output_schema`. When declared, tool outputs are
  parsed as JSON and validated on both execution paths; violations fail the call.

### Security hardening

- Git child processes no longer inherit the full host environment. They receive the
  scrubbed allowlist (`PATH`, `HOME`, `LANG`, locale, `TMPDIR`, and friends) with
  `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, and other repository-override
  variables removed.
- Failed isolated-worktree creation no longer leaks its temporary directory.
- The web gateway gained a global per-token rate limit (240 requests per minute,
  429 with `Retry-After`), a WebSocket connection cap, generic catch-all error bodies
  (details stay in server logs), component-correct static-file containment, and
  `text/html` media is served as an attachment instead of inline.
- All provider header values are redacted in `GET /api/config` responses.
- Media payloads are written with `0600` permissions inside `0700` session directories.

### Zero comments

- The comment checker now covers every code, script, configuration, and SQL file in
  the repository (Python, TypeScript/JavaScript, CSS, HTML, SQL, shell, TOML, YAML)
  and runs in CI. The repository is at zero comments across all checked files.

### Versioning

- Package version: `0.4.0b1` (PEP 440). Frontend version: `0.4.0-b1` (npm semver
  spelling of the same release). Development status classifier moved to `4 - Beta`.

## 0.3.0

- Knowledge lifecycle, bounded surfaces, and phase-9 stabilization work.

## 0.2.0

- Learning loop and mission intelligence.

## 0.1.0

- Initial pre-alpha: tool ecosystem, MCP integration, browser automation, coding
  workspace, gateways, autonomous runtime.
