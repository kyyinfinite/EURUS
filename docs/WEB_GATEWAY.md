# EURUS Web Gateway

The Web Gateway is a **new interface for the existing EURUS agent**, not a second
implementation of it. It exposes the canonical runtime — the same `AgentEngine`,
`AgentRuntime`, tool boundary, approval service, stores and configuration the TUI
and CLI already use — through a local REST + WebSocket API and a React frontend.

```
Terminal 1:  eurus-tui            (unchanged terminal experience)
Terminal 2:  eurus gateway web    (prints http://127.0.0.1:8712/?token=...)
Browser:     open the URL, chat with the same agent and sessions
```

## Architecture

```
Browser (React + TS + Vite build, served by the gateway process)
   │  REST  /api/...          WebSocket /api/ws (token-authenticated)
   ▼
FastAPI gateway  (src/eurus/gateway/web/)
   │  application service layer (service.py) — orchestration only
   ▼
Canonical EURUS runtime (unchanged)
   ├─ AgentRuntime(require_engine=True)  runtime/application.py
   ├─ AgentEngine (durable runs)         runtime/engine.py
   ├─ build_agent / build_router         agent/factory.py, providers/factory.py
   ├─ ToolExecutionBoundary + approval   tools/boundary.py, gateway/approval.py
   └─ SQLite authority ~/.eurus/data/eurus.sqlite3
        sessions, runs, runtime events, tool attempts, gateway approvals
```

### One runtime, many interfaces

EURUS entry points are separate OS processes; the repository's existing pattern
for this (see `eurus gateway telegram`) is **one `AgentRuntime` per process over
one shared SQLite authority**. The web gateway follows that same pattern:

* **Sessions / messages** live in `SessionStore` — created in the TUI, visible in
  the browser and vice versa (`/sessions` + `/resume <id>` in the TUI).
* **Runs** are durable `Run` records. The browser sees its own runs live (events
  stream from the in-process `AgentEventBridge`) and *observes runs started in
  other interfaces* by tailing the durable `EventStore` (`run_event` frames).
* **One execution loop per session**: the gateway refuses to start a second run
  for a session that already has an active one (HTTP 409), checked both against
  its own registry and the persisted run table.
* **Cancellation** uses the canonical persisted-cancel path
  (`RunStore.request_cancel`), which the engine polls every 0.2 s — this is the
  same mechanism `eurus cancel` uses, and it works across processes.
* **Approvals** for gateway-executed runs are handled by `WebApprovalBroker`
  (extends `gateway.approval.ApprovalBroker`): pending requests are broadcast to
  browsers, resolved via `POST /api/approvals/{token}/resolve`, persisted to the
  `gateway_approval_requests` table, with duplicate protection, expiry and owner
  lockout inherited from the canonical broker. Mutating tools stay fail-closed:
  no approver, no execution. (TUI-run approvals remain the TUI's interactive
  terminal prompt; the two processes do not share an in-flight approval object.)

Additive canonical changes: `AgentRuntime.run(..., run_factory=...)` (pre-allocate
a canonical run id per web turn), `SessionStore.create_session()`, and
`ApprovalBroker._render_prompt()` (override point). Nothing else in the core was
changed.

## CLI usage

```bash
eurus gateway web                 # bind 127.0.0.1:8712, print token URL
eurus gateway web --open          # also open the browser
eurus gateway web --port 9000
EURUS_GATEWAY_TOKEN=... eurus gateway web    # explicit token
EURUS_GATEWAY_APPROVAL_TIMEOUT=300 eurus gateway web
```

The token is generated on first start and stored in `~/.eurus/gateway-web.json`
(mode 0600). Append `?token=<token>` once; the browser app stores it locally and
strips it from the address bar.

### Development (frontend)

```bash
cd frontend
npm ci
npm run dev          # Vite on :5173, proxies /api (incl. WS) to :8712
eurus gateway web    # in another terminal
```

### Production build

```bash
cd frontend
npm ci
npm run build        # tsc --noEmit && vite build -> frontend/dist
eurus gateway web    # serves dist/ at / with SPA fallback
```

Without a frontend build the gateway still serves the API and shows a hint page.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness (host-checked, no token required) |
| GET | `/api/runtime` | Active runs, engine health, clients |
| GET | `/api/dashboard` | Aggregated dashboard payload |
| GET/POST | `/api/sessions` | List / create sessions |
| DELETE | `/api/sessions/{id}` | Delete session |
| GET/POST | `/api/sessions/{id}/messages` | History / send message (202 + run_id; optional `attachments: [media_id]`) |
| POST | `/api/media` | Multipart upload (`session_id` + `file`) → `MediaView` |
| GET | `/api/media/{id}` · `/api/media/{id}/content` | Media metadata / authenticated payload |
| DELETE | `/api/media/{id}` | Remove media row + payload |
| GET | `/api/runs` · `/api/runs/{id}` · `/api/runs/{id}/events` | Run inspection |
| POST | `/api/runs/{id}/cancel` | Persisted-cancellation |
| GET | `/api/approvals` | Pending approvals (redacted arguments) |
| POST | `/api/approvals/{token}/resolve` | Authoritative decision |
| GET/PATCH | `/api/config` | Read / validate + save via `TomlManager` |
| GET | `/api/providers` · POST `/api/providers/{name}/discover` | Provider status/models |
| GET | `/api/tools` · `/api/mcp` | Registry + MCP server status |
| GET/POST/DELETE | `/api/memory…` | Canonical memory store |
| GET | `/api/logs` | In-process redacted ring buffer |
| WS | `/api/ws?token=…` | `agent_event`, `run_event`, `run_status`, `approval_required`, `sessions_changed` |

