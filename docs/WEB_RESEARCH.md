# Web Research

Agent runs enable three web tools without any search API key:

| Tool | Description |
| --- | --- |
| `web_search` | Public web search through DDGS; returns titles, URLs, and snippets |
| `web_fetch` | Fetches one public HTTP(S) page and extracts readable text and metadata |
| `deep_search` | Searches, fetches up to five sources in parallel, and returns evidence with source URLs |

The optional browser tools (see below) provide richer interaction when the
`browser` extra is installed and `[browser] enabled = true`.

## Security rules

- All tools run through the shared network guard (`src/eurus/security/net_guard.py`).
- Only public HTTP(S) destinations are allowed. Loopback, private, link-local,
  multicast, reserved, and unspecified addresses are rejected before any request
  is made.
- Redirects are validated hop by hop; a redirect to a private or forbidden
  destination is blocked.
- Connections are pinned to the address that was validated at connect time, so
  DNS rebinding between validation and connection cannot reach internal
  services. Unix sockets are rejected outright.
- Page size is capped (2 MB by default) and extracted text is truncated.
- Page content is untrusted data. Tool output repeats this warning to the model
  and page text is never treated as instructions.
- Research answers must cite sources; the completion verifier rejects research
  conclusions that lack citations or web evidence in the run transcript.

## Limits

Web tools require internet access and may be affected by search-engine rate
limits. On Termux, the same tools work over the device's normal network stack;
no browser binaries are required.

## Browser automation

The browser tools are documented in [Browser Automation](BROWSER.md). In
short: enable them in configuration, install either the `[browser]` (Playwright)
or `[browser-cdp]` (raw CDP, Termux-friendly) extra, and the agent gets a
snapshot-driven browser it drives by element reference instead of by guessed CSS
selectors. Screenshots stay confined to the workspace and consequential actions
require approval. When no backend is importable, the tools return an explicit
unavailable-capability message instead of pretending a browser session exists,
and the HTTP tools above remain the fallback.

## Network guard and DNS resolution limits

The HTTP tools (`web_fetch`, `web_search`) connect through a validating
transport that resolves the hostname at connect time, rejects private, local,
CGNAT (100.64.0.0/10), multicast, and unspecified addresses (including
IPv4-mapped IPv6 forms such as `::ffff:127.0.0.1`), and pins the connection to
the validated address, so a DNS rebinding between check and connect does not
help an attacker.

The browser session intercepts every request through a Playwright route guard.
That guard validates the target URL off the event loop (resolution runs in a
worker thread) and aborts forbidden targets before the browser loads them.
Because the browser's own network stack performs the connection, the resolved
IP is not pinned at the transport level the way the HTTP tools pin it: a
hostname that resolves to a public address at guard time could theoretically be
re-resolved to a private address by the browser (a DNS rebinding gap). Playwright
offers no per-request address pinning API; closing this gap would require
routing browser traffic through a local validating proxy. The guard also runs
for subresource requests (scripts, frames, XHR), which keeps the practical
attack surface small.
