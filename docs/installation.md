# Installation

## Requirements

- Python 3.11, 3.12, or 3.13
- Node.js >= 22.19.0 (only for building the web frontend from source; matches
  `frontend/package.json` engines and CI)

## Verify a release artifact

If you received `eurus-0.4.1.zip`, `eurus_agent-0.4.1-py3-none-any.whl`, or
`eurus_agent-0.4.1.tar.gz` together with a `SHA256SUMS.txt` file, confirm the
artifacts are intact before installing:

```bash
sha256sum -c SHA256SUMS.txt
```

Every line must report `OK`. A mismatch means the artifact is corrupted or was
replaced; do not install it.

## Install the package

From a release wheel:

```bash
python -m pip install "eurus_agent-0.4.1-py3-none-any.whl[web-gateway]"
```

From a source checkout (or the unzipped source archive), build the frontend
first when the web UI should be embedded, then install the local tree (not
PyPI):

```bash
cd frontend
npm ci && npm run build
cd ..
python -m pip install ".[web-gateway]"
```

Building the frontend is optional; without `frontend/dist` the wheel installs
in API-only mode and the gateway serves no web UI.

From PyPI:

```bash
python -m pip install eurus-agent
```

Optional extras pull in the integrations you want:

```bash
python -m pip install "eurus-agent[web]"          # web search and page extraction
python -m pip install "eurus-agent[telegram]"     # Telegram gateway
python -m pip install "eurus-agent[web-gateway]"  # local web gateway server
python -m pip install "eurus-agent[browser]"      # Playwright browser automation
python -m pip install "eurus-agent[documents]"    # Office document tools
python -m pip install "eurus-agent[google]"       # Google Workspace MCP auth
```

For browser automation also install the Chromium binary:

```bash
python -m playwright install chromium
```

## First run

1. Create a minimal configuration:

   ```bash
   eurus config init
   ```

   or copy `eurus.example.toml` to `~/.eurus/config.toml` and edit it. At minimum set
   `agent.model` to a `provider/model` pair you have credentials for.

2. Put provider API keys in `~/.eurus/.env` (never in the config file):

   ```bash
   mkdir -p ~/.eurus && chmod 700 ~/.eurus
   printf 'OPENAI_API_KEY=sk-...\nGROQ_API_KEY=gsk_...\n' > ~/.eurus/.env
   chmod 600 ~/.eurus/.env
   ```

   `eurus config init` writes only `config.toml`; the `.env` file stays yours to
   manage. Keys referenced by `api_key_env` are read from `~/.eurus/.env` or the
   process environment.

3. Validate the setup:

   ```bash
   eurus doctor
   eurus mcp doctor
   ```

4. Start your first session:

   ```bash
   eurus run "list the files in this directory and summarize the project"
   ```

   The interactive TUI is available with `eurus-tui`.

## Database

Eurus creates and migrates its SQLite database automatically at
`~/.eurus/data/eurus.sqlite3` on first use. No manual migration step is required.
See `docs/upgrade-guide.md` for version upgrades.

## Local web gateway

```bash
python -m pip install "eurus-agent[web-gateway]"
eurus gateway web
```

The gateway binds to `127.0.0.1:8712` by default and prints an access token that is
generated once and stored with `0600` permissions in `~/.eurus/gateway-web.json`.
The token stays valid until you delete that file, so treat it like a password.
Official release wheels bundle the built web UI, so the frontend is served from the
installed package with no extra steps.

### How the web UI is located

The gateway resolves its static directory in this order and uses the first match that
contains an `index.html`:

1. `EURUS_GATEWAY_STATIC` — an explicit directory. Absolute paths are used as given;
   relative paths are resolved against the current working directory.
2. A directory passed by the component that started the gateway (internal use).
3. Assets bundled inside the installed wheel (`eurus/gateway/web/static`).
4. `frontend/dist` inside a source checkout (development installs).

If none is found the gateway starts in API-only mode: it logs a warning, prints the
limitation in the startup banner, reports `"web_ui": {"available": false}` from
`GET /api/health`, and returns an explanatory `503` page for frontend routes. API
routes are unaffected. To restore the UI from a source checkout:

```bash
git clone <repository> && cd eurus
cd frontend && npm ci && npm run build && cd ..
python -m pip install ".[web-gateway]"
```

or point the gateway at an existing build:

```bash
EURUS_GATEWAY_STATIC=/path/to/frontend/dist eurus gateway web
```

### Verify the web UI

```bash
curl -s "http://127.0.0.1:8712/api/health?token=$TOKEN"
```

`web_ui.available` is `true` and `web_ui.source` names where the assets came from
(`environment`, `explicit`, `packaged`, or `repository`).

## Verify the installation

```bash
eurus --version
eurus --help
eurus mcp list
```

If any of these fail, run `eurus doctor` and follow its instructions.