## Local security

* Loopback-only by default; non-loopback `--host` prints a warning.
* Host-header allowlist (DNS-rebinding protection) on every request and upgrade.
* Bearer-token auth on all API routes and WebSocket upgrades (except the
  read-only `/api/health` probe).
* Origin/Referer validation on all state-changing requests and WebSocket
  upgrades (CSRF); no cookies are used, so the token is never attached
  implicitly.
* Secrets never leave the process: config responses contain API-key **env var
  names**, never values; approval payloads pass through `redact_arguments`;
  the log ring buffer applies `eurus.logs.redact`.
* No filesystem access, no shell access, no tool execution from the frontend —
  the browser can only trigger operations through the canonical services.

## Tests

```bash
python3 -m pytest tests/test_web_gateway_security.py     # auth, host, origin, WS
python3 -m pytest tests/test_web_gateway_api.py          # API surface
python3 -m pytest tests/test_web_gateway_media.py        # media uploads + attachments
python3 -m pytest tests/test_web_gateway_integration.py  # real engine E2E (mocked provider)
scripts/live_gateway_check.sh                            # boots the real server + mock provider
cd frontend && npm test                                   # vitest units (media parsing, gateway store)
```

## Frontend (obsidian workspace redesign)

The React frontend (`frontend/`) is a polished, responsive agent workspace:

* **Welcome screen** (`pages/Home.tsx`) — spacious centered hero (emblem,
  wordmark, subtitle, headline, live status chips), a prominent central
  composer (the same `ChatComposer` as Chat) and shortcut actions. Sending
  creates a canonical session, runs the agent, and deep-links into
  `/chat/<id>`. A restrained workspace overview (runtime health, runs,
  sessions, providers, activity) lives below the fold.
* **Floating chat** (`components/floating/FloatingChat.tsx`) — available on
  every page except `/chat` itself. Desktop: bottom-right panel with session
  picker, expand-to-workspace, live streaming. Mobile: full-screen sheet with
  safe-area handling. It shares the exact react-query caches and WebSocket
  store with the full workspace — one canonical chat system, no duplicates.
* **Chat workspace** (`components/chat/`) — three-pane layout (conversation
  sidebar with search, main conversation, optional context panel). On mobile
  the sidebar and context panel become drawers; the composer stays above the
  keyboard (`100dvh` + safe-area insets).
* **Multimodal composer** — attachment menu (image / document / audio /
  voice note via `MediaRecorder`), drag-and-drop, clipboard image paste,
  per-file upload progress with retry. Files upload to `/api/media`; sending
  attaches canonical media ids.
* **Message renderer** (`components/chat/MessageRenderer.tsx`) — parses the
  `[attach …]` markers embedded by the backend into typed media blocks
  (image viewer with zoom/rotate, audio player with seek, video, document
  cards). Inlined text excerpts from the backend render as collapsible
  monospace blocks.
* **Design system** — Tailwind tokens in `tailwind.config.js` (obsidian /
  bronze / copper / sage palette, restrained radii and shadows) and reusable
  primitives in `components/ui.tsx` (Button, IconButton, Badge, Card, Modal,
  Drawer, Toast, Tooltip, Empty/Loading/Error states, PageHeader).
* **Accessibility** — semantic landmarks, ARIA labels, focus-visible rings,
  focus traps in Modal/Drawer, Escape handling, `prefers-reduced-motion`
  support, ≥40px touch targets on mobile.

### Media pipeline (honest capability model)

`eurus.media.MediaStore` is the canonical store (payloads under
`~/.eurus/media/<session>/`, metadata in the shared SQLite database). Uploads
are validated (20 MB cap, MIME allowlist, magic-byte sniffing, filename
sanitisation) and scoped to a session. Sending a message with attachments
embeds stable `[attach …]` markers plus metadata into the user message:
text-like documents (txt/md/csv/json) are inlined (bounded) so text-only
providers can genuinely work with them; binary media is referenced by
metadata + storage path. No second processing pipeline exists — the agent
loop, verification, approvals and persistence stay authoritative, and the UI
states plainly that current providers are text-only.

## Known limitations

* **Model providers are text-only.** Uploaded images, audio and video are
  stored, previewed and delivered as references (metadata + storage path);
  they are not sent as vision/audio content blocks. Text documents are
  inlined so any provider can read them. Provider-side multimodal support
  would require provider protocol changes and is out of scope.
* Voice notes use the browser `MediaRecorder` API and auto-stop after 60 s;
  browsers without the API show an honest "unavailable" notice.
* Approval requests for runs started in the TUI are resolved in the TUI (its
  approver is the interactive terminal); approvals for gateway runs are shared
  across all browser clients. A cross-process approval service would require
  TUI changes and is deliberately out of scope.
* Runs started outside the browser are *observed* live via the durable event
  store, but the TUI does not live-refresh sessions started in the browser —
  it picks them up through `/sessions` / `/resume`, exactly as it picks up any
  persisted session today.
* A run row left `RUNNING` by a killed process is treated as inactive after a
  2-hour activity window; use `eurus resume` / `eurus history` to reconcile
  interrupted runs (existing tooling).
