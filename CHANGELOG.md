# Changelog

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
