# Clean Room

Eurus is implemented from scratch. Its design is informed by studying open-source
agent projects, not by copying their code.

## Process

- The runtime, tool boundary, persistence layer, providers, TUI, and gateway are
  original implementations in this repository.
- Prior art is used to understand problems (loop control, approval UX, sandbox
  patterns, checkpointing), not as a source of code.
- No third-party agent framework is embedded or vendored. Dependencies are the
  lightweight set declared in `pyproject.toml`: httpx, click, rich, pygments,
  prompt_toolkit, ddgs, trafilatura, and tzdata, with optional extras for
  Groq/Google SDKs, MCP, Telegram, browser automation, and document formats.

## Scope

The clean-room claim covers `src/eurus` and `tests`. Skill descriptions under
`src/eurus/skills/builtin` are prompt assets, not code, and are written for this
project.
