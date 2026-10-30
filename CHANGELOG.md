# Changelog

All notable changes to Eurus are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While Eurus is pre-alpha (`0.x`), any minor release may include breaking changes to configuration, persistence schemas, CLI behavior or public APIs.

## [Unreleased]

## [0.1.0] - 2026-10-31

**Pre-release.** First tagged version of Eurus, an experimental provider-agnostic AI agent runtime. Expect rough edges and do not rely on it for critical or unattended work.

### Added

#### Runtime and execution
- `AgentEngine` (`src/eurus/runtime/engine.py`) as the single durable execution boundary shared by the CLI, TUI, Telegram gateway and scheduler.
- Explicit run state machine (`QUEUED`, `RUNNING`, `WAITING_INPUT`, `VERIFYING`, `INTERRUPTED`, `COMPLETED`, `FAILED`, `CANCELLED`) with budget tracking, cancellation, token governor and pricing estimates.
- SQLite-backed stores for runs, events, tool attempts and jobs, with additive migrations, WAL mode, a connection pool and off-loop writes.
- Write-ahead tool-attempt records, checkpoints with history snapshots, task-state snapshots, and recovery classification so unresolved side effects are not blindly replayed.
- Resume, cancel and undo (`eurus resume`, `eurus cancel`, `eurus undo`, `/undo`) and runtime inspection (`eurus trace`, `inspect`, `history`, `events`, `metrics`).
- Async tool-calling agent loop with bounded iterations, timeouts, retry and fallback handling, and interruption support.
- Planner and structured planner, step and completion verification that separates supported results from unverified claims, and context compaction.
- Sub-agent delegation (`delegate_task`, `delegate_code_task`) and a graph scheduler for orchestration roles.

#### Model providers
- Protocols: OpenAI-compatible, Anthropic, Gemini (REST), plus optional official Groq and Google GenAI SDK adapters.
- Built-in provider catalog: OpenAI, Anthropic, Gemini, Groq, NVIDIA NIM, OpenRouter, DeepSeek, Mistral, Together, xAI and Ollama, plus user-defined `[providers.*]` entries.
- Provider router with configurable fallback chains, Retry-After-aware cooldown, retries with jitter, streaming, and failure classification.
- `eurus providers` and `eurus diagnose` for connectivity diagnosis (for example edge, model and region blocks).

#### Tools and workflows
- Shell tool with head-and-tail bounded output and accurate exit-code and timeout reporting; host and container sandbox backends.
- Coding workspace tools: guarded file operations, stale-patch detection, patch sets, git tools, validation, project index, worktrees, checkpoints and multi-file workflows.
- Web research: `web_search`, `web_fetch` and `deep_search`.
- Snapshot-driven browser automation with 18 `browser_*` tools, a pure-Python CDP backend (Termux-friendly) and an optional Playwright backend, plus a risk and security policy.
- Document workflows for DOCX, XLSX and PPTX (optional `[documents]` extra).
- Utility tools (`calculate`, `get_time`) and a `viewport` tool.

#### MCP platform
- MCP client with independent per-server connection tasks, concurrent connects, per-server timeouts, single-server reconnection, and states `disabled`, `disconnected`, `connecting`, `connected`, `failed` and `reauth_required`. A failing server never disconnects healthy ones.
- `eurus mcp list|add-stdio|add-http|test|remove|tools|doctor|permissions` for external servers (`mcp.json`, stdio and Streamable HTTP; plain `http://` only on loopback).
- Eurus as an MCP server over STDIO and Streamable HTTP (`eurus mcp server start|tools`) with an explicit tool allowlist, bearer-token authentication, loopback-only default, and refusal of unauthenticated remote binds.
- `MCPRuntime`, a single builder shared by the CLI, TUI, gateway and scheduler.
- Security policy engine with `read_only`, `reversible_write`, `sensitive_mutation`, `destructive` and `unknown` classes. Sensitive and destructive actions always require approval and this cannot be disabled. External servers keep the fail-closed approval default.
- Untrusted-content envelope around every remote MCP result.
- `mcp>=1.27,<2` is now a core dependency; the `[mcp]` extra is kept for compatibility.

#### Google Workspace (built-in MCP provider)
- Native provider for all nine official Google Workspace MCP services: Gmail, Drive, Docs, Sheets, Slides, Calendar, Chat, People and Classroom. All are disabled by default, and only the scopes of enabled services are requested.
- Live tool discovery (nothing hard-coded); tools are named `mcp_google_<service>_<tool>`.
- Per-service enable, disable and timeouts, with failure isolation per service and per account.
- `eurus mcp google list|enable|disable|status|test`.
- Google OAuth 2.0 authorization-code flow with PKCE (S256), random single-use state, loopback callback, offline access, automatic refresh, refresh-token rotation, revocation and multi-account switching (`eurus auth google login|logout|status|accounts|use`).
- Credential storage in the OS keyring, or a PBKDF2-HMAC-SHA256 plus Fernet encrypted file protected by `EURUS_GOOGLE_TOKEN_PASSPHRASE`. There is no silent plaintext fallback. New `[google]` extra (`keyring`, `cryptography`).
- Guides: `docs/GOOGLE_OAUTH.md`, `docs/GOOGLE_WORKSPACE.md`, `docs/MCP.md`, `docs/MCP_SERVER.md`, `docs/MCP_SECURITY.md`, `docs/TROUBLESHOOTING.md`.

