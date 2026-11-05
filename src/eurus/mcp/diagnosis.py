from __future__ import annotations

import asyncio
import enum
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx


class ErrorCode(enum.StrEnum):
    AGENTIC_CLIENT_REQUIRED = "agentic_client_required"
    SERVICE_DISABLED = "service_disabled"
    SCOPE_MISSING = "scope_missing"
    TOKEN_EXPIRED = "token_expired"
    ACCESS_DENIED_TEST_USER = "access_denied_test_user"
    REDIRECT_MISMATCH = "redirect_mismatch"
    CLIENT_NOT_CONFIGURED = "client_not_configured"
    STORE_UNAVAILABLE = "store_unavailable"
    PREVIEW_NOT_AVAILABLE = "preview_not_available"
    TRANSIENT = "transient"
    NETWORK = "network"
    UNKNOWN = "unknown"


AGENTIC_DEADLINE = "11 Nov 2026"

_AGENTIC_MARKERS = (
    "not marked as an ai agent",
    "ai-powered agent",
    "agentic client",
    "is_agentic",
)
_SERVICE_DISABLED_MARKERS = ("service_disabled", "has not been used in project", "is disabled")
_SCOPE_MARKERS = (
    "access_token_scope_insufficient",
    "insufficient permissions",
    "insufficient scopes",
    "missing scopes",
)
_TEST_USER_MARKERS = (
    "has not completed the google verification process",
    "access_denied",
    "not been registered as a test user",
    "test users",
)
_PREVIEW_MARKERS = (
    "developer preview",
    "not enrolled",
    "preview is not available",
    "allowlist",
    "allow list",
    "unregistered",
)
_REDIRECT_MARKERS = ("redirect_uri_mismatch", "redirect mismatch", "invalid_redirect_uri")
_INVALID_GRANT_MARKERS = ("invalid_grant", "token has been expired or revoked", "token expired")
_INVALID_CLIENT_MARKERS = ("invalid_client", "unauthorized_client")
_TRANSIENT_MARKERS = (
    "resource_exhausted",
    "rate limit",
    "quota exceeded",
    "backend error",
    "internal error",
    "unavailable",
    "deadline exceeded",
)
_AUTH_MARKERS = (
    "request had invalid authentication credentials",
    "invalid authentication credentials",
    "unauthorized",
    "authentication required",
)

_MCP_SERVICE_NAMES = (
    "gmailmcp",
    "drivemcp",
    "docsmcp",
    "sheetsmcp",
    "slidesmcp",
    "calendarmcp",
    "chatmcp",
    "classroommcp",
    "gmail.googleapis.com",
    "drive.googleapis.com",
    "docs.googleapis.com",
    "sheets.googleapis.com",
    "slides.googleapis.com",
    "calendar-json.googleapis.com",
    "chat.googleapis.com",
    "people.googleapis.com",
    "classroom.googleapis.com",
)

_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-/]+=*"),
    re.compile(
        r"(?i)(access_token|refresh_token|client_secret|code|assertion)=[^&\s\"']+",
    ),
    re.compile(r"(?i)\"(access_token|refresh_token|client_secret|id_token)\"\s*:\s*\"[^\"]*\""),
    re.compile(r"\bey[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b1//[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)password[=:]\s*\S+"),
)

_REDACTED = "<redacted>"


def redact(text: str) -> str:
    result = text
    for pattern in _REDACT_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


@dataclass(frozen=True, slots=True)
class Diagnosis:
    code: ErrorCode
    message: str
    fix: str
    transient: bool = False
    reauth: bool = False

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message} Fix: {self.fix}"


def _iter_exceptions(exc: BaseException) -> Iterator[BaseException]:
    yield exc
    for child in getattr(exc, "exceptions", ()) or ():
        yield from _iter_exceptions(child)


def status_code_of(exc: BaseException) -> int | None:
    for item in _iter_exceptions(exc):
        response = getattr(item, "response", None)
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    code = getattr(exc, "status", None)
    if isinstance(code, int):
        return code
    return None


def _is_network_exception(exc: BaseException) -> bool:
    return isinstance(
        exc, httpx.TransportError | httpx.StreamError | ConnectionError | TimeoutError | OSError
    )


