from __future__ import annotations

import asyncio

from eurus.agent.evaluation import (
    FailureKind,
    classify_exception,
    classify_failure,
    is_retryable_exception,
    is_retryable_kind,
    recovery_note,
)
from eurus.errors import (
    AccessDeniedError,
    AllProvidersFailedError,
    AuthenticationError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ToolError,
)
from eurus.models import ToolResult


def _error(output: str, code: str = "") -> ToolResult:
    return ToolResult("1", "shell", output, True, code)


def test_structured_codes_classify_without_substring_matching() -> None:
    assert classify_failure(_error("weird message", "timeout")) is FailureKind.TIMEOUT
    assert classify_failure(_error("anything", "approval_denied")) is FailureKind.PERMISSION_DENIED
    assert classify_failure(_error("anything", "uncertain")) is FailureKind.UNCERTAIN_SIDE_EFFECT
    assert (
        classify_failure(_error("anything", "workspace_violation"))
        is FailureKind.WORKSPACE_VIOLATION
    )
    assert classify_failure(_error("anything", "network_policy")) is FailureKind.NETWORK_POLICY


def test_changed_messages_do_not_break_classification() -> None:
    result = _error("Request ratelimited: too many requests (429)")
    assert classify_failure(result) is FailureKind.TOOL_FAILURE
    assert classify_exception(RateLimitError("too many", retryable=True)) is FailureKind.RATE_LIMIT


def test_marker_fallback_remains_conservative() -> None:
    assert (
        classify_failure(_error("error: the mutation may or may not have been applied"))
        is FailureKind.UNCERTAIN_SIDE_EFFECT
    )
    assert (
        classify_failure(_error("error: no approval provider is configured"))
        is FailureKind.PERMISSION_DENIED
    )
    assert classify_failure(_error("error: timed out after 10s")) is FailureKind.TIMEOUT
    assert classify_failure(_error("error: something novel")) is FailureKind.TOOL_FAILURE


def test_exception_classification_covers_provider_hierarchy() -> None:
    assert classify_exception(AuthenticationError("bad key")) is FailureKind.AUTHENTICATION
    assert classify_exception(AccessDeniedError("forbidden")) is FailureKind.AUTHENTICATION
    assert classify_exception(ProviderUnavailableError("down")) is FailureKind.PROVIDER
    assert classify_exception(asyncio.CancelledError()) is FailureKind.CANCELLATION
    assert classify_exception(TimeoutError()) is FailureKind.TIMEOUT
    assert classify_exception(RuntimeError("mystery")) is FailureKind.UNKNOWN
    assert classify_exception(ToolError("x", code="timeout")) is FailureKind.TIMEOUT


def test_unknown_errors_are_not_retryable() -> None:
    assert is_retryable_exception(RuntimeError("whatever")) is False
    assert is_retryable_kind(FailureKind.UNKNOWN) is False
    assert is_retryable_kind(FailureKind.AUTHENTICATION) is False
    assert is_retryable_kind(FailureKind.RATE_LIMIT) is True
    assert is_retryable_kind(FailureKind.TIMEOUT) is True


def test_provider_retryability_follows_flag() -> None:
    retryable = ProviderError("boom", retryable=True)
    permanent = ProviderError("boom", retryable=False)
    assert is_retryable_exception(retryable) is True
    assert is_retryable_exception(permanent) is False
    aggregate = AllProvidersFailedError([("a", retryable)])
    assert is_retryable_exception(aggregate) is True
    aggregate_bad = AllProvidersFailedError([("a", permanent)])
    assert is_retryable_exception(aggregate_bad) is False


def test_nested_exceptions_preserve_original_type() -> None:
    try:
        try:
            raise RateLimitError("limited", retryable=True)
        except RateLimitError as inner:
            raise ProviderError("wrapper") from inner
    except ProviderError as exc:
        assert classify_exception(exc) is FailureKind.PROVIDER
        assert is_retryable_exception(exc) is False


def test_recovery_note_mentions_action_and_guidance() -> None:
    note = recovery_note(FailureKind.AUTHENTICATION, "api key rejected")
    assert "authentication" in note
    assert "do not retry" in note.lower()