#### Memory, sessions and learning
- Local SQLite session history with FTS5 keyword search (`eurus sessions`, `session_search`).
- Trust-tiered long-lived memory with typed records, scopes, duplicate rejection, credential-pattern checks and budgeted prompt injection (`eurus memory`, `remember_memory`, `recall_memory`, `replace_memory`, `forget_memory`).
- Privacy export and purge (`eurus privacy export|purge`).
- Lesson capture, quarantined skill drafts and owner-approved promotion (`eurus skills list|show|validate|promote|patch`, `eurus learn consolidate`).
- Eight built-in skills: `antislop`, `antislop-code`, `antislop-copywriting`, `antislop-layoutmobile`, `antislop-ui`, `html-layout-canvas`, `office-artifacts` and `presentation-design`.

#### Interfaces
- CLI: `ask`, `run`, `chat`, `tui`, `config`, `providers`, `diagnose`, `doctor`, `bootstrap`, `setup`, `schedule`, `gateway`, `browser`, `eval`, `benchmark` and the command groups above.
- Terminal UI (`eurus-tui`) with slash commands, a model picker, session resume, nine built-in themes (`eurus`, `midnight`, `paper`, `mono`, `nord`, `dracula`, `gruvbox`, `tokyo-night`, `catppuccin`), user-defined themes (`eurus theme list|use|new|path`), per-theme suggested fonts, a bundled-fonts folder (`/font`, `eurus font`) and glyph sets (`unicode`, `ascii`, `nerd`, `auto`).
- Telegram gateway with allowlist, approval prompts, rate limiting and persistent reminders (`create_reminder`, `list_reminders`, `reschedule_reminder`, `cancel_reminder`).
- Scheduler daemon and scheduled runs (`eurus schedule add|list|enable|disable|daemon|history`).

#### Environment manager
- `eurus bootstrap` (`--check`, `--quick`, `--repair`, `--update`) and `eurus doctor` detect the platform (Termux, Debian/Ubuntu proot, native Linux, containers and VPS), install optional extras, write the `~/.eurus/` layout (directories `0700`, secrets `0600`), back up before repair, and record state in `~/.eurus/bootstrap/state.json`.
- `eurus setup provider|browser|mcp|telegram|startup|config` and autostart installers for systemd, Termux:Boot and cron.

#### Security
- Fail-closed approval boundary for mutating tools: with no approver available, the operation is denied.
- Action journal, secret redaction before persistence, and a network guard that rejects non-HTTP(S) schemes and local or private destinations on the guarded path.

#### Quality and project
- Deterministic evaluation suite (`eurus eval --compare-learning`) and offline benchmarks under `benchmarks/`.
- Test suite under `tests/` and a CI workflow for lint, format, type checks, tests, packaging and smoke tests on Python 3.11 to 3.13.
- Documentation under `docs/`, `CONTRIBUTING.md`, `SECURITY.md` and `ENGINEERING_REPORT.md`; MIT license.
- Optional extras: `web`, `groq`, `genai`, `sdk`, `mcp`, `google`, `telegram`, `browser`, `browser-cdp`, `documents` and `dev`.

### Changed
- `README.md` rewritten with architecture diagrams, a Google Workspace section and a command reference; the broken `BOOTSTRAP_*.md` links were removed.

### Fixed
- Type-checking cleanups in the bootstrap and CLI modules (typed `CONFIG_UNSET` sentinel, narrowed optional arguments, corrected imports and loop-variable reuse).

### Known limitations
- **Pre-alpha.** APIs, configuration, persistence schemas and behavior may change without notice.
- **Live Google Workspace integration has not been verified** against real Google endpoints. It is covered by mock MCP and mock OAuth servers only. The servers are part of Google's Developer Preview, and Chat and Classroom require Workspace accounts.
- **No container isolation on Termux.** Commands run on the host and the approval gate is the only control before execution.
- Memory retrieval is lexical (hashed n-gram and FTS5), not semantic embeddings.
- The built-in MCP server exposes registry tools only, not prompts or resources.
- Telegram reminder delivery is at-least-once: a crash between Telegram accepting a message and SQLite recording it can produce a duplicate.
- Google tool risk classification is name-heuristic based; unmatched tools fail closed.
- Local test, ruff and mypy results depend on optional dependencies and platform; check the CI run for the tagged commit before relying on a release.

[Unreleased]: https://github.com/kyyinfinite/eurus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kyyinfinite/eurus/releases/tag/v0.1.0
