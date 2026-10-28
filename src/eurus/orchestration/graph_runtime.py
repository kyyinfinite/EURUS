from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from eurus.orchestration.graph import TaskGraph, TaskNode
from eurus.runtime.cancellation import CancellationToken
from eurus.runtime.contracts import VerificationStatus

log = logging.getLogger(__name__)

NodeExecutor = Callable[[TaskNode, dict[str, dict[str, Any]]], Awaitable[dict[str, Any]]]


class GraphMutationError(ValueError):
    pass


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


ACTIVE_NODE_STATUSES = frozenset({NodeStatus.PENDING, NodeStatus.RUNNING})
TERMINAL_NODE_STATUSES = frozenset(
    {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class NodeRetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")


DEFAULT_RETRY_POLICY = NodeRetryPolicy()


@dataclass(slots=True)
class NodeState:
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    last_error: str = ""
    result: dict[str, Any] | None = None
    verified: bool = False
    started_at: str | None = None
    finished_at: str | None = None

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_error": self.last_error[:500],
            "result": self.result,
            "verified": self.verified,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> NodeState:
        status = str(data.get("status", NodeStatus.PENDING.value))
        if status not in {item.value for item in NodeStatus}:
            raise GraphMutationError(f"unknown node status in snapshot: {status!r}")
        result = data.get("result")
        return cls(
            node_id=str(data["node_id"]),
            status=NodeStatus(status),
            attempts=max(0, int(data.get("attempts", 0))),
            last_error=str(data.get("last_error", "")),
            result=dict(result) if isinstance(result, dict) else None,
            verified=bool(data.get("verified", False)),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )


def _validate_acyclic(nodes: dict[str, TaskNode]) -> None:
    pending = {node_id: set(node.dependencies) for node_id, node in nodes.items()}
    ordered: list[str] = []
    while pending:
        ready = [node_id for node_id, deps in pending.items() if not deps]
        if not ready:
            raise GraphMutationError("dependency cycle detected")
        for node_id in ready:
            ordered.append(node_id)
            pending.pop(node_id)
            for deps in pending.values():
                deps.discard(node_id)


class HarnessGraph:
    def __init__(self, graph: TaskGraph, *, retry_policy: NodeRetryPolicy | None = None) -> None:
        self._nodes: dict[str, TaskNode] = graph.by_id()
        self._order: tuple[str, ...] = graph.topological_order()
        self.retry_policy = retry_policy or DEFAULT_RETRY_POLICY
        self.states: dict[str, NodeState] = {
            node_id: NodeState(node_id=node_id) for node_id in self._nodes
        }
        self._mutation_lock = asyncio.Lock()
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def node(self, node_id: str) -> TaskNode:
        return self._nodes[node_id]

    def state(self, node_id: str) -> NodeState:
        return self.states[node_id]

    def topological_order(self) -> tuple[str, ...]:
        return self._order

    def dependencies_satisfied(self, node: TaskNode) -> tuple[bool, str]:
        for dep in node.dependencies:
            dep_state = self.states.get(dep)
            if dep_state is None:
                return False, f"dependency {dep!r} is unknown"
            if dep_state.status is not NodeStatus.SUCCEEDED:
                return False, f"dependency {dep!r} is {dep_state.status.value}"
            if getattr(node, "require_verified_deps", False) and not dep_state.verified:
                return False, f"dependency {dep!r} is not verified"
        return True, ""

    def ready_nodes(self) -> list[TaskNode]:
        ready: list[TaskNode] = []
        for node_id in self._order:
            state = self.states[node_id]
            if state.status is not NodeStatus.PENDING:
                continue
            node = self._nodes[node_id]
            satisfied, _ = self.dependencies_satisfied(node)
            if satisfied:
                ready.append(node)
        return ready

    def blocked_dependents(self, failed_node_id: str) -> list[str]:
        blocked: list[str] = []
        for node_id in self._order:
            if failed_node_id in self._nodes[node_id].dependencies:
                state = self.states[node_id]
                if state.status is NodeStatus.PENDING:
                    blocked.append(node_id)
        return blocked

    def mark_running(self, node_id: str) -> None:
        state = self.states[node_id]
        if state.status is not NodeStatus.PENDING:
            raise GraphMutationError(
                f"node {node_id!r} cannot start from status {state.status.value}"
            )
        state.status = NodeStatus.RUNNING
        state.attempts += 1
        state.started_at = _utcnow()
        self._version += 1

    def mark_retry_wait(self, node_id: str, error: str) -> None:
        state = self.states[node_id]
        if state.status is not NodeStatus.RUNNING:
            raise GraphMutationError(
                f"node {node_id!r} cannot retry from status {state.status.value}"
            )
        state.status = NodeStatus.PENDING
        state.last_error = error[:500]
        self._version += 1

    def mark_succeeded(self, node_id: str, result: dict[str, Any], verified: bool) -> None:
        state = self.states[node_id]
        if state.status is not NodeStatus.RUNNING:
            raise GraphMutationError(
                f"node {node_id!r} cannot succeed from status {state.status.value}"
            )
        state.status = NodeStatus.SUCCEEDED
        state.result = result
        state.verified = verified
        state.finished_at = _utcnow()
        self._version += 1

    def mark_failed(self, node_id: str, error: str) -> None:
        state = self.states[node_id]
        if state.status not in {NodeStatus.RUNNING, NodeStatus.PENDING}:
            raise GraphMutationError(
                f"node {node_id!r} cannot fail from status {state.status.value}"
            )
        state.status = NodeStatus.FAILED
        state.last_error = error[:500]
        state.finished_at = _utcnow()
        self._version += 1

    def mark_skipped(self, node_id: str, reason: str) -> None:
        state = self.states[node_id]
        if state.status is not NodeStatus.PENDING:
            raise GraphMutationError(
                f"node {node_id!r} cannot be skipped from status {state.status.value}"
            )
        state.status = NodeStatus.SKIPPED
        state.last_error = reason[:500]
        state.finished_at = _utcnow()
        self._version += 1

    def mark_cancelled(self, node_id: str) -> None:
        state = self.states[node_id]
        if state.status in TERMINAL_NODE_STATUSES:
            return
        state.status = NodeStatus.CANCELLED
        state.finished_at = _utcnow()
        self._version += 1

    async def add_node(self, node: TaskNode) -> None:
        async with self._mutation_lock:
            if node.node_id in self._nodes:
                raise GraphMutationError(f"node {node.node_id!r} already exists")
            candidate = dict(self._nodes)
            candidate[node.node_id] = node
            _validate_acyclic(candidate)
            self._nodes = candidate
            self._order = TaskGraph(tuple(candidate.values())).topological_order()
            self.states[node.node_id] = NodeState(node_id=node.node_id)
            self._version += 1

    async def add_dependencies(self, node_id: str, dependencies: tuple[str, ...]) -> None:
        async with self._mutation_lock:
            node = self._nodes.get(node_id)
            if node is None:
                raise GraphMutationError(f"unknown node {node_id!r}")
            if self.states[node_id].status is not NodeStatus.PENDING:
                raise GraphMutationError(
                    f"node {node_id!r} is {self.states[node_id].status.value}; "
                    "dependencies can only be added to pending nodes"
                )
            for dep in dependencies:
                if dep not in self._nodes:
                    raise GraphMutationError(f"unknown dependency {dep!r}")
                if dep in node.dependencies:
                    raise GraphMutationError(
                        f"dependency {dep!r} already exists on node {node_id!r}"
                    )
                dep_state = self.states[dep]
                if dep_state.status not in {NodeStatus.PENDING, NodeStatus.SUCCEEDED}:
                    raise GraphMutationError(
                        f"dependency {dep!r} is {dep_state.status.value} and cannot be required"
                    )
            merged = tuple(dict.fromkeys((*node.dependencies, *dependencies)))
            candidate = dict(self._nodes)
            candidate[node_id] = TaskNode(
                node_id=node.node_id,
                objective=node.objective,
                dependencies=merged,
                required=node.required,
                allowed_tools=node.allowed_tools,
                read_scope=node.read_scope,
                write_scope=node.write_scope,
                budget=dict(node.budget),
                timeout_seconds=node.timeout_seconds,
                output_contract=node.output_contract,
            )
            _validate_acyclic(candidate)
            self._nodes = candidate
            self._order = TaskGraph(tuple(candidate.values())).topological_order()
            self._version += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self._version,
            "nodes": {
                node_id: state.to_snapshot() for node_id, state in sorted(self.states.items())
            },
            "order": list(self._order),
        }

    @classmethod
    def restore(
        cls,
        graph: TaskGraph,
        snapshot: dict[str, Any],
        *,
        retry_policy: NodeRetryPolicy | None = None,
    ) -> HarnessGraph:
        instance = cls(graph, retry_policy=retry_policy)
        nodes = snapshot.get("nodes")
        if not isinstance(nodes, dict):
            raise GraphMutationError("snapshot nodes must be an object")
        unknown = set(nodes) - set(instance.states)
        if unknown:
            raise GraphMutationError(f"snapshot references unknown nodes: {sorted(unknown)}")
        for node_id, data in nodes.items():
            if not isinstance(data, dict):
                raise GraphMutationError(f"snapshot entry for {node_id!r} must be an object")
            instance.states[node_id] = NodeState.from_snapshot({"node_id": node_id, **data})
        for state in instance.states.values():
            if state.status is NodeStatus.RUNNING:
                state.status = NodeStatus.PENDING
        instance._version = max(0, int(snapshot.get("version", 0)))
        return instance


def _utcnow() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    completed_nodes: tuple[str, ...]
    failed_nodes: tuple[str, ...]
    skipped_nodes: tuple[str, ...]
    cancelled: bool
    attempts: dict[str, int] = field(default_factory=dict)


class GraphScheduler:
    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        cancellation: CancellationToken | None = None,
        checkpoint_writer: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        progress_listener: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None,
        retry_policy: NodeRetryPolicy | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.max_concurrency = max_concurrency
        self.cancellation = cancellation or CancellationToken()
        self.checkpoint_writer = checkpoint_writer
        self.progress_listener = progress_listener
        self.retry_policy = retry_policy or DEFAULT_RETRY_POLICY
        self._node_locks: dict[str, asyncio.Lock] = {}

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.progress_listener is None:
            return
        try:
            recorded = self.progress_listener(event_type, payload)
            if inspect.isawaitable(recorded):
                await recorded
        except Exception:
            log.warning("graph progress listener failed", exc_info=True)

    async def _checkpoint(self, harness: HarnessGraph) -> None:
        if self.checkpoint_writer is None:
            return
        try:
            written = self.checkpoint_writer(harness.snapshot())
            if inspect.isawaitable(written):
                await written
        except Exception:
            log.warning("graph checkpoint write failed", exc_info=True)

    def _retry_policy_for(self, node: TaskNode) -> NodeRetryPolicy:
        raw = node.budget.get("max_attempts") if node.budget else None
        if raw is None:
            return self.retry_policy
        try:
            attempts = max(1, int(raw))
        except (TypeError, ValueError):
            return self.retry_policy
        return NodeRetryPolicy(
            max_attempts=attempts,
            backoff_seconds=float(
                node.budget.get("backoff_seconds", self.retry_policy.backoff_seconds)
            ),
        )

    async def run(
        self,
        graph: TaskGraph,
        executor: NodeExecutor,
        *,
        harness: HarnessGraph | None = None,
    ) -> SchedulerReport:
        harness = harness or HarnessGraph(graph, retry_policy=self.retry_policy)
        semaphore = asyncio.Semaphore(self.max_concurrency)
        running: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                self.cancellation.raise_if_cancelled()
                await self._propagate_dependency_failures(harness)
                ready = harness.ready_nodes()
                for node in ready:
                    if node.node_id in running:
                        continue
                    lock = self._node_locks.setdefault(node.node_id, asyncio.Lock())
                    if lock.locked():
                        continue
                    running[node.node_id] = asyncio.create_task(
                        self._run_node(harness, node, executor, semaphore),
                        name=f"eurus-node-{node.node_id}",
                    )
                if not running:
                    break
                done, _ = await asyncio.wait(
                    set(running.values()), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    name = task.get_name()
                    node_id = name.removeprefix("eurus-node-")
                    running.pop(node_id, None)
                    exc = task.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        log.warning("node task crashed: %s", exc)
                await self._checkpoint(harness)
            self.cancellation.raise_if_cancelled()
        except asyncio.CancelledError:
            self.cancellation.cancel()
            for task in running.values():
                if not task.done():
                    task.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)
            for node_id in harness.states:
                harness.mark_cancelled(node_id)
            await self._checkpoint(harness)
            raise
        succeeded = tuple(
            node_id
            for node_id, state in harness.states.items()
            if state.status is NodeStatus.SUCCEEDED
        )
        failed = tuple(
            node_id
            for node_id, state in harness.states.items()
            if state.status is NodeStatus.FAILED
        )
        skipped = tuple(
            node_id
            for node_id, state in harness.states.items()
            if state.status is NodeStatus.SKIPPED
        )
        return SchedulerReport(
            completed_nodes=succeeded,
            failed_nodes=failed,
            skipped_nodes=skipped,
            cancelled=False,
            attempts={node_id: state.attempts for node_id, state in harness.states.items()},
        )

    async def _propagate_dependency_failures(self, harness: HarnessGraph) -> None:
        changed = True
        while changed:
            changed = False
            for node_id in harness.topological_order():
                state = harness.states[node_id]
                if state.status is not NodeStatus.PENDING:
                    continue
                node = harness.node(node_id)
                blocked, reason = harness.dependencies_satisfied(node)
                if blocked:
                    continue
                dep_failed = False
                for dep in node.dependencies:
                    dep_state = harness.states[dep]
                    if dep_state.status in {
                        NodeStatus.FAILED,
                        NodeStatus.SKIPPED,
                        NodeStatus.CANCELLED,
                    }:
                        dep_failed = True
                if dep_failed:
                    harness.mark_skipped(
                        node_id, f"dependency failed: {reason or 'upstream failure'}"
                    )
                    await self._emit(
                        "graph.node.skipped", {"node_id": node_id, "reason": "dependency failed"}
                    )
                    changed = True

    async def _run_node(
        self,
        harness: HarnessGraph,
        node: TaskNode,
        executor: NodeExecutor,
        semaphore: asyncio.Semaphore,
    ) -> None:
        policy = self._retry_policy_for(node)
        while True:
            self.cancellation.raise_if_cancelled()
            lock = self._node_locks.setdefault(node.node_id, asyncio.Lock())
            async with lock:
                harness.mark_running(node.node_id)
            await self._emit(
                "graph.node.started",
                {"node_id": node.node_id, "attempt": harness.state(node.node_id).attempts},
            )
            dep_results = {dep: harness.states[dep].result or {} for dep in node.dependencies}
            async with semaphore:
                try:
                    raw = await asyncio.wait_for(
                        executor(node, dep_results), timeout=node.timeout_seconds
                    )
                except asyncio.CancelledError:
                    harness.mark_cancelled(node.node_id)
                    await self._emit("graph.node.cancelled", {"node_id": node.node_id})
                    raise
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    state = harness.state(node.node_id)
                    if state.attempts < policy.max_attempts:
                        harness.mark_retry_wait(node.node_id, error)
                        await self._emit(
                            "graph.node.retry",
                            {
                                "node_id": node.node_id,
                                "attempt": state.attempts,
                                "error": error[:200],
                            },
                        )
                        if policy.backoff_seconds > 0:
                            await asyncio.sleep(policy.backoff_seconds)
                        continue
                    harness.mark_failed(node.node_id, error)
                    await self._emit(
                        "graph.node.failed", {"node_id": node.node_id, "error": error[:200]}
                    )
                    return
            status = str(raw.get("status", "completed"))
            verified = bool(raw.get("verified", False))
            if status == "completed" and verified:
                harness.mark_succeeded(node.node_id, dict(raw), verified=True)
                await self._emit("graph.node.succeeded", {"node_id": node.node_id})
            else:
                state = harness.state(node.node_id)
                error = str(raw.get("error", "")) or "node result not verified"
                if state.attempts < policy.max_attempts:
                    harness.mark_retry_wait(node.node_id, error)
                    await self._emit(
                        "graph.node.retry",
                        {"node_id": node.node_id, "attempt": state.attempts, "error": error[:200]},
                    )
                    if policy.backoff_seconds > 0:
                        await asyncio.sleep(policy.backoff_seconds)
                    continue
                harness.mark_failed(node.node_id, error)
                await self._emit(
                    "graph.node.failed", {"node_id": node.node_id, "error": error[:200]}
                )
            return


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def verification_from_node_state(state: NodeState) -> VerificationStatus:
    if state.status is NodeStatus.SUCCEEDED:
        return VerificationStatus.VERIFIED if state.verified else VerificationStatus.UNVERIFIED
    if state.status is NodeStatus.FAILED:
        return VerificationStatus.FAILED
    return VerificationStatus.NOT_RUN
