"""OAuth 2.0 authorization-code flow primitives with PKCE and CSRF protection.

Provider-agnostic building blocks used by the Google integration: state
generation, PKCE challenge derivation, token exchange, refresh and revocation.
Every network function accepts an ``httpx.AsyncClient`` so tests can inject a
mock transport or run against a local OAuth server.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

STATE_MAX_AGE_SECONDS = 600.0


class OAuthFlowError(RuntimeError):
    pass


class StateMismatchError(OAuthFlowError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str
    auth_endpoint: str
    token_endpoint: str
    revoke_endpoint: str
    userinfo_endpoint: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip())


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    client: OAuthClient,
    scopes: list[str],
    state: str,
    code_challenge: str,
    *,
    extra_params: dict[str, str] | None = None,
) -> str:
    if not client.configured:
        raise OAuthFlowError("OAuth client is not configured")
    params: dict[str, str] = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    params.update(extra_params or {})
    return str(httpx.URL(client.auth_endpoint, params=params))


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    expires_in: int
    scope: str
    token_type: str
    refresh_token: str = ""
    id_token: str = ""


def parse_token_response(payload: dict[str, Any]) -> TokenResponse:
    access = str(payload.get("access_token", ""))
    if not access:
        raise OAuthFlowError(f"token endpoint returned no access token: {sorted(payload)}")
    return TokenResponse(
        access_token=access,
        expires_in=int(payload.get("expires_in", 3600)),
        scope=str(payload.get("scope", "")),
        token_type=str(payload.get("token_type", "Bearer")),
        refresh_token=str(payload.get("refresh_token", "") or ""),
        id_token=str(payload.get("id_token", "") or ""),
    )


async def exchange_code(
    client: OAuthClient,
    code: str,
    code_verifier: str,
    http: httpx.AsyncClient,
) -> TokenResponse:
    data = {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": client.redirect_uri,
    }
    response = await http.post(client.token_endpoint, data=data)
    if response.status_code != 200:
        raise OAuthFlowError(
            f"authorization code exchange failed ({response.status_code}): "
            f"{_error_description(response)}"
        )
    return parse_token_response(_json_of(response))


async def refresh_access_token(
    client: OAuthClient,
    refresh_token: str,
    http: httpx.AsyncClient,
) -> TokenResponse:
    data = {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = await http.post(client.token_endpoint, data=data)
    if response.status_code in {400, 401}:
        raise OAuthFlowError(
            f"refresh token rejected ({response.status_code}): {_error_description(response)}"
        )
    if response.status_code != 200:
        raise OAuthFlowError(f"token refresh failed ({response.status_code})")
    token = parse_token_response(_json_of(response))
    if not token.refresh_token:
        return TokenResponse(
            access_token=token.access_token,
            expires_in=token.expires_in,
            scope=token.scope,
            token_type=token.token_type,
            refresh_token=refresh_token,
            id_token=token.id_token,
        )
    return token


async def revoke_token(token: str, client: OAuthClient, http: httpx.AsyncClient) -> bool:
    response = await http.post(client.revoke_endpoint, data={"token": token})
    return response.status_code == 200


async def fetch_userinfo(
    client: OAuthClient, access_token: str, http: httpx.AsyncClient
) -> dict[str, Any]:
    if not client.userinfo_endpoint:
        return {}
    response = await http.get(
        client.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}
    )
    if response.status_code != 200:
        raise OAuthFlowError(f"userinfo lookup failed ({response.status_code})")
    payload = _json_of(response)
    return payload if isinstance(payload, dict) else {}


def _json_of(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthFlowError("OAuth endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthFlowError("OAuth endpoint returned an unexpected payload")
    return payload


def _error_description(response: httpx.Response) -> str:
    payload = _json_of(response)
    return str(payload.get("error_description") or payload.get("error") or response.text[:200])
