# Google OAuth Setup

Eurus authenticates to Google with a real OAuth 2.0 Authorization Code flow:
browser-based consent, offline access, refresh tokens and automatic token
refresh. Eurus never stores Google passwords, never asks you to paste access
tokens, and never logs token values.

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/> and create (or select) a project.
2. Note the project ID; several Workspace MCP services must be enabled per
   project (see [GOOGLE_WORKSPACE.md](GOOGLE_WORKSPACE.md)).

## 2. Configure the OAuth consent screen

Google Workspace MCP servers use OAuth 2.0, so the consent screen must be
configured before creating a client:

1. Go to **Google Auth Platform > Branding** (or click **Get Started** if it
   is not configured yet).
2. App name: for example `Eurus`.
3. Audience: **Internal** for a Google Workspace organization, otherwise
   **External**.
4. Add a contact email and finish the wizard.
5. If you chose **External**, add your account under **Audience > Test users**
   while the app remains in testing mode.
6. Under **Data Access > Add or Remove Scopes**, paste the scopes for the
   services you plan to enable (listed in
   [GOOGLE_WORKSPACE.md](GOOGLE_WORKSPACE.md) and shown by
   `eurus mcp google enable <service>`).

## 3. Create the OAuth client

Eurus is an installed desktop application:

1. In **Google Auth Platform > Clients**, click **Create Client**.
2. Application type: **Desktop app** (recommended for Eurus). A **Web
   application** client also works if you register an explicit redirect URI.
3. **Important for Google Workspace MCP:** when creating the client, enable
   the **"Use this client for an AI-powered agent"** option. Google's
   Workspace MCP servers require this setting; if it cannot be toggled on an
   existing client, create a new one with the option selected.
4. Copy the generated **Client ID** and **Client secret**.

Desktop-app clients accept loopback redirects, so Eurus binds a temporary
`http://127.0.0.1:<port>/callback` listener during login and no redirect URI
needs to be registered. If you use a Web-application client instead, register
an exact redirect URI and export it (see below).

## 4. Give the credentials to Eurus

Either environment variables (recommended):

```bash
export EURUS_GOOGLE_CLIENT_ID="1234-abc.apps.googleusercontent.com"
export EURUS_GOOGLE_CLIENT_SECRET="GOCSPX-..."
# Optional, Web-application clients only:
export EURUS_GOOGLE_REDIRECT_URI="http://localhost:8912/callback"
```

or a credentials file at `~/.eurus/auth/google/client.json` (create it with
mode 600; Eurus never writes this file itself):

```json
{
  "installed": {
    "client_id": "1234-abc.apps.googleusercontent.com",
    "client_secret": "GOCSPX-..."
  }
}
```

The file also accepts Google's console export shape for web clients
(`{"web": {...}}`) or a flat `{"client_id": ..., "client_secret": ...}`.

## 5. Log in

```bash
eurus mcp google enable gmail     # first enable the services you want
eurus auth google login           # opens the browser
```

Eurus requests only the scopes needed by the enabled services (plus the base
identity scopes), never everything. The flow uses PKCE, a
cryptographically random `state`, and a localhost callback; after consent the
tokens are stored securely (below) and refreshed automatically.

Useful commands:

```bash
eurus auth google status        # accounts, token validity, storage backend
eurus auth google accounts      # list accounts
eurus auth google use <email>   # switch the active account
eurus auth google logout        # revoke + remove the active account
eurus auth google logout --all  # revoke + remove every account
```

## Secure token storage

Eurus picks the first available backend, in this order:

1. **OS keyring** (Secret Service on Linux, Keychain on macOS, Credential
   Manager on Windows) - install with `pip install "eurus-agent[google]"`.
2. **Encrypted file** under `~/.eurus/auth/google/tokens/` - the encryption
   key is derived (PBKDF2-HMAC-SHA256, 600k iterations) from a passphrase you
   provide via `EURUS_GOOGLE_TOKEN_PASSPHRASE` or an interactive prompt. Only
   a random salt is stored next to the ciphertext; the passphrase is never
   written anywhere.
3. **No storage** - if neither backend is usable, login refuses to proceed
   with instructions. Eurus never silently stores plaintext tokens.

Account metadata (emails only, no tokens) lives in
`~/.eurus/auth/google/accounts.json`. Tokens never appear in `mcp.json`,
`google-workspace.json`, logs, or the model context.

## Termux / headless servers

On Termux or headless machines there is usually no OS keyring:

```bash
pkg install python
pip install "eurus-agent[google]"     # brings in cryptography
export EURUS_GOOGLE_TOKEN_PASSPHRASE="a long random passphrase"
eurus auth google login --no-browser  # prints the URL to open elsewhere
```

Keep the passphrase available in the environment for future sessions;
without it the stored tokens cannot be decrypted. `--no-browser` prints the
authorization URL instead of trying to open a browser.

## Rotating or removing credentials

- Logging out revokes the refresh token at Google and deletes local copies.
- Refresh-token rotation is handled automatically when Google issues a new
  refresh token; the stored credential is updated in place.
- A revoked or expired refresh token moves the affected account to
  `reauth_required`; other accounts and other MCP servers keep working. Run
  `eurus auth google login` again to fix it.
