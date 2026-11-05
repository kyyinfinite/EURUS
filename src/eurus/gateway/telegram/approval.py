from __future__ import annotations

from eurus.gateway.approval import (
    MAX_APPROVAL_TIMEOUT,
    MAX_DISPLAYED_ARGUMENT_CHARS,
    TOKEN_PATTERN,
    ApprovalBroker,
    ApprovalContext,
    ApprovalStateCallback,
    Notifier,
    PendingApproval,
    current_approval_context,
    parse_approval_timeout,
)

TelegramApprovalBroker = ApprovalBroker

__all__ = [
    "MAX_APPROVAL_TIMEOUT",
    "MAX_DISPLAYED_ARGUMENT_CHARS",
    "TOKEN_PATTERN",
    "ApprovalBroker",
    "ApprovalContext",
    "ApprovalStateCallback",
    "Notifier",
    "PendingApproval",
    "TelegramApprovalBroker",
    "current_approval_context",
    "parse_approval_timeout",
]
