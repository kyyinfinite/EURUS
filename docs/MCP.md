# MCP Integration

Eurus supports optional Model Context Protocol (MCP) clients.

## Installation

```bash
python -m pip install -e ".[mcp]"
```

## Configuration

MCP servers are stored in `EURUS_HOME/mcp.json` and managed with:

```bash
eurus mcp list
eurus mcp add-stdio <name> <command> [--arg ...]
eurus mcp add-http <name> <url>
eurus mcp test [name]
eurus mcp remove <name>
```

## Security model

- Review local server commands before connecting; the stdio transport runs a
  local process you configured.
- MCP tools are registered with `mutates = true` and
  `requires_approval = true` by default, so every external tool call goes
  through the same fail-closed approval boundary as built-in tools.
- MCP tool output is treated as untrusted data, never as instructions.
- `eurus mcp add-http` does not set authentication headers; provide credentials
  through the server configuration you control.
- Connections are closed when the CLI or gateway shuts down.
