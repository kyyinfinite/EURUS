# Troubleshooting

## `mcp doctor`

Start here:

```bash
eurus mcp doctor
```

It reports the MCP SDK version, every configured server with its state
(`disabled` / `disconnected` / `connecting` / `connected` / `failed` /
`reauth_required`), the policy file, Google OAuth status and any runtime
warnings.

## Common problems

### "Google OAuth client is not configured"

Set `EURUS_GOOGLE_CLIENT_ID` and `EURUS_GOOGLE_CLIENT_SECRET`, or create
`~/.eurus/auth/google/client.json` - see [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).
Eurus works normally without Google credentials; only the Google services are
unavailable.

### "no secure credential storage available"

Login needs a keyring or an encryption passphrase:

```bash
pip install "eurus-agent[google]"        # keyring + cryptography
export EURUS_GOOGLE_TOKEN_PASSPHRASE="…" # or answer the interactive prompt
```

On systems without a keyring daemon (Termux, headless servers), the
passphrase environment variable is the supported path.

### "cannot decrypt stored tokens: wrong passphrase or corrupted store"

The `EURUS_GOOGLE_TOKEN_PASSPHRASE` in the environment does not match the one
used when the tokens were stored. Restore the original passphrase or log in
again.

### A Google service shows state `failed`

1. Check the service is enabled in your Google Cloud project
   (`gcloud services enable …mcp.googleapis.com`) - see
   [GOOGLE_WORKSPACE.md](GOOGLE_WORKSPACE.md).
2. Check the Developer Preview enrollment for the Workspace MCP program.
3. Check the OAuth client has the **AI-powered agent** option enabled.
4. Chat requires a Workspace account and a configured Chat app; Classroom
   requires an Education account.
5. Re-authorize: `eurus auth google login`.

Only the failing service is affected; other services and MCP servers keep
working.

### State `reauth_required`

The refresh token was revoked or expired (password change, security review,
revocation). Run `eurus auth google login` again. Other accounts are
unaffected.

### "tool '…' is not allowlisted on this MCP server"

You are exposing Eurus as an MCP server; add the tool with `--tool <name>` or
in `security/server-access.json`. Mutating tools additionally need
`allow_mutating: true`. See [MCP_SERVER.md](MCP_SERVER.md).

### MCP tool call timed out

Per-server `timeout_seconds` (1-300s, default 30/45) bounds each call. Raise
it in `mcp.json` (`timeout_seconds`) or in
`integrations/google-workspace.json`.

### stdio server exits immediately

`eurus mcp test <name>` shows the per-server error. Common causes: the
command does not exist, the server script errors on startup, or the server
speaks a different protocol version.

### "HTTP MCP servers must use an https:// URL"

Plain `http://` is only accepted for `127.0.0.1`, `localhost` and `::1`.
Remote servers must use TLS.

## Termux notes

- Install: `pkg install python && pip install eurus-agent`.
- No OS keyring: export `EURUS_GOOGLE_TOKEN_PASSPHRASE` before
  `eurus auth google login --no-browser`.
- The browser may not open automatically; `--no-browser` prints the URL to
  open on any device.
- Playwright is unavailable on Termux; use the CDP backend
  (`browser-cdp` extra) with a system Chromium if you need browser tools.
