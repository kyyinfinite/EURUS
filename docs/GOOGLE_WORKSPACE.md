# Google Workspace Services

Eurus ships with a native provider for Google's official Workspace MCP
servers. There is nothing to clone or run locally: Eurus connects to Google's
managed endpoints and authenticates with your Google account (see
[GOOGLE_OAUTH.md](GOOGLE_OAUTH.md)).

> **Developer Preview.** The Google Workspace MCP servers are part of the
> [Google Workspace Developer Preview Program](https://developers.google.com/workspace/guides/configure-mcp-servers).
> Availability, tool sets and verification requirements may change; not every
> Google account or project has access to every service. Eurus reports
> per-service connection errors instead of failing the whole MCP stack.

## Services and endpoints

| Service | Endpoint | OAuth scopes |
| --- | --- | --- |
| Gmail | `https://gmailmcp.googleapis.com/mcp/v1` | `gmail.readonly`, `gmail.compose` |
| Google Drive | `https://drivemcp.googleapis.com/mcp/v1` | `drive.readonly`, `drive.file` |
| Google Docs | `https://docsmcp.googleapis.com/mcp/v1` | drive scopes + `documents.readonly`, `documents` |
| Google Sheets | `https://sheetsmcp.googleapis.com/mcp/v1` | drive scopes + `spreadsheets.readonly`, `spreadsheets` |
| Google Slides | `https://slidesmcp.googleapis.com/mcp/v1` | drive scopes + `presentations.readonly`, `presentations` |
| Google Calendar | `https://calendarmcp.googleapis.com/mcp/v1` | `calendar.calendarlist.readonly`, `calendar.events.freebusy`, `calendar.events.readonly` |
| Google Chat | `https://chatmcp.googleapis.com/mcp/v1` | `chat.spaces.readonly`, `chat.memberships.readonly`, `chat.messages.readonly`, `chat.messages.create`, `chat.users.readstate`, `chat.users.spacesettings` |
| Google People | `https://people.googleapis.com/mcp/v1` | `directory.readonly`, `userinfo.profile`, `contacts.readonly` |
| Google Classroom | `https://classroommcp.googleapis.com/mcp/v1` | the seven `classroom.*` scopes listed by `eurus mcp google enable classroom` |

All nine services are included in Eurus but **disabled by default**, and only
the scopes of enabled services are requested during login.

## Enable the backing services in Google Cloud

The MCP services must be enabled per project (replace `PROJECT_ID`):

```bash
gcloud services enable gmailmcp.googleapis.com \
  drivemcp.googleapis.com \
  docsmcp.googleapis.com \
  sheetsmcp.googleapis.com \
  slidesmcp.googleapis.com \
  calendarmcp.googleapis.com \
  chatmcp.googleapis.com \
  people.googleapis.com \
  classroommcp.googleapis.com \
  --project=PROJECT_ID
```

Enable only the services you plan to use; the corresponding consent-screen
scopes must be added as described in [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).

### Google Chat specifics

The Chat MCP server needs a Chat app in the same project:

1. **Google Chat API > Manage > Configuration**, create an app named
   `Chat MCP` (or similar).
2. Turn **off** interactive features.
3. Save.

Chat app configuration is generally unavailable for consumer Google
accounts; if the console says the Chat API is Workspace-only, do not enable
the `chat` service in Eurus.

### Google Classroom specifics

Classroom requires a Google Workspace for Education account; the service is
unavailable for regular consumer accounts.

## Enable services in Eurus

```bash
eurus mcp google list          # catalog with endpoints and scope counts
eurus mcp google enable gmail
eurus mcp google enable drive
eurus auth google login        # (re-)authorize with the new scopes
eurus mcp google status        # configuration + connection overview
eurus mcp google test          # live connection test
```

Configuration is stored in `~/.eurus/integrations/google-workspace.json`,
separately from external servers in `~/.eurus/mcp.json`:

```json
{
  "version": 1,
  "services": {
    "gmail": {"enabled": true, "timeout_seconds": 45.0},
    "drive": {"enabled": true, "timeout_seconds": 45.0}
  }
}
```

## Tool discovery and safety

- Tools are discovered live from each Google endpoint after connecting;
  nothing is hardcoded.
- Discovered tools are named `mcp_google_<service>_<tool>`, for example
  `mcp_google_gmail_search_threads`.
- Risk classification follows the tool name (see
  [MCP_SECURITY.md](MCP_SECURITY.md)): `search_threads` is read-only,
  `create_draft` requires approval, `send_message` and `delete_thread` always
  require explicit approval.
- Gmail and Drive can succeed while Docs fails: each service connects
  independently, and a failed service never disconnects the others.
- If the active account's token expires, it is refreshed transparently; if
  the refresh token is revoked, only that account's services move to
  `reauth_required` and everything else keeps working.
