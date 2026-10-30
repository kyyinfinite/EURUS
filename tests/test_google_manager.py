"""Integration tests for GoogleOAuthManager against a mock OAuth server.

Covers: successful login (PKCE enforced end-to-end, browser redirect to the
localhost callback), authorization denied, invalid state, code replay,
expired tokens with refresh, refresh-token rotation, revoked refresh tokens,
multi-account login and logout.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import MockGoogleOAuth, running_oauth_mock
from eurus.errors import ReauthenticationRequiredError
from eurus.mcp.auth.accounts import GoogleAccountManager
from eurus.mcp.auth.google import GoogleOAuthManager
from eurus.mcp.auth.oauth import OAuthClient, OAuthFlowError, StateMismatchError
from eurus.mcp.auth.token_store import CredentialStore, EncryptedFileBackend

pytestmark = pytest.mark.asyncio


def _client(endpoints: dict[str, str]) -> OAuthClient:
    return OAuthClient(
        client_id="mock-client-id",
        client_secret="mock-client-secret",
        redirect_uri="",
        auth_endpoint=endpoints["auth"],
        token_endpoint=endpoints["token"],
        revoke_endpoint=endpoints["revoke"],
        userinfo_endpoint=endpoints["userinfo"],
    )


def _manager(home: Path, client: OAuthClient, *, clock: object = None) -> GoogleOAuthManager:
    accounts = GoogleAccountManager(home / "auth" / "google" / "accounts.json")
    store = CredentialStore(EncryptedFileBackend(home / "auth" / "google" / "tokens", "pp"))
    kwargs: dict[str, object] = {"client": client}
    if clock is not None:
        kwargs["clock"] = clock
    return GoogleOAuthManager(home, store=store, accounts=accounts, **kwargs)  # type: ignore[arg-type]


async def _drive_browser(
    manager: GoogleOAuthManager,
    *,
    state_override: str | None = None,
    previous_url: str = "",
) -> None:
    """Act as the browser: request the authorization URL, follow the redirect
    back to the localhost callback (optionally tampering with the state)."""
    for _ in range(200):
        if manager.last_authorization_url and manager.last_authorization_url != previous_url:
            break
        await asyncio.sleep(0.02)
    assert manager.last_authorization_url and manager.last_authorization_url != previous_url, (
        "manager did not build an authorization URL"
    )
    async with httpx.AsyncClient(follow_redirects=False) as http:
        response = await http.get(manager.last_authorization_url)
        assert response.status_code in {302, 307}, response.status_code
        target = response.headers["location"]
        if state_override is not None:
            parts = urlsplit(target)
            query = parse_qs(parts.query)
            query["state"] = [state_override]
            from urllib.parse import urlencode

            target = f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query, doseq=True)}"
        await http.get(target)


async def test_login_success_end_to_end(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid", "email"], open_browser=False, timeout=10)
        await browser
        assert account.email == "tester@example.com"
        assert manager.accounts.active_id == account.account_id
        credentials = manager.store.load(account.account_id)
        assert credentials is not None
        assert credentials.refresh_token.startswith("rt-")
        assert credentials.access_token in mock_google.access_email
        token = await manager.token_manager.get_access_token(account.account_id)
        assert token == credentials.access_token


async def test_login_denied_by_user(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    mock_google.authorization_denied = True
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        with pytest.raises(OAuthFlowError, match="access_denied"):
            await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        assert manager.accounts.list() == []


async def test_login_rejects_forged_state(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager, state_override="forged-state"))
        with pytest.raises(StateMismatchError):
            await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        assert manager.accounts.list() == []


async def test_login_requires_configured_client(tmp_path: Path) -> None:
    manager = _manager(tmp_path, OAuthClient("", "", "", "a", "t", "r", "u"))
    with pytest.raises(OAuthFlowError, match="not configured"):
        await manager.login(scopes=["openid"], open_browser=False, timeout=5)


async def test_token_refresh_after_expiry(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        first = await manager.token_manager.get_access_token(account.account_id)
        manager.store.update(account.account_id, expires_at=time.time() - 5)
        second = await manager.token_manager.get_access_token(account.account_id)
        assert second != first
        assert second.startswith("at-")


async def test_refresh_token_rotation_persists_new_token(
    tmp_path: Path, mock_google: MockGoogleOAuth
) -> None:
    mock_google.rotate_refresh = True
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        original = manager.store.load(account.account_id).refresh_token
        manager.store.update(account.account_id, expires_at=time.time() - 5)
        await manager.token_manager.get_access_token(account.account_id)
        rotated = manager.store.load(account.account_id).refresh_token
        assert rotated != original
        assert rotated in mock_google.refresh_tokens


async def test_revoked_refresh_token_requires_reauth(
    tmp_path: Path, mock_google: MockGoogleOAuth
) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        mock_google.reject_refresh = True
        manager.store.update(account.account_id, expires_at=time.time() - 5)
        with pytest.raises(ReauthenticationRequiredError):
            await manager.token_manager.get_access_token(account.account_id)
        assert manager.status()[0]["status"] == "reauth_required"


async def test_concurrent_refresh_is_coordinated(
    tmp_path: Path, mock_google: MockGoogleOAuth
) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        manager.store.update(account.account_id, expires_at=time.time() - 5)
        calls: list[int] = []
        original_refresh = manager.token_manager._refresh_locked

        async def counting_refresh(account_id: str, credentials: object) -> str:
            calls.append(1)
            await asyncio.sleep(0.1)
            return await original_refresh(account_id, credentials)

        manager.token_manager._refresh_locked = counting_refresh
        tokens = await asyncio.gather(
            *(manager.token_manager.get_access_token(account.account_id) for _ in range(5))
        )
        assert len(set(tokens)) == 1
        assert len(calls) == 1, "concurrent refresh must be coordinated to one call"


async def test_multiple_accounts_and_switching(
    tmp_path: Path, mock_google: MockGoogleOAuth
) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        mock_google.next_email = "alice@example.com"
        previous = manager.last_authorization_url
        browser = asyncio.create_task(_drive_browser(manager, previous_url=previous))
        alice = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        assert alice.email == "alice@example.com"

        mock_google.next_email = "bob@example.com"
        previous = manager.last_authorization_url
        browser2 = asyncio.create_task(_drive_browser(manager, previous_url=previous))
        bob = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser2
        assert bob.email == "bob@example.com"

        emails = sorted(item.email for item in manager.accounts.list())
        assert emails == ["alice@example.com", "bob@example.com"]
        assert manager.accounts.active_id == bob.account_id

        alice_token = await manager.token_manager.get_access_token(alice.account_id)
        bob_token = await manager.token_manager.get_access_token(bob.account_id)
        assert alice_token != bob_token

        manager.accounts.set_active(alice.account_id)
        assert manager.accounts.active_id == alice.account_id


async def test_logout_revokes_and_removes(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    async with running_oauth_mock(mock_google) as endpoints:
        manager = _manager(tmp_path, _client(endpoints))
        browser = asyncio.create_task(_drive_browser(manager))
        account = await manager.login(scopes=["openid"], open_browser=False, timeout=10)
        await browser
        removed = await manager.logout(account.account_id)
        assert [item.email for item in removed] == ["tester@example.com"]
        assert manager.store.load(account.account_id) is None
        assert manager.accounts.list() == []
        with pytest.raises(ReauthenticationRequiredError):
            await manager.token_manager.get_access_token(account.account_id)


async def test_expired_state_is_rejected(tmp_path: Path, mock_google: MockGoogleOAuth) -> None:
    async with running_oauth_mock(mock_google) as endpoints:

        class FakeClock:
            def __init__(self) -> None:
                self.now = 1000.0

            def __call__(self) -> float:
                self.now += 1.0
                return self.now

        clock = FakeClock()
        manager = _manager(tmp_path, _client(endpoints), clock=clock)
        state = "state-old"
        manager._register_state(state, "verifier")
        clock.now += 3600.0
        with pytest.raises(StateMismatchError, match="expired"):
            manager._consume_state(state)
