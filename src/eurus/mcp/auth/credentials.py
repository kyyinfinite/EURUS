"""Google OAuth client credentials.

EURUS is an installed desktop application, so the recommended Google Cloud
console setup is an OAuth 2.0 client of type "Desktop app" (or a "Web
application" with an explicit redirect URI). Google's Workspace MCP servers
additionally require the "Use this client for an AI-powered agent" option on
the OAuth client.

Credentials are read from, in order:

1. Environment variables: EURUS_GOOGLE_CLIENT_ID / EURUS_GOOGLE_CLIENT_SECRET
   (and optionally EURUS_GOOGLE_REDIRECT_URI).
2. ``auth/google/client.json`` under the Eurus home directory, either in the
   flat ``{"client_id": ..., "client_secret": ...}`` form or Google's
   ``{"installed": {...}}`` / ``{"web": {...}}`` console export shape.

The file never contains tokens and must be created by the user; Eurus never
writes client secrets itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from eurus.mcp.auth.oauth import OAuthClient

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

CLIENT_ID_ENV = "EURUS_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "EURUS_GOOGLE_CLIENT_SECRET"
REDIRECT_URI_ENV = "EURUS_GOOGLE_REDIRECT_URI"

CLIENT_FILE_NAME = "client.json"


def client_file_path(home: Path) -> Path:
    return home / "auth" / "google" / CLIENT_FILE_NAME


def _from_console_export(raw: dict[str, object]) -> tuple[str, str, str]:
    for section in ("installed", "web"):
        data = raw.get(section)
        if isinstance(data, dict):
            redirect = ""
            uris = data.get("redirect_uris")
            if isinstance(uris, list) and uris:
                redirect = str(uris[0])
            return str(data.get("client_id", "")), str(data.get("client_secret", "")), redirect
    return "", "", ""


def load_oauth_client(
    home: Path,
    *,
    environ: dict[str, str] | None = None,
    redirect_port: int = 0,
) -> OAuthClient | None:
    env = os.environ if environ is None else environ
    client_id = env.get(CLIENT_ID_ENV, "").strip()
    client_secret = env.get(CLIENT_SECRET_ENV, "").strip()
    redirect_uri = env.get(REDIRECT_URI_ENV, "").strip()

    if not client_id:
        path = client_file_path(home)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict):
                file_id, file_secret, file_redirect = _from_console_export(raw)
                client_id = client_id or file_id
                client_secret = client_secret or file_secret
                redirect_uri = redirect_uri or file_redirect

    if not client_id:
        return None
    # An empty redirect_uri means "dynamic loopback": the login flow binds an
    # ephemeral localhost port, which Google accepts for Desktop-app clients.
    return OAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        auth_endpoint=GOOGLE_AUTH_ENDPOINT,
        token_endpoint=GOOGLE_TOKEN_ENDPOINT,
        revoke_endpoint=GOOGLE_REVOKE_ENDPOINT,
        userinfo_endpoint=GOOGLE_USERINFO_ENDPOINT,
    )
