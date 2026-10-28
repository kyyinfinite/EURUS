# Roadmap

Eurus is pre-alpha. Features and interfaces may change without notice.

## Current focus

- Harden the durable runtime: verification evidence, resume safety, and budget
  honesty across every entry point.
- Keep the dependency footprint small enough for Termux, small VPS instances,
  and Pterodactyl containers.
- Grow the deterministic evaluation suite so regressions are caught without
  paid model calls.

## Recently landed (stability hardening)

- Accurate tool-outcome reporting: shell exit codes and timeouts flow through
  `ToolOutput(is_error, status)`; history repair synthesizes missing tool
  results; `text_reset` prevents duplicated retry text; every terminal path
  closes task state and records lessons.
- Durable attempts with write-ahead pending rows, history snapshots inside
  checkpoints, observable checkpoint failures, WAL SQLite with a connection
  pool and off-loop writes, and bounded engine event buffers.
- Provider cooldown in the router (Retry-After aware), head+tail bounded
  subprocess readers on host and podman sandboxes, LLM compaction fed the
  original transcript instead of the extractive digest, boundary lock-map
  cleanup, and logging for previously silent fallbacks.

## Planned work

- Richer deterministic verifiers for more task classes, selected by plan step
  and changed files rather than objective keywords alone.
- Session-scoped approvals for narrowly defined, safe action patterns with
  expiration and revocation.
- Optional remote embedding providers for memory retrieval (today the
  hybrid provider only does fuzzy lexical matching over hashed n-gram
  vectors, which is not semantic), with automatic fallback to the local
  hybrid provider.
- More orchestration roles on top of the graph scheduler, all behind the same
  trusted integration boundary used by coder delegation.

## Non-goals

- Replacing the human approver for mutating actions.
- Mandatory heavyweight infrastructure: Docker-only sandboxes, vector databases,
  or native compilation requirements.
- Treating model, subagent, or web claims as proof of success.
