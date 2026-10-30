# Eurus MCP Server

Eurus can expose its own tools to external MCP clients over STDIO or
Streamable HTTP. External clients go through the same chain as everything
else in Eurus:

```
External MCP client
  -> authentication (HTTP)
  -> tool allowlist
  -> ToolRegistry (schema validation)
  -> execution boundary
  -> Eurus tools
```

The ToolRegistry and the existing execution boundary are never bypassed, and
no tool is exposed unless you explicitly allowlist it.

## STDIO (local)

```bash
eurus mcp server start --transport stdio --tool read_file --tool search_memory
```

STDIO is intended for a parent process (an IDE, another agent) that launches
Eurus; it adds no authentication overhead because the parent already controls
the process.

## Streamable HTTP

```bash
eurus mcp server start \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8765 \
  --tool read_file \
  --token "$(eurus-mcp-token-of-your-choice)"
```

- Binds to `127.0.0.1:8765` by default and serves MCP at `http://<host>:<port>/mcp`.
- HTTP always requires a bearer token: pass `--token` or set
  `EURUS_MCP_SERVER_TOKEN`. If neither is set on a loopback bind, Eurus
  generates a one-time token and prints it to stderr.
- `--no-auth` (loopback only) disables authentication for local scripting.
- Binding to a non-loopback address requires `--token` (or the environment
  variable); unauthenticated remote endpoints are refused at startup.

Clients authenticate with `Authorization: Bearer <token>`.

## Choosing exposed tools

Nothing is exposed by default. Options:

- `--tool <name>` (repeatable) for one-off exposure.
- `~/.eurus/security/server-access.json` for a persistent policy:

```json
{
  "allowed_tools": ["read_file", "search_memory"],
  "allowed_patterns": ["memory_*"],
  "allow_mutating": false
}
```

- `eurus mcp server tools` lists every registry tool with its
  mutating/approval flags.

Mutating or approval-gated tools are refused even when allowlisted unless
`allow_mutating` is explicitly `true` (or `--allow-mutating`): external MCP
clients cannot approve actions interactively, so the fail-closed default is
to deny them. Credentials (Google tokens, account data) are never exposed as
tools.

## Behavior

- Unknown tools and non-allowlisted tools return a clear access-denied error.
- Arguments are validated against each tool's JSON schema before execution.
- Invalid arguments return the validation error; tool failures return the
  tool's error output.
- The server shuts down gracefully on SIGINT/SIGTERM (HTTP) or stdin close
  (STDIO).
