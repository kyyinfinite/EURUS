"""Google OAuth 2.0 authentication and secure credential storage."""

from eurus.mcp.auth.accounts import (
    STATUS_CONNECTED,
    STATUS_LOGGED_OUT,
    STATUS_REAUTH_REQUIRED,
    GoogleAccount,
    GoogleAccountManager,
    account_id_for_email,
)
from eurus.mcp.auth.callback import CallbackResult, CallbackServer
from eurus.mcp.auth.credentials import (
    GOOGLE_AUTH_ENDPOINT,
    GOOGLE_REVOKE_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    GOOGLE_USERINFO_ENDPOINT,
    client_file_path,
    load_oauth_client,
)
from eurus.mcp.auth.google import GoogleOAuthManager, TokenManager
from eurus.mcp.auth.oauth import (
    OAuthClient,
    OAuthFlowError,
    StateMismatchError,
    build_authorization_url,
    code_challenge_s256,
    exchange_code,
    generate_code_verifier,
    generate_state,
    refresh_access_token,
    revoke_token,
)
from eurus.mcp.auth.token_store import (
    PASSPHRASE_ENV_VAR,
    CredentialStore,
    CredentialStoreError,
    StoredCredentials,
)

__all__ = [
    "CallbackResult",
    "CallbackServer",
    "CredentialStore",
    "CredentialStoreError",
    "GOOGLE_AUTH_ENDPOINT",
    "GOOGLE_REVOKE_ENDPOINT",
    "GOOGLE_TOKEN_ENDPOINT",
    "GOOGLE_USERINFO_ENDPOINT",
    "GoogleAccount",
    "GoogleAccountManager",
    "GoogleOAuthManager",
    "OAuthClient",
    "OAuthFlowError",
    "PASSPHRASE_ENV_VAR",
    "STATUS_CONNECTED",
    "STATUS_LOGGED_OUT",
    "STATUS_REAUTH_REQUIRED",
    "StateMismatchError",
    "StoredCredentials",
    "TokenManager",
    "account_id_for_email",
    "build_authorization_url",
    "client_file_path",
    "code_challenge_s256",
    "exchange_code",
    "generate_code_verifier",
    "generate_state",
    "load_oauth_client",
    "refresh_access_token",
    "revoke_token",
]
