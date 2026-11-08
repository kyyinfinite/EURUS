from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from eurus.mcp.auth.oauth import OAuthFlowError
from eurus.mcp.diagnosis import (
    ErrorCode,
    diagnose,
    diagnose_exception,
    is_transport_failure,
    redact,
    refresh_token_expiry_warning,
)
from eurus.mcp.policy import (
    DESTRUCTIVE,
    READ_ONLY,
    REVERSIBLE_WRITE,
    SENSITIVE_MUTATION,
    MCPPolicy,
    ToolAnnotations,
)


def test_agentic_client_marker_maps_to_setup_error_not_reauth() -> None:
    result = diagnose(403, "Client is not marked as an AI-powered agent (is_agentic).")
    assert result.code is ErrorCode.AGENTIC_CLIENT_REQUIRED
    assert result.reauth is False
    assert "11 Nov 2026" in result.fix or "AI-powered agent" in result.fix


def test_service_disabled_body_yields_gcloud_hint() -> None:
    result = diagnose(
        403,
        "Google APIs are not enabled: gmailmcp.googleapis.com has not been used in project",
    )
    assert result.code is ErrorCode.SERVICE_DISABLED
    assert "gcloud services enable gmailmcp.googleapis.com" in result.fix


def test_scope_insufficient_requires_reauth() -> None:
    result = diagnose(403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT for this request")
    assert result.code is ErrorCode.SCOPE_MISSING
    assert result.reauth is True


def test_invalid_grant_maps_to_token_expired_reauth() -> None:
    result = diagnose(400, '{"error": "invalid_grant", "error_description": "Token expired"}')
    assert result.code is ErrorCode.TOKEN_EXPIRED
    assert result.reauth is True


def test_plain_401_is_reauth_but_403_unknown_is_not() -> None:
    assert diagnose(401, "").reauth is True
    plain_403 = diagnose(403, "some unrecognized body")
    assert plain_403.reauth is False


def test_rate_limit_is_transient() -> None:
    for status, body in ((429, ""), (503, ""), (500, "backend error")):
        result = diagnose(status, body)
        assert result.code is ErrorCode.TRANSIENT, (status, body)
        assert result.transient is True
        assert result.reauth is False


def test_network_exception_is_transient() -> None:
    exc = httpx.ConnectError("connection dropped")
    result = diagnose_exception(exc)
    assert result.code is ErrorCode.NETWORK
    assert result.transient is True


def test_bodies_are_redacted_before_matching() -> None:
    leaky = "error with access_token=ya29.SUPERSECRETVALUE123 and Bearer abc.def.ghi"
    cleaned = redact(leaky)
    assert "SUPERSECRETVALUE" not in cleaned
    assert "<redacted>" in cleaned


def test_transport_failure_detection() -> None:
    assert is_transport_failure(httpx.RemoteProtocolError("peer closed connection"))
    assert is_transport_failure(httpx.ConnectError("reset"))
    assert is_transport_failure(RuntimeError("Stream closed before response completed"))
    assert is_transport_failure(RuntimeError("Session expired"))
    assert not is_transport_failure(ValueError("bad argument"))
    request = httpx.Request("POST", "https://x")
    response = httpx.Response(404, request=request)
    assert is_transport_failure(httpx.HTTPStatusError("gone", request=request, response=response))


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_expiry_warning_when_close_to_seven_days() -> None:
    clock = _Clock(0.0)
    assert refresh_token_expiry_warning(0.0, "testing", clock) == ""
    assert refresh_token_expiry_warning(1.0, "production", clock) == ""
    issued = 1.0
    clock.now = issued + 6.5 * 86400
    warning = refresh_token_expiry_warning(issued, "testing", clock)
    assert "expires in about" in warning
    assert "Production" in warning


def test_expiry_warning_after_expiry() -> None:
    issued = 1.0
    clock = _Clock(issued + 8.0 * 86400)
    warning = refresh_token_expiry_warning(issued, "testing", clock)
    assert "likely expired" in warning


def test_annotations_take_priority_over_name_heuristics() -> None:
    policy = MCPPolicy()
    decision = policy.tool_flags(
        "google-drive",
        "list_shared_drives",
        ToolAnnotations(read_only_hint=True, destructive_hint=None),
    )
    assert decision.level is READ_ONLY
    assert decision.mutates is False

    decision = policy.tool_flags(
        "google-drive", "get_and_wipe", ToolAnnotations(read_only_hint=False, destructive_hint=True)
    )
    assert decision.level is DESTRUCTIVE
    assert decision.approval_is_mandatory is True


def test_annotations_none_falls_back_to_keywords() -> None:
    policy = MCPPolicy()
    decision = policy.tool_flags("google-drive", "create_draft", None)
    assert decision.level is REVERSIBLE_WRITE


def test_keyword_heuristics_are_token_based() -> None:
    policy = MCPPolicy()
    assert policy.classify("google-drive", "list_shared_drives").level is READ_ONLY
    assert policy.classify("google-drive", "share_file").level is SENSITIVE_MUTATION
    assert policy.classify("google-drive", "empty_trash").level is DESTRUCTIVE
    assert policy.classify("google-drive", "set_permissions").level is SENSITIVE_MUTATION
    assert policy.classify("google-drive", "create_draft").level is REVERSIBLE_WRITE
    assert policy.classify("google-drive", "mark_as_read").level is REVERSIBLE_WRITE
    assert policy.classify("google-drive", "search_threads").level is READ_ONLY


def test_user_rules_beat_annotations() -> None:
    policy = MCPPolicy()
    policy.set_server_rule("google-drive", "list_*", SENSITIVE_MUTATION)
    decision = policy.tool_flags("google-drive", "list_files", ToolAnnotations(read_only_hint=True))
    assert decision.level is SENSITIVE_MUTATION
    assert decision.approval_is_mandatory is True


def test_access_floor_read_requires_approval_for_mutating() -> None:
    policy = MCPPolicy()
    policy.google_access = "read"
    decision = policy.tool_flags("google-gmail", "create_draft", None)
    assert decision.mutates is True
    assert decision.requires_approval is True
    assert policy.tool_flags("google-gmail", "search_threads", None).requires_approval is False


def test_access_floor_full_relaxes_only_reversible_writes() -> None:
    policy = MCPPolicy()
    policy.google_access = "full"
    reversible = policy.tool_flags("google-gmail", "create_draft", None)
    assert reversible.requires_approval is False
    assert policy.tool_flags("google-gmail", "send_message", None).requires_approval is True
    assert policy.tool_flags("google-gmail", "delete_thread", None).requires_approval is True


def test_external_servers_stay_fail_closed_with_annotations() -> None:
    policy = MCPPolicy()
    decision = policy.tool_flags("remote", "get_file", ToolAnnotations(read_only_hint=True))
    assert decision.level is READ_ONLY


async def test_refresh_503_retries_without_reauth_status(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eurus.errors import ReauthenticationRequiredError
    from eurus.mcp.auth.accounts import GoogleAccountManager
    from eurus.mcp.auth.google import GoogleOAuthManager
    from eurus.mcp.auth.oauth import OAuthClient, TransientOAuthError
    from eurus.mcp.auth.token_store import CredentialStore, EncryptedFileBackend, StoredCredentials

    accounts = GoogleAccountManager(tmp_path / "accounts.json")
    account = accounts.add("alice@example.com")
    store = CredentialStore(EncryptedFileBackend(tmp_path / "tokens", "pp"))
    store.store(
        StoredCredentials(
            account_id=account.account_id,
            email=account.email,
            client_id="cid",
            refresh_token="rt",
            access_token="at",
            expires_at=0.0,
            scope="openid",
        )
    )
    client = OAuthClient("cid", "sec", "", "a", "t", "r", "u")
    manager = GoogleOAuthManager(tmp_path, store=store, accounts=accounts, client=client)

    calls: list[int] = []

    async def fake_refresh(client_obj: OAuthClient, refresh_token: str, http: Any) -> Any:
        calls.append(1)
        raise TransientOAuthError("token refresh failed temporarily (503)", status=503)

    monkeypatch.setattr("eurus.mcp.auth.google.refresh_access_token", fake_refresh)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(OAuthFlowError) as excinfo:
        await manager.token_manager.get_access_token(account.account_id)
    assert "temporarily" in str(excinfo.value)
    assert len(calls) == 3, "transient refresh must be retried with backoff"
    assert len(sleeps) == 2
    assert accounts.status_of(account.account_id) != "reauth_required", (
        "transient failures must never require re-authentication (S4)"
    )

    async def permanent_refresh(client_obj: OAuthClient, refresh_token: str, http: Any) -> Any:
        raise OAuthFlowError("refresh token rejected (400): invalid_grant")

    monkeypatch.setattr("eurus.mcp.auth.google.refresh_access_token", permanent_refresh)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(ReauthenticationRequiredError):
        await manager.token_manager.get_access_token(account.account_id)
    assert accounts.status_of(account.account_id) == "reauth_required"
