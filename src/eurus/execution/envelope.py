from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from eurus.errors import EurusError
from eurus.runtime.cancellation import CancellationToken


class EnvelopeError(EurusError):
    pass


class NetworkProfile(StrEnum):
    OFFLINE = "offline"
    WEB_READ = "web_read"
    API_ACCESS = "api_access"
    PACKAGE_INSTALL = "package_install"
    RESTRICTED = "restricted"


class IsolationBackend(StrEnum):
    CONTAINER = "container"
    HOST = "host"


DEFAULT_CAPABILITIES = frozenset({"filesystem.read", "filesystem.write", "shell.execute"})

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_./:@=-]{1,256}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+(:[a-z0-9_-]+)?$")


def _validate_id(value: str, label: str, *, allow_empty: bool = False) -> str:
    if not value:
        if allow_empty:
            return value
        raise EnvelopeError(f"envelope {label} must not be empty")
    if len(value) > 128 or not _ID_PATTERN.match(value):
        raise EnvelopeError(f"envelope {label} contains invalid characters")
    return value


def _validate_workspace_id(value: str) -> str:
    if not value or len(value) > 256 or not _WORKSPACE_ID_PATTERN.match(value):
        raise EnvelopeError("envelope workspace_id contains invalid characters")
    return value


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    memory_mb: int = 1024
    cpu_limit: float = 1.0
    pids_limit: int = 128
    timeout_seconds: float = 120.0
    max_output_bytes: int = 20000

    def __post_init__(self) -> None:
        if not 128 <= self.memory_mb <= 65536:
            raise EnvelopeError("budget.memory_mb must be between 128 and 65536")
        if not 0.1 <= self.cpu_limit <= 64:
            raise EnvelopeError("budget.cpu_limit must be between 0.1 and 64")
        if not 16 <= self.pids_limit <= 4096:
            raise EnvelopeError("budget.pids_limit must be between 16 and 4096")
        if not 0 < self.timeout_seconds <= 86400:
            raise EnvelopeError("budget.timeout_seconds must be between 0 and 86400")
        if not 1 <= self.max_output_bytes <= 10_000_000:
            raise EnvelopeError("budget.max_output_bytes must be between 1 and 10000000")


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.allowed_tools is not None:
            object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "denied_tools", frozenset(self.denied_tools))

    def permits(self, tool_name: str) -> bool:
        if tool_name in self.denied_tools:
            return False
        return self.allowed_tools is None or tool_name in self.allowed_tools


@dataclass(frozen=True, slots=True)
class ExecutionEnvelope:
    """Immutable authorization context for a tool execution scope.

    Instances are validated on construction; ``dataclasses.replace`` re-runs
    validation, so a stamped copy (for example with a run id) cannot drift into
    an invalid state after authorization.
    """

    envelope_id: str
    workspace_id: str
    workspace_root: str
    backend: IsolationBackend
    network_profile: NetworkProfile = NetworkProfile.OFFLINE
    capabilities: frozenset[str] = DEFAULT_CAPABILITIES
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    approval_scope: ApprovalScope | None = None
    run_id: str = ""
    attempt: int = 1
    owner_id: str = "local"
    session_id: str | None = None
    cancellation: CancellationToken | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.backend, IsolationBackend):
            raise EnvelopeError("envelope backend must be an IsolationBackend value")
        if not isinstance(self.network_profile, NetworkProfile):
            raise EnvelopeError("envelope network_profile must be a NetworkProfile value")
        _validate_id(self.envelope_id, "envelope_id")
        if not self.workspace_root:
            raise EnvelopeError("envelope workspace_root must not be empty")
        _validate_workspace_id(self.workspace_id)
        _validate_id(self.run_id, "run_id", allow_empty=True)
        if (
            self.backend is IsolationBackend.HOST
            and self.network_profile is not NetworkProfile.OFFLINE
        ):
            raise EnvelopeError(
                "host isolation cannot enforce a network profile; use the container backend"
            )
        for capability in self.capabilities:
            if not _CAPABILITY_PATTERN.match(capability):
                raise EnvelopeError(f"invalid capability name: {capability!r}")
        if not 1 <= self.attempt <= 1_000_000:
            raise EnvelopeError("envelope attempt must be between 1 and 1000000")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise EnvelopeError("envelope ttl_seconds must be positive when set")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    @classmethod
    def authorize(
        cls,
        *,
        workspace_root: str,
        backend: IsolationBackend,
        envelope_id: str | None = None,
        network_profile: NetworkProfile = NetworkProfile.OFFLINE,
        capabilities: frozenset[str] | None = None,
        budget: ResourceBudget | None = None,
        approval_scope: ApprovalScope | None = None,
        run_id: str = "",
        attempt: int = 1,
        owner_id: str = "local",
        session_id: str | None = None,
        cancellation: CancellationToken | None = None,
        ttl_seconds: float | None = None,
    ) -> ExecutionEnvelope:
        if capabilities is None:
            capabilities = DEFAULT_CAPABILITIES
        identifier = envelope_id or f"env-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        return cls(
            envelope_id=identifier,
            workspace_id=workspace_root,
            workspace_root=workspace_root,
            backend=backend,
            network_profile=network_profile,
            capabilities=frozenset(capabilities),
            budget=budget or ResourceBudget(),
            approval_scope=approval_scope,
            run_id=run_id,
            attempt=attempt,
            owner_id=owner_id,
            session_id=session_id,
            cancellation=cancellation,
            ttl_seconds=ttl_seconds,
        )

    def with_run(self, run_id: str, attempt: int = 1) -> ExecutionEnvelope:
        if not run_id:
            raise EnvelopeError("run binding requires a run id")
        return replace(self, run_id=run_id, attempt=attempt)

    def expired(self, now: datetime | None = None) -> bool:
        if self.ttl_seconds is None:
            return False
        reference = now or datetime.now(UTC)
        created = self.created_at if self.created_at.tzinfo else self.created_at.replace(tzinfo=UTC)
        return (reference - created).total_seconds() > self.ttl_seconds


def envelope_violation(
    envelope: ExecutionEnvelope,
    *,
    tool_name: str,
    required_capabilities: tuple[str, ...] | frozenset[str] = (),
    run_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    if envelope.cancellation is not None and envelope.cancellation.cancelled:
        return "execution envelope was cancelled"
    if envelope.expired(now):
        return "execution envelope expired; request a fresh authorization"
    if envelope.run_id and run_id and run_id != envelope.run_id:
        return "tool call run id does not match the execution envelope"
    missing = sorted(set(required_capabilities) - envelope.capabilities)
    if missing:
        return "missing capabilities: " + ", ".join(missing)
    if envelope.approval_scope is not None and not envelope.approval_scope.permits(tool_name):
        return "tool is not permitted by the envelope approval scope"
    return None
