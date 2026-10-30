# Browser Automation

Eurus can drive a real Chromium browser: navigate pages, read an accessibility
snapshot with stable element references, click, type, select, scroll, wait,
manage tabs and dialogs, capture screenshots, upload and download files, and
extract structured data. The browser is an optional integration and is disabled
by default.

## How it works

The agent never invents CSS selectors as the normal interaction path. It reads
a compact snapshot of interactive elements and acts by reference:

```
page "Dashboard" at https://example.com/app
Interact using element references from the latest snapshot; references may
change when the page changes.
[e1] heading1 "Dashboard"
[e2] link "Home"
[e3] button "Create project"
[e4] textbox "Project name"
[e5] checkbox "Public"
[e6] button "Submit"
```

Every tool result that changes the page returns a fresh snapshot (or a compact
diff), so the model always works from current references. When a reference is
no longer valid, the tool returns a clear `stale reference` error and the model
re-snapshots.

## Backends

`browser.backend` selects the driver:

| Backend | Extra | Platform | Notes |
|---|---|---|---|
| `cdp` | `[browser-cdp]` (websockets) | Termux, Linux, any OS | Launches (or connects to) Chromium and speaks the Chrome DevTools Protocol directly. No Playwright, Node.js, Docker, or compiler. Default on Termux. |
| `playwright` | `[browser]` | Linux, PC, VPS | Full Playwright driver with auto-waiting locators. Not available on Android. |
| `remote` | `[browser-cdp]` | anywhere | Connects to an externally managed Chromium via `browser.cdp_endpoint`; Eurus never owns or kills that process. |
| `attach` | `[browser-cdp]` | anywhere | Attaches to an existing Chrome started with `--remote-debugging-port`. |
| `auto` | — | — | Default. Picks `cdp` on Termux, `playwright` when importable, otherwise `cdp`. |

### Termux setup

```bash
pkg install x11-repo
pkg install chromium
pip install -e ".[browser-cdp]"
```

Enable the browser in `~/.eurus/config.toml`:

```toml
[browser]
enabled = true
# backend = "auto" is enough; it resolves to "cdp" on Termux.
```

The Chromium binary is auto-detected (`chromium-browser`/`chromium` on PATH,
`$PREFIX/bin`, or the Playwright cache). Override with
`browser.chromium_executable` or the `EURUS_CHROMIUM` environment variable.

### PC / VPS setup

```bash
pip install -e ".[browser]"
playwright install chromium
```

Or use the CDP backend with a system Chromium; both paths share the same tools.

### Remote or already-running Chromium

Start Chrome with a debugging port, then point Eurus at it:

```toml
[browser]
enabled = true
backend = "remote"                # or "attach"
cdp_endpoint = "http://127.0.0.1:9222"
```

Eurus attaches read-only to the process: it never closes a browser it does not
own.

## Tool reference

| Tool | Purpose | Approval |
|---|---|---|
| `browser_navigate` | Open a public HTTP(S) URL; returns the snapshot | no |
| `browser_snapshot` | Capture the element inventory with references | no |
| `browser_click` | Click an element by reference (`element: "e3"`) | yes |
| `browser_type` | Type into a referenced field; `{{secret:NAME}}` placeholders supported | yes |
| `browser_press_key` | Enter, Tab, Escape, arrows, `Control+a`, ... | yes |
| `browser_select_option` | Select dropdown options by label or value | yes |
| `browser_hover` | Hover an element | no |
| `browser_scroll` | Scroll up/down/left/right/top/bottom | no |
| `browser_wait_for` | Wait for text, its disappearance, URL change, network idle, or time | no |
| `browser_back` | History back | no |
| `browser_tabs` | List / select / open / close tabs; popups are tracked automatically | no |
| `browser_handle_dialog` | Arm accept/dismiss for the next JS dialog | yes |
| `browser_screenshot` | Save a PNG into the workspace | yes |
| `browser_upload` | Attach workspace files to a file input | yes |
| `browser_download` | Download a URL into the workspace download dir | no |
| `browser_extract` | Named fields by reference or selector; whole tables as JSON | no |
| `browser_read` | Legacy: read one element by CSS selector | no |
| `browser_close` | Close this run's pages (the process stays for later runs) | no |

All browser tools run serialized against the shared page (`parallel_safe =
false`), and every tool call is bounded by the configured timeouts.

## Security model