def _enabled_services_in(body: str) -> list[str]:
    found = [name for name in _MCP_SERVICE_NAMES if name in body]
    return found


def _service_disabled_fix(body: str) -> str:
    qualified = sorted(
        {match.group(0) for match in re.finditer(r"[a-z][a-z0-9-]*\.googleapis\.com", body)}
    )
    if qualified:
        known = [
            name
            for name in qualified
            if name in _MCP_SERVICE_NAMES
            or any(name.startswith(f"{short}.") for short in _MCP_SERVICE_NAMES)
            or any(short in name for short in _MCP_SERVICE_NAMES)
        ]
        if known:
            names = " ".join(f"gcloud services enable {name}" for name in known)
            return f"Enable the required APIs in your Google Cloud project, e.g.: {names}"
    services = _enabled_services_in(body)
    if services:
        names = " ".join(f"gcloud services enable {name}" for name in services)
        return f"Enable the required APIs in your Google Cloud project, e.g.: {names}"
    return (
        "Enable both the product API and the MCP service for this app in your "
        "Google Cloud project (APIs & Services -> Library), then reconnect."
    )


def _classify_body(body: str, status: int | None) -> Diagnosis | None:
    lowered = body.lower()
    if any(marker in lowered for marker in _AGENTIC_MARKERS):
        return Diagnosis(
            ErrorCode.AGENTIC_CLIENT_REQUIRED,
            "Google rejected this OAuth client because it is not marked as an AI-powered agent.",
            "Create a NEW OAuth client in Google Cloud Console with 'Use this client for an "
            f"AI-powered agent' enabled (existing clients cannot be changed after "
            f"{AGENTIC_DEADLINE}), then update client.json.",
        )
    if any(marker in lowered for marker in _SERVICE_DISABLED_MARKERS):
        return Diagnosis(
            ErrorCode.SERVICE_DISABLED,
            "A required Google API or MCP service is not enabled for your project.",
            _service_disabled_fix(body),
        )
    if any(marker in lowered for marker in _SCOPE_MARKERS):
        return Diagnosis(
            ErrorCode.SCOPE_MISSING,
            "The access token does not include the scopes this tool needs.",
            "Re-run: eurus auth google login (after enabling the service with "
            "'eurus mcp google enable <service>').",
            reauth=True,
        )
    if any(marker in lowered for marker in _INVALID_GRANT_MARKERS):
        return Diagnosis(
            ErrorCode.TOKEN_EXPIRED,
            "The Google refresh token is expired or was revoked.",
            "Re-run: eurus auth google login. With a consent screen in Testing mode the "
            "refresh token expires after 7 days; publish the app to Production for "
            "long-lived tokens.",
            reauth=True,
        )
    if any(marker in lowered for marker in _INVALID_CLIENT_MARKERS):
        return Diagnosis(
            ErrorCode.CLIENT_NOT_CONFIGURED,
            "Google rejected the OAuth client credentials (invalid_client).",
            "Check client_id/client_secret in client.json or the EURUS_GOOGLE_CLIENT_* "
            "environment variables.",
        )
    if any(marker in lowered for marker in _REDIRECT_MARKERS):
        return Diagnosis(
            ErrorCode.REDIRECT_MISMATCH,
            "The redirect URI does not match any authorized redirect URI of the OAuth client.",
            "Add the exact loopback redirect URI shown by the login command to the OAuth "
            "client, or use a Desktop-app client.",
        )
    if any(marker in lowered for marker in _TEST_USER_MARKERS):
        return Diagnosis(
            ErrorCode.ACCESS_DENIED_TEST_USER,
            "Your Google account is not allowed to use this app (OAuth testing policy).",
            "Add this account as a test user on the OAuth consent screen, or publish the app.",
        )
    if any(marker in lowered for marker in _PREVIEW_MARKERS):
        return Diagnosis(
            ErrorCode.PREVIEW_NOT_AVAILABLE,
            "Google Workspace MCP is a Developer Preview and is not available for this "
            "account or project.",
            "Join the Developer Preview / enable the service for this account, or try a "
            "different Google account.",
        )
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return Diagnosis(
            ErrorCode.TRANSIENT,
            "Google reported a temporary condition (rate limit or temporary backend error).",
            "No action needed; the request is retried automatically.",
            transient=True,
        )
    if status == 401 or any(marker in lowered for marker in _AUTH_MARKERS):
        return Diagnosis(
            ErrorCode.TOKEN_EXPIRED,
            "Google rejected the access token (401 unauthorized).",
            "The token is refreshed automatically; if this persists, re-run: "
            "eurus auth google login.",
            reauth=True,
        )
    if status == 403:
        return Diagnosis(
            ErrorCode.PREVIEW_NOT_AVAILABLE,
            "Google denied access (403). With Workspace MCP this usually means the MCP "
            "service is not enabled, the client is not agentic, or the preview is not "
            "available for this account.",
            "Check the APIs & MCP services in your Google Cloud project and the OAuth "
            "client's 'AI-powered agent' flag.",
        )
    return None


