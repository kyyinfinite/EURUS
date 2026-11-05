# Known limitations (0.4.0b1)

Honest inventory of what this beta does not do. Each entry is verified against the
code and tests; none are speculative.

## Security

- **Host sandbox backend is unconfined.** With `[sandbox] backend = "host"` (the
  default on Termux; the default elsewhere is `container`), the shell tool runs
  commands with your OS permissions. Approval gating and the scrubbed environment
  still apply, but there is no filesystem or network isolation. The `container`
  backend is the confined path.
- **Coding tools validate then use paths non-atomically.** Path checks and file
  operations are separate steps. A process already running inside the workspace could
  race a symlink swap between them. The race-resistant primitives
  (`WorkspaceManager.open_read` / `open_write` with `dir_fd` and `O_NOFOLLOW`) exist
  but are not yet wired into the coding tools.
- **Remote CDP attach has no egress proxy.** When connecting to an externally started
  Chromium (`browser.cdp_endpoint`), Eurus cannot inject the enforcing proxy flags;
  the request-level guard still blocks forbidden schemes, but destination addresses
  are the browser's own resolution. Use a managed launch for untrusted browsing.
- **MCP server surfaces are allowlist-gated, not approval-gated.** The built-in MCP
  server cannot run interactive approvals; mutating tools are denied to MCP clients
  unless `allow_mutating` is explicitly enabled in the server access policy.
- **Gateway media is single-owner.** Any holder of the gateway token can read any
  session's media; sessions are a UI concept, not security principals. Media IDs are
  unguessable, and the token is required.
- **JSON Schema `format` is annotation-only.** The validator does not enforce
  `format: uri` and friends, matching JSON Schema's default behaviour.

## Reliability

- **No cross-process coordination for the same workspace.** Two Eurus processes
  pointed at one workspace rely on path locks inside a process, not between processes.
- **Egress proxy stops with the manager.** The proxy lives inside the browser manager
  and is cleaned up when the manager closes; a hard process kill leaves it to OS
  socket teardown.
- **Plain-HTTP proxying closes connections after each response** (`Connection:
  close`). HTTPS traffic tunnels normally; visible only as slightly higher
  connection counts on plain-HTTP sites.

## Product

- **No multi-user mode.** One owner per installation. Memory, media, and missions are
  not separated between principals.
- **Autonomous workers cannot approve.** Jobs that require approval fail rather than
  escalate; this is deliberate.
- **Browser downloads require the managed backends.** The `container` sandbox backend
  has no download support wired today.
- **Windows is not tested.** The suite runs on Linux; macOS is best-effort.

## Testing

- Real-browser tests require a local Chromium (`python -m playwright install
  chromium`) and are skipped when unavailable. CI runs the full matrix on Linux only.
- No prior released version exists, so upgrade testing covers schema migration from
  the oldest supported schema and data preservation, not a live old-to-new binary
  upgrade.
