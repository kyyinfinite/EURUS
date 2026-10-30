"""Unit tests for OAuth primitives: PKCE, state, token exchange, refresh."""

from __future__ import annotations

import httpx
import pytest

from eurus.mcp.auth.oauth import (
    OAuthClient,
    OAuthFlowError,
    StateMismatchError,
    build_authorization_url,
    code_challenge_s256,
    exchange_code,
    generate_code_verifier,
    generate_state,
    parse_token_response,
    refresh_access_token,
    revoke_token,
)


def _client(**overrides: str) -> OAuthClient:
    defaults = {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "secret",
        "redirect_uri": "http://127.0.0.1:1/callback",
        "auth_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://token.example/token",
        "revoke_endpoint": "https://token.example/revoke",
        "userinfo_endpoint": "https://token.example/userinfo",
    }
    defaults.update(overrides)
    return OAuthClient(**defaults)


def test_state_is_random_and_long() -> None:
    states = {generate_state() for _ in range(32)}
    assert len(states) == 32
    assert all(len(state) >= 32 for state in states)


def test_pkce_verifier_and_challenge() -> None:
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128
    challenge = code_challenge_s256(verifier)
    assert len(challenge) >= 43
    assert code_challenge_s256(verifier) == challenge
    assert code_challenge_s256("other") != challenge


def test_authorization_url_contains_flow_params() -> None:
    url = build_authorization_url(_client(), ["openid", "email"], "state-1", "challenge-1")
    assert url.startswith("https://auth.example/authorize?")
    params = dict(httpx.URL(url).params)
    assert params["client_id"] == "cid.apps.googleusercontent.com"
    assert params["response_type"] == "code"
    assert params["access_type"] == "offline"
    assert params["state"] == "state-1"
    assert params["code_challenge"] == "challenge-1"
    assert params["code_challenge_method"] == "S256"
    assert "openid" in params["scope"] and "email" in params["scope"]


def test_authorization_url_requires_client() -> None:
    with pytest.raises(OAuthFlowError, match="not configured"):
        build_authorization_url(_client(client_id=" "), ["openid"], "s", "c")


def test_parse_token_response_requires_access_token() -> None:
    with pytest.raises(OAuthFlowError, match="no access token"):
        parse_token_response({"expires_in": 100})
    token = parse_token_response({"access_token": "a", "expires_in": 5})
    assert token.refresh_token == ""


@pytest.mark.asyncio
async def test_exchange_code_success() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "expires_in": 3600,
                "refresh_token": "rt",
                "scope": "openid",
                "token_type": "Bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await exchange_code(_client(), "the-code", "verifier-1", http)
    assert token.access_token == "at"
    assert token.refresh_token == "rt"
    assert "code=the-code" in seen["body"]
    assert "code_verifier=verifier-1" in seen["body"]
    assert "grant_type=authorization_code" in seen["body"]


@pytest.mark.asyncio
async def test_exchange_code_rejects_invalid_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthFlowError, match="Bad code"):
            await exchange_code(_client(), "bad", "v", http)


@pytest.mark.asyncio
async def test_refresh_access_token_rotates_or_keeps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "at2", "expires_in": 100, "token_type": "Bearer"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await refresh_access_token(_client(), "rt-orig", http)
    assert token.access_token == "at2"
    assert token.refresh_token == "rt-orig"


@pytest.mark.asyncio
async def test_refresh_rejected_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthFlowError, match="invalid_grant"):
            await refresh_access_token(_client(), "rt", http)


@pytest.mark.asyncio
async def test_revoke_token_reports_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await revoke_token("rt", _client(), http) is True


def test_state_mismatch_error_exists() -> None:
    assert issubclass(StateMismatchError, OAuthFlowError)
