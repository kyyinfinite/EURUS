# Security Policy

## Supported releases

| Version | Supported |
| ------- | --------- |
| 0.4.1 | yes |
| < 0.4.1 | no |

## Reporting a vulnerability

1. Do not open a public issue for security problems.
2. Report privately to the maintainer of the repository you received Eurus from,
   including reproduction steps, the affected file paths, and the Eurus version
   (`eurus --version`).
3. Include the output of `eurus mcp doctor` if the problem involves the MCP stack.
4. You will receive an acknowledgement within 72 hours and a fix assessment within
   14 days.

## Security model

Eurus is a single-owner, local-first agent runtime. The documented assumptions are:

- The owner runs Eurus on their own machine; other local users are untrusted.
- The web gateway binds to loopback by default and requires a per-installation bearer
  token plus Origin/Referer validation on every state-changing request and WebSocket
  upgrade.
- Approvals are the only path to mutating tools. Autonomous workers cannot approve;
  remote MCP clients cannot approve.
- Mutating tools require a durable write-ahead attempt record. If the record cannot
  be persisted the mutation is aborted (fail closed).
- Browser egress is enforced by a local proxy that resolves and validates every
  destination, including redirects and WebSocket upgrades. Private, loopback,
  link-local, multicast, reserved, and cloud-metadata destinations are refused unless
  the owner explicitly enables `browser.allow_private_addresses`.

## Verified enforcement boundaries

- Tool execution: single boundary path with approval, envelope capabilities, path
  locks, timeouts, and write-ahead recording (`src/eurus/tools/boundary.py`).
- Browser egress: proxy-enforced destination validation (`src/eurus/browser/egress.py`).
- Workspace: path resolution rejects absolute paths, `..`, and any symlink component
  before containment is enforced (`src/eurus/coding/paths.py`).
- Credentials: secrets are read from the environment or encrypted stores, never from
  the config file; provider headers are redacted in gateway responses; child processes
  receive a scrubbed environment.

## Known limitations

See `docs/known-limitations.md`. The two most important ones:

- With `[sandbox] backend = "host"` the shell tool has no filesystem or network
  confinement beyond OS permissions; the container backend is the confined path.
- The coding file tools validate paths then use them non-atomically; a concurrent
  in-workspace process could race a symlink swap. The race-resistant primitives
  exist (`WorkspaceManager.open_read`/`open_write`) but are not yet wired into the
  coding tools.
