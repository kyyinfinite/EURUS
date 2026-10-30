# MCP Integration

Eurus is a native MCP platform: it acts both as an **MCP client** (connecting
to external and Google Workspace servers) and as an **MCP server** (exposing
its own tools to external clients). The MCP SDK (`mcp>=1.27,<2`) is a core
dependency and is installed with Eurus automatically.

## Architecture

```
External MCP Client ──> Eurus MCP Server (stdio / streamable HTTP)
                          └─ auth ─> allowlist ─> ToolRegistry ─> execution boundary

Eurus agent ──> MCPRuntime (one builder for CLI, TUI, gateway, scheduler)
                 ├─ external servers      (EURUS_HOME/mcp.json, unchanged format)
                 ├─ Google Workspace      (integrations/google-workspace.json)
                 └─ security policy       (security/mcp-policies.json)
                      └─ MCPToolManager: per-server connection lifecycles
```

Every entry point (CLI, TUI, Telegram gateway, scheduler) builds its MCP stack
through `eurus.mcp.runtime.MCPRuntime`, so there is exactly one manager and one
policy per process.

## Installation

MCP is installed by default:

```bash
python -m pip install -e .
```

The historical command still works (the extra is now redundant):

```bash
python -m pip install -e ".[mcp]"
```

Optional secure-storage backends for Google OAuth:

```bash
python -m pip install -e ".[google]"   # keyring + cryptography
```

## External MCP servers

Stored in `EURUS_HOME/mcp.json` and managed with:

```bash
eurus mcp list
eurus mcp add-stdio <name> <command> [--arg ...]
eurus mcp add-http <name> <url>
eurus mcp test [name]
eurus mcp remove <name>
```

The config format is unchanged from earlier releases:

```json
{"servers": [{"name": "filesystem", "command": "python", "args": ["server.py"]}]}
```

Supported transports: `stdio` (local processes) and `http` (Streamable HTTP).
HTTP servers must use `https://`; plain `http://` is accepted only for
loopback addresses (`127.0.0.1`, `localhost`, `::1`) so local servers can be
tested.

## Per-server connection lifecycle

Each server has its own connection task, exit stack and discovered tools, with
these states: `disabled`, `disconnected`, `connecting`, `connected`,
`failed`, `reauth_required`.

- Servers connect concurrently; a failing server never disconnects healthy
  ones, and its failure is recorded per server (`eurus mcp doctor`).
- Individual servers can be reconnected (`MCPToolManager.reconnect(name)`);
  a tool call against a disconnected server attempts one automatic reconnect.
- Timeouts are per server (`timeout_seconds`, 1-300s) and cover the
  initialize handshake, tool discovery and each tool call.
- Tools are discovered from the server after connecting and removed when the
  server disconnects.
- If two servers would expose the same tool name, the second server's
  conflicting tools are skipped and the conflict is reported in diagnostics.

## Google Workspace (native provider)

All nine Google Workspace MCP services are built in, disabled by default:

```bash
eurus mcp google list
eurus mcp google enable gmail
eurus mcp google status
eurus mcp google test
eurus mcp google disable gmail
```

See [docs/GOOGLE_WORKSPACE.md](GOOGLE_WORKSPACE.md) for setup and
[docs/GOOGLE_OAUTH.md](GOOGLE_OAUTH.md) for authentication.

## Security model

- External MCP tools are registered with `mutates = true` and
  `requires_approval = true` by default, so every external tool call goes
  through the same fail-closed approval boundary as built-in tools.
- Google tools are risk-classified (`read_only`, `reversible_write`,
  `sensitive_mutation`, `destructive`); sensitive and destructive actions
  always require explicit approval and this cannot be disabled.
- All remote MCP output is wrapped in an untrusted-content guard; retrieved
  emails, documents and server responses are data, never instructions.
- `eurus mcp add-http` does not set authentication headers; provide
  credentials through server configuration you control.
- See [docs/MCP_SECURITY.md](MCP_SECURITY.md) for policies and token
  security.

## Diagnostics

```bash
eurus mcp doctor        # SDK version, server states, policy, Google auth
eurus mcp tools         # tools the connected servers would register
eurus mcp permissions   # show/edit risk policies
eurus mcp test [name]   # connect and report per-server results
```

## Running Eurus as an MCP server

```bash
eurus mcp server start --transport stdio
eurus mcp server start --transport streamable-http --host 127.0.0.1 --port 8765
```

Nothing is exposed by default; tools are allowlisted explicitly with
`--tool <name>` (see `eurus mcp server tools`). See
[docs/MCP_SERVER.md](MCP_SERVER.md).