- **URL policy**: HTTP/HTTPS only; `file:`, `chrome:`, `about:`, `data:`,
  `javascript:`, `blob:` and friends are rejected. Localhost, private-network
  and cloud-metadata addresses (`169.254.169.254`, `metadata.google.internal`,
  `fd00:ec2::254`, Alibaba `100.100.100.200`) are blocked. DNS resolves off the
  event loop through a bounded TTL cache, and every resolved address is
  validated before the navigation proceeds. A per-request guard (Playwright
  route interception or CDP `Fetch` interception) re-checks every subresource
  URL.
- **Domain policy**: `blocked_domains` always rejects; when `allowed_domains`
  is non-empty it becomes an allowlist. Listing `localhost` or another local
  host in `allowed_domains` is the explicit opt-in for local testing.
- **Consequential actions**: clicks, typing, key presses, option selection,
  dialog handling, screenshots, and uploads always require owner approval.
  Risk classification adds context to the result (for example a submit button
  is reported as consequential), but approval is not derived from button labels
  alone — the tools fail safe by asking. These tools can never be session-
  scoped; every action is approved individually.
- **Credentials**: put `{{secret:NAME}}` in typed text; Eurus replaces it with
  the value of `EURUS_SECRET_NAME` at execution time. The secret never appears
  in tool results, snapshots, or logs, and typed secrets are redacted from all
  page text returned to the model.
- **Profile**: Eurus uses its own persistent profile at
  `~/.eurus/browser/profile` (override with `browser.profile_dir`). It never
  reads the user's normal Chrome profile. Use `eurus browser login <url>` for a
  manual login handoff: a visible Chromium opens the Eurus profile, you log in,
  and the cookies persist for later agent runs.

## CLI and TUI

```bash
eurus browser status   # effective configuration and detected Chromium
eurus browser start    # verify the backend can start and stop cleanly
eurus browser stop     # explains the lifecycle (processes close with the run)
eurus browser login [url] [--timeout N]  # manual login handoff
eurus browser config   # dump every browser.* option
eurus browser test [--network]           # self-test; --network also loads example.com
```

Inside the TUI, `/browser` shows the backend, connection state, process
ownership, profile, download directory, and active runs; `/browser stop` shuts
the browser down until the next tool call.

## Configuration reference

All keys live under `[browser]`; every key is optional and backwards
compatible:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch; tools register only when true |
| `backend` | `auto` | `auto` / `cdp` / `playwright` / `remote` / `attach` |
| `cdp_endpoint` | `""` | Required for `remote` and `attach` |
| `chromium_executable` | `""` | Auto-detected when empty |
| `profile_dir` | `""` | Defaults to `~/.eurus/browser/profile` |
| `headless` | `true` | Set `false` for visible windows (login handoff) |
| `max_page_bytes` | `2000000` | Page size bound (100000–20000000) |
| `navigation_timeout_seconds` | `30.0` | Navigation bound (5–300) |
| `interaction_timeout_seconds` | `10.0` | Click/type/select bound (1–120) |
| `startup_timeout_seconds` | `25.0` | Chromium launch/connect bound (5–120) |
| `max_screenshot_width` / `max_screenshot_height` | `1280` / `800` | Screenshot viewport bounds |
| `max_snapshot_tokens` | `3000` | Snapshot budget in tokens (~4 chars/token) |
| `allowed_domains` / `blocked_domains` | `[]` | Domain policy; supports `*.example.com` |
| `download_dir` | `""` | Defaults to `<workspace>/downloads` |
| `max_download_mb` | `50` | Download size bound (1–500) |
| `dns_cache_ttl_seconds` | `60.0` | DNS cache TTL (1–3600) |
| `max_sessions` | `3` | Concurrent agent-run browser sessions |
| `session_idle_ttl_seconds` | `900.0` | Idle run sessions are closed after this |

## Known limitations

- The CDP backend drives Chromium through the DevTools Protocol directly; very
  exotic widgets may behave slightly differently than under Playwright. Both
  backends are covered by the same integration tests.
- Subresource SSRF protection re-validates each request URL, but the final DNS
  resolution is performed by Chromium itself; a DNS-rebinding gap remains for
  third-party subresources. First-party navigation uses the validated policy
  above. Run Eurus on a trusted network for sensitive work.
- Downloads initiated by clicks (not by URL) are not intercepted; use
  `browser_download` with the direct URL.
- The login handoff needs a display; on headless servers use Xvfb or run the
  login on your phone/PC with a shared `profile_dir` on mounted storage.