def diagnose(status: int | None = None, body: str = "") -> Diagnosis:
    clean = redact(body or "")
    found = _classify_body(clean, status)
    if found is not None:
        return found
    if status is not None:
        if status == 429 or 500 <= status <= 599:
            return Diagnosis(
                ErrorCode.TRANSIENT,
                f"Google returned HTTP {status}.",
                "No action needed; the request is retried automatically.",
                transient=True,
            )
        if status == 401:
            return Diagnosis(
                ErrorCode.TOKEN_EXPIRED,
                "Google returned HTTP 401 (invalid credentials).",
                "Re-run: eurus auth google login if this persists.",
                reauth=True,
            )
    return Diagnosis(
        ErrorCode.UNKNOWN,
        f"Google returned an unrecognized error ({status or 'no status'}).",
        "Details: " + (clean[:300] or "no error body"),
    )


def diagnose_exception(exc: BaseException) -> Diagnosis:
    for item in _iter_exceptions(exc):
        if _is_network_exception(item):
            name = type(item).__name__
            return Diagnosis(
                ErrorCode.NETWORK,
                f"Network problem while talking to Google ({name}).",
                "Check connectivity. Mobile connections that drop are retried automatically "
                "for read-only calls; retry the request otherwise.",
                transient=True,
            )
    status = status_code_of(exc)
    body = str(exc)
    if status is None and not body:
        return Diagnosis(ErrorCode.UNKNOWN, f"{type(exc).__name__}", "Inspect the logs.")
    return diagnose(status, body)


def is_transport_failure(exc: BaseException) -> bool:
    for item in _iter_exceptions(exc):
        if isinstance(item, asyncio.CancelledError):
            continue
        if _is_network_exception(item):
            return True
        lowered = str(item).lower()
        if (
            "stream closed" in lowered
            or "session expired" in lowered
            or "closedresource" in lowered
            or "remote protocol error" in lowered
        ):
            return True
    return status_code_of(exc) == 404


def refresh_token_expiry_warning(
    issued_at: float,
    publish_status: str,
    clock: Any,
    *,
    lifetime_days: float = 7.0,
    warn_hours: float = 24.0,
) -> str:
    if publish_status != "testing" or issued_at <= 0:
        return ""
    now = float(clock())
    expires_at = issued_at + lifetime_days * 86400.0
    remaining = expires_at - now
    if remaining <= 0:
        return (
            "The Google refresh token has likely expired (consent screen is in Testing "
            "mode, tokens last 7 days). Re-run: eurus auth google login, or publish the "
            "app to Production for tokens that do not expire."
        )
    if remaining <= warn_hours * 3600.0:
        hours = remaining / 3600.0
        return (
            f"The Google refresh token expires in about {hours:.0f}h (consent screen is in "
            "Testing mode, tokens last 7 days). Re-run: eurus auth google login soon, or "
            "publish the app to Production (or Internal for Workspace) to avoid the 7-day expiry."
        )
    return ""


__all__ = [
    "AGENTIC_DEADLINE",
    "Diagnosis",
    "ErrorCode",
    "diagnose",
    "diagnose_exception",
    "is_transport_failure",
    "redact",
    "refresh_token_expiry_warning",
    "status_code_of",
]
