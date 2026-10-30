"""Google OAuth 2.0 authentication for Eurus.

Implements the Authorization Code flow with offline access, PKCE, a
cryptographically random CSRF state, a localhost browser callback, automatic
token refresh with concurrency coordination, revocation and multi-account
support.

The flow never stores Google passwords, never asks the user to paste access
tokens, and never logs token values.
"""

from __future__ import annotations

import asyncio
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from eurus.errors import ReauthenticationRequiredError
from eurus.mcp.auth.accounts import (
    STATUS_CONNECTED,
    STATUS_LOGGED_OUT,
    STATUS_REAUTH_REQUIRED,
    GoogleAccount,
    GoogleAccountManager,
)
from eurus.mcp.auth.callback import CallbackServer
from eurus.mcp.auth.credentials import load_oauth_client
from eurus.mcp.auth.oauth import (
    STATE_MAX_AGE_SECONDS,
    OAuthClient,
    OAuthFlowError,
    StateMismatchError,
    build_authorization_url,
    code_challenge_s256,
    exchange_code,
    fetch_userinfo,
    generate_code_verifier,
    generate_state,
    refresh_access_token,
    revoke_token,
)
from eurus.mcp.auth.token_store import CredentialStore, CredentialStoreError

EXPIRY_SKEW_SECONDS = 60.0
DEFAULT_LOGIN_TIMEOUT = 300.0
DEFAULT_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")


def _default_http_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


