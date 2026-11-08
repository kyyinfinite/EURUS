# Eurus 0.4.0b1 — Private Beta / Pre-release

This is the first beta-quality build, distributed as a private beta for trusted
testers. It is intended for single owners who want a local, durable, approval-gated
agent runtime. It is not a stable release, not production-ready, and not multi-user
software.

## What this release is

- A stabilization release. No new feature surface was added; the existing runtime,
  tool ecosystem, MCP integration, browser automation, coding workspace, gateways,
  autonomous runtime, and persistence layers were hardened, tested, and packaged.

## What changed since 0.3.0

See `CHANGELOG.md`. The headline items:

- Fail-closed write-ahead recording and pre-mutation checkpoints.
- Mature JSON Schema validation for tool arguments and declared outputs.
- Proxy-enforced browser egress with DNS-rebinding protection, verified against a
  real Chromium.
- Scrubbed environments for all child processes, including git.
- Rate limiting, redaction, and containment fixes in the web gateway.
- Release wheels bundle the built web UI; the gateway resolves its static directory
  through a documented chain (`EURUS_GATEWAY_STATIC`, explicit, packaged, repository)
  and answers frontend routes with an actionable 503 page when no UI build exists.
- Unknown `/api/*` routes return 404 JSON instead of the SPA fallback; static asset
  serving blocks traversal and symlink escapes.
- Repo-wide zero-comment policy with a multi-language checker in CI.
- Documentation-link checker in CI; installation, upgrade, and limitations docs
  re-verified against actual behavior.
- Version metadata synchronized at `0.4.0b1` across package, frontend, and docs.
- Release artifacts (wheel, sdist, full source zip) ship with a `SHA256SUMS.txt`;
  verify with `sha256sum -c SHA256SUMS.txt`.

## Validation evidence

- `ruff check` / `ruff format --check`: clean.
- `mypy` strict: clean (404 source files).
- `pytest -q`: 3251 passed, 1 skipped.
- `python -m compileall -q src tests`: clean.
- Frontend: `tsc --noEmit` clean, 65 tests passed, production build succeeds.
- Zero-comment checker: 0 violations across 770 files.
- Wheel and sdist pass `twine check`; wheel `RECORD` hashes verified; the wheel
  embeds the built web UI (`eurus/gateway/web/static`).
- Clean-venv wheel install with declared dependencies verified: CLI entry points,
  `eurus doctor`, `eurus config init`, packaged-UI resolution from outside the
  source tree.
- Installed-wheel live smoke (`scripts/verify_release_install.py`): 18 checks
  covering static serving, API auth, rebinding/traversal rejection, a full agent
  run with `calculate` tool execution, response delivery, and log redaction.
- Real-Chromium egress tests verify traffic flows through the enforcing proxy and
  that rebinding to a forbidden address is refused with a visible 403.
- End-to-end scenario clusters (research-to-document, coding, MCP failure, autonomous
  recovery, security, gateway consistency) all pass.
- Remote CI runs the same gates; results are recorded per commit on the CI system.

## How to install

See `docs/installation.md`. In short:

```bash
python -m pip install eurus-agent
eurus config init
eurus doctor
eurus run "hello"
```

## What to try first

1. `eurus run "..."` — a single-shot session with approval-gated tools.
2. `eurus gateway web` — the local web UI.
3. `eurus missions` — hierarchical engineering missions with durable recovery.

## Reporting issues

- Security problems: see `SECURITY.md` (private report, no public issues).
- Everything else: open an issue with the output of `eurus --version`,
  `eurus doctor`, and `eurus mcp doctor` attached.

## Known limitations

`docs/known-limitations.md` is the honest list. The short version: the host sandbox
backend is unconfined (use the container backend when you need isolation), remote CDP
attach cannot enforce browser egress, and there is no multi-user mode.
