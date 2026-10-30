# Eurus v0.1.0 (pre-release)

First tagged pre-release of **Eurus**, an experimental, provider-agnostic AI agent runtime for the terminal, built with Android Termux and small servers in mind.

> [!WARNING]
> Pre-alpha. APIs, configuration and persistence schemas may change without notice. Review every proposed or executed change and do not use it for critical or unattended work.

## Highlights

- **Durable runtime.** One `AgentEngine` boundary with SQLite run, event, tool-attempt and job stores, checkpoints, resume and recovery classification.
- **Provider-agnostic.** OpenAI-compatible, Anthropic, Gemini and optional Groq / GenAI SDKs, with fallback chains and cooldown. Built-in catalog: OpenAI, Anthropic, Gemini, Groq, NVIDIA NIM, OpenRouter, DeepSeek, Mistral, Together, xAI, Ollama.
- **Native MCP platform.** MCP client with per-server lifecycle, MCP server mode, risk-classified tool policy and an untrusted-content guard.
- **Built-in Google Workspace.** Gmail, Drive, Docs, Sheets, Slides, Calendar, Chat, People and Classroom through Google's managed MCP endpoints, with OAuth 2.0 + PKCE and keyring or encrypted token storage. Disabled by default.
- **Coding, web and browser tools.** Guarded workspace edits, git, undo, web search / fetch, and snapshot-driven browser automation (CDP for Termux, Playwright for PC/VPS).
- **Terminal UI.** Slash commands, model picker, 9 built-in themes, custom themes, fonts and glyph sets.
- **Telegram gateway, scheduler and reminders**, long-lived memory (FTS5), skill learning with owner-approved promotion, and an environment manager (`eurus bootstrap`, `eurus doctor`).

## Install

```bash
git clone https://github.com/kyyinfinite/eurus.git
cd eurus
git checkout v0.1.0
python -m venv .venv && source .venv/bin/activate
pip install -e .
eurus bootstrap --check
```

## Known limitations

- Google Workspace integration is verified against mock servers only; live behavior depends on Google's Developer Preview and your account type.
- On Termux there is no container isolation; the approval gate is the only control before execution.
- Memory retrieval is lexical, not semantic.

Full details: [CHANGELOG.md](CHANGELOG.md) · Docs: [README](README.md) · Security: [SECURITY.md](SECURITY.md)