class TokenManager:
    """Validates token expiration and refreshes access tokens.

    Concurrent refresh operations for the same account are coordinated through
    a per-account lock so only one refresh round-trip happens at a time; a
    refresh failure for one account never affects other accounts.
    """

    def __init__(
        self,
        store: CredentialStore,
        accounts: GoogleAccountManager,
        client_provider: Callable[[], OAuthClient | None],
        *,
        http_factory: Callable[[], httpx.AsyncClient] = _default_http_factory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.accounts = accounts
        self.client_provider = client_provider
        self.http_factory = http_factory
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def _is_expired(self, expires_at: float) -> bool:
        return self.clock() >= expires_at - EXPIRY_SKEW_SECONDS

    async def get_access_token(self, account_id: str) -> str:
        credentials = self.store.load(account_id)
        if credentials is None or not credentials.refresh_token:
            self.accounts.set_status(account_id, STATUS_REAUTH_REQUIRED)
            raise ReauthenticationRequiredError(
                f"Google account '{account_id}' has no stored credentials; "
                "run: eurus auth google login",
                account=account_id,
                provider="google",
            )
        if credentials.access_token and not self._is_expired(credentials.expires_at):
            return credentials.access_token
        async with self._lock(account_id):
            credentials = self.store.load(account_id)
            if credentials is None or not credentials.refresh_token:
                self.accounts.set_status(account_id, STATUS_REAUTH_REQUIRED)
                raise ReauthenticationRequiredError(
                    f"Google account '{account_id}' has no stored credentials",
                    account=account_id,
                    provider="google",
                )
            if credentials.access_token and not self._is_expired(credentials.expires_at):
                return credentials.access_token
            return await self._refresh_locked(account_id, credentials)

    async def _refresh_locked(self, account_id: str, credentials: Any) -> str:
        client = self.client_provider()
        if client is None:
            raise ReauthenticationRequiredError(
                "Google OAuth client is no longer configured",
                account=account_id,
                provider="google",
            )
        try:
            async with self.http_factory() as http:
                token = await refresh_access_token(client, credentials.refresh_token, http)
        except OAuthFlowError as exc:
            self.accounts.set_status(account_id, STATUS_REAUTH_REQUIRED)
            raise ReauthenticationRequiredError(
                f"Google refresh token is no longer valid for '{account_id}': {exc}",
                account=account_id,
                provider="google",
            ) from exc
        self.store.update(
            account_id,
            access_token=token.access_token,
            expires_at=self.clock() + token.expires_in,
            refresh_token=token.refresh_token,
            scope=token.scope or credentials.scope,
            client_id=client.client_id,
        )
        self.accounts.set_status(account_id, STATUS_CONNECTED)
        return token.access_token


class GoogleOAuthManager:
    """Central Google authentication: login, refresh, revoke, multi-account."""

    def __init__(
        self,
        home: Any,
        *,
        store: CredentialStore,
        accounts: GoogleAccountManager,
        environ: dict[str, str] | None = None,
        http_factory: Callable[[], httpx.AsyncClient] = _default_http_factory,
        open_browser: Callable[[str], bool] = webbrowser.open,
        clock: Callable[[], float] = time.time,
        client: OAuthClient | None = None,
    ) -> None:
        self.home = home
        self.store = store
        self.accounts = accounts
        self.environ = environ
        self.http_factory = http_factory
        self.open_browser = open_browser
        self.clock = clock
        self._client: OAuthClient | None = client
        self._client_loaded = client is not None
        self.last_authorization_url = ""
        self._pending_states: dict[str, tuple[float, str]] = {}
        self.token_manager = TokenManager(
            store,
            accounts,
            self._load_client,
            http_factory=http_factory,
            clock=clock,
        )

    def _load_client(self) -> OAuthClient | None:
        if not self._client_loaded:
            self._client = load_oauth_client(self.home, environ=self.environ)
            self._client_loaded = True
        return self._client

    @property
    def client(self) -> OAuthClient | None:
        return self._load_client()

    @property
    def client_configured(self) -> bool:
        client = self.client
        return client is not None and client.configured

    def _register_state(self, state: str, verifier: str) -> None:
        now = self.clock()
        expired = [
            key
            for key, (created, _) in self._pending_states.items()
            if now - created > STATE_MAX_AGE_SECONDS
        ]
        for key in expired:
            del self._pending_states[key]
        self._pending_states[state] = (now, verifier)

    def _consume_state(self, state: str, verifier_hint: str | None = None) -> str:
        entry = self._pending_states.pop(state, None)
        if entry is None:
            raise StateMismatchError("unknown or reused OAuth state (possible CSRF attempt)")
        created, verifier = entry
        if self.clock() - created > STATE_MAX_AGE_SECONDS:
            raise StateMismatchError("OAuth state has expired; start the login again")
        del verifier_hint
        return verifier

    async def login(
        self,
        scopes: list[str] | None = None,
        *,
        open_browser: bool = True,
        timeout: float = DEFAULT_LOGIN_TIMEOUT,
    ) -> GoogleAccount:
        client = self._load_client()
        if client is None or not client.configured:
            raise OAuthFlowError(
                "Google OAuth client is not configured. Set EURUS_GOOGLE_CLIENT_ID and "
                "EURUS_GOOGLE_CLIENT_SECRET, or create "
                "<eurus-home>/auth/google/client.json - see docs/GOOGLE_OAUTH.md"
            )
        requested_scopes = list(scopes or DEFAULT_SCOPES)
        port = _loopback_port_of(client.redirect_uri)
        async with CallbackServer(port=port) as server:
            flow_client = (
                client if client.redirect_uri else replace(client, redirect_uri=server.redirect_uri)
            )
            state = generate_state()
            verifier = generate_code_verifier()
            self._register_state(state, verifier)
            url = build_authorization_url(
                flow_client,
                requested_scopes,
                state,
                code_challenge_s256(verifier),
            )
            self.last_authorization_url = url
            if open_browser:
                opened = False
                try:
                    opened = bool(self.open_browser(url))
                except Exception:
                    opened = False
                if not opened:
                    print(f"Open this URL in your browser to authorize Eurus:\n\n{url}\n")
            else:
                print(f"Authorization URL:\n\n{url}\n")
            result = await server.wait_for_callback(timeout)
            if result.state != state:
                self._pending_states.pop(state, None)
                raise StateMismatchError("OAuth state returned by Google does not match")
            expected_verifier = self._consume_state(state)
            if expected_verifier != verifier:
                raise StateMismatchError("PKCE verifier mismatch")
            async with self.http_factory() as http:
                token = await exchange_code(flow_client, result.code, verifier, http)
                userinfo = await fetch_userinfo(flow_client, token.access_token, http)
        email = str(userinfo.get("email", "")).strip().lower()
        if not email:
            raise OAuthFlowError("Google did not return an email address for this account")
        account = self.accounts.add(email)
        from eurus.mcp.auth.token_store import StoredCredentials

        self.store.store(
            StoredCredentials(
                account_id=account.account_id,
                email=email,
                client_id=flow_client.client_id,
                refresh_token=token.refresh_token,
                access_token=token.access_token,
                expires_at=self.clock() + token.expires_in,
                scope=token.scope or " ".join(requested_scopes),
            )
        )
        self.accounts.set_active(account.account_id)
        self.accounts.set_status(account.account_id, STATUS_CONNECTED)
        return account

    async def logout(
        self, account_id: str | None = None, *, revoke: bool = True
    ) -> list[GoogleAccount]:
        if account_id is None:
            targets = self.accounts.list()
        else:
            account = self.accounts.get(account_id)
            if account is None:
                raise ValueError(f"unknown Google account: {account_id}")
            targets = [account]
        removed: list[GoogleAccount] = []
        client = self._load_client()
        for account in targets:
            credentials = self.store.load(account.account_id)
            if revoke and credentials and credentials.refresh_token and client is not None:
                try:
                    async with self.http_factory() as http:
                        await revoke_token(credentials.refresh_token, client, http)
                except Exception:
                    pass
            self.store.delete(account.account_id)
            self.accounts.set_status(account.account_id, STATUS_LOGGED_OUT)
            self.accounts.remove(account.account_id)
            removed.append(account)
        return removed

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for account in self.accounts.list():
            credentials = self.store.load(account.account_id)
            rows.append(
                {
                    "account_id": account.account_id,
                    "email": account.email,
                    "active": account.account_id == self.accounts.active_id,
                    "has_credentials": credentials is not None,
                    "token_valid": bool(
                        credentials
                        and credentials.access_token
                        and not self.token_manager._is_expired(credentials.expires_at)
                    ),
                    "status": self.accounts.status_of(account.account_id),
                }
            )
        return rows

    def diagnostics(self) -> dict[str, Any]:
        return {
            "client_configured": self.client_configured,
            "storage_backend": self.store.backend_name,
            "accounts": len(self.accounts.list()),
            "active_account": self.accounts.active_id,
        }


def _loopback_port_of(redirect_uri: str) -> int:
    if not redirect_uri:
        return 0
    parts = urlsplit(redirect_uri)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise OAuthFlowError(
            f"non-loopback redirect URIs are not supported for desktop logins: {redirect_uri}"
        )
    return int(parts.port or 0)


__all__ = [
    "CredentialStoreError",
    "GoogleOAuthManager",
    "OAuthFlowError",
    "TokenManager",
]
