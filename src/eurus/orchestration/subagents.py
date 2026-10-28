from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from eurus.orchestration.aggregation import AggregationResult, aggregate_results
from eurus.orchestration.concurrency import ConcurrencyLimiter
from eurus.orchestration.graph import TaskGraph, TaskNode
from eurus.orchestration.graph_runtime import (
    GraphScheduler,
    HarnessGraph,
    NodeRetryPolicy,
)
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.cancellation import CancellationToken
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.state_machine import transition_run
from eurus.storage.runs import RunStore

log = logging.getLogger(__name__)

ERROR_POLICIES = {"fail_fast", "continue_independent", "retry_bounded"}

ChildExecutor = Callable[
    [TaskNode, dict[str, dict[str, Any]], CancellationToken], Awaitable[dict[str, Any]]
]


class OrchestrationError(RuntimeError):
    pass


class SubagentOrchestrator:
    def __init__(
        self,
        run_store: RunStore,
        *,
        max_concurrency: int = 4,
        max_child_runs: int = 16,
        allowed_tools: frozenset[str] = frozenset(),
        allow_nested: bool = False,
        default_retries: int = 1,
    ) -> None:
        if max_child_runs < 1:
            raise ValueError("max_child_runs must be at least 1")
        if default_retries < 0:
            raise ValueError("default_retries cannot be negative")
        self.run_store = run_store
        self.max_child_runs = max_child_runs
        self.allowed_tools = allowed_tools
        self.allow_nested = allow_nested
        self.default_retries = default_retries
        self.limiter = ConcurrencyLimiter(max_concurrency)
        self.last_harness: HarnessGraph | None = None

    def list_children(self, parent_run_id: str) -> list[Run]:
        return [
            run
            for run in self.run_store.list_runs(limit=1000)
            if run.parent_run_id == parent_run_id
        ]

    def recover_orphaned_children(self, parent_run_id: str) -> list[str]:
        recovered: list[str] = []
        for child in self.list_children(parent_run_id):
            if child.status == RunStatus.RUNNING:
                transition_run(
                    child, RunStatus.INTERRUPTED, stop_reason="orchestrator_restart_recovery"
                )
                self.run_store.save(child, expected_status_version=child.status_version - 1)
                recovered.append(child.run_id)
        return recovered

    async def execute(
        self,
        parent: Run,
        graph: TaskGraph,
        executor: ChildExecutor,
        *,
        budget: BudgetSpec,
        cancellation: CancellationToken | None = None,
        error_policy: str = "fail_fast",
        verifier: Callable[[AggregationResult], Awaitable[bool]] | None = None,
        resume_snapshot: dict[str, Any] | None = None,
    ) -> AggregationResult:
        if error_policy not in ERROR_POLICIES:
            raise ValueError("unsupported error policy")
        if parent.parent_run_id is not None and not self.allow_nested:
            raise PermissionError("nested delegation is disabled")
        if len(graph.nodes) > min(self.max_child_runs, budget.max_child_runs):
            raise OrchestrationError("child-run budget exceeded")
        for node in graph.nodes:
            if node.allowed_tools and not node.allowed_tools.issubset(self.allowed_tools):
                raise PermissionError(
                    f"node {node.node_id!r} requests tools outside parent allowlist"
                )
        token = cancellation or CancellationToken()
        retry_policy = NodeRetryPolicy(
            max_attempts=self.default_retries + 1 if error_policy == "retry_bounded" else 1,
            backoff_seconds=0.0,
        )
        harness = (
            HarnessGraph.restore(graph, resume_snapshot, retry_policy=retry_policy)
            if resume_snapshot is not None
            else HarnessGraph(graph, retry_policy=retry_policy)
        )
        self.last_harness = harness
        child_runs: dict[str, Run] = {}
        budget_remaining = budget.max_iterations
        budget_lock = asyncio.Lock()
        failures: dict[str, str] = {}

        async def run_child(
            node: TaskNode, dep_results: dict[str, dict[str, Any]]
        ) -> dict[str, Any]:
            nonlocal budget_remaining
            token.raise_if_cancelled()
            async with budget_lock:
                requested = int(
                    node.budget.get(
                        "max_iterations", max(1, budget.max_iterations // len(graph.nodes))
                    )
                )
                if requested < 1 or budget_remaining < 1:
                    raise OrchestrationError("shared iteration budget exhausted")
                allocation = min(requested, budget_remaining)
                budget_remaining -= allocation
            child = Run(
                owner_id=parent.owner_id,
                objective=node.objective,
                parent_run_id=parent.run_id,
                session_id=parent.session_id,
                budget={
                    **node.budget,
                    "max_iterations": allocation,
                    "allowed_tools": sorted(node.allowed_tools),
                },
            )
            self.run_store.create(child)
            child_runs[node.node_id] = child
            transition_run(child, RunStatus.RUNNING)
            self.run_store.save(child, expected_status_version=0)
            try:

                async def invoke() -> dict[str, Any]:
                    return await asyncio.wait_for(
                        executor(node, {**dep_results, **self._completed(harness)}, token),
                        timeout=node.timeout_seconds,
                    )

                raw = await self.limiter.run(invoke)
                return await self._finalize_child(node, child, raw)
            except asyncio.CancelledError:
                if child.status == RunStatus.RUNNING:
                    transition_run(
                        child, RunStatus.CANCELLED, stop_reason="orchestration_cancelled"
                    )
                    self.run_store.save(child, expected_status_version=child.status_version - 1)
                raise
            except Exception as exc:
                if child.status == RunStatus.RUNNING:
                    transition_run(child, RunStatus.FAILED, stop_reason=type(exc).__name__)
                    self.run_store.save(child, expected_status_version=child.status_version - 1)
                failures[node.node_id] = type(exc).__name__
                if error_policy == "fail_fast":
                    token.cancel()
                raise

        scheduler = GraphScheduler(
            max_concurrency=self.limiter.limit,
            cancellation=token,
            progress_listener=self._progress_event,
            retry_policy=retry_policy,
        )
        try:
            await scheduler.run(graph, run_child, harness=harness)
        except asyncio.CancelledError:
            if error_policy == "fail_fast" and failures:
                raise OrchestrationError(
                    f"required child failed under fail_fast policy: {failures}"
                ) from None
            raise
        except Exception as exc:
            if error_policy == "fail_fast" and failures:
                raise OrchestrationError(
                    f"required child failed under fail_fast policy: {failures}"
                ) from exc
            raise
        results = self._collect_results(harness, child_runs)
        aggregate = aggregate_results(
            results, required_node_ids={n.node_id for n in graph.nodes if n.required}
        )
        if (
            aggregate.status == "completed"
            and verifier is not None
            and not await self._verify(verifier, aggregate)
        ):
            return AggregationResult(
                "incomplete", "Parent aggregation verification failed", results, ()
            )
        return aggregate

    @staticmethod
    async def _verify(
        verifier: Callable[[AggregationResult], Awaitable[bool]],
        aggregate: AggregationResult,
    ) -> bool:
        outcome = verifier(aggregate)
        if inspect.isawaitable(outcome):
            return bool(await outcome)
        return bool(outcome)

    def _completed(self, harness: HarnessGraph) -> dict[str, dict[str, Any]]:
        return {
            node_id: state.result or {}
            for node_id, state in harness.states.items()
            if state.status.value == "succeeded"
        }

    def _collect_results(
        self, harness: HarnessGraph, child_runs: dict[str, Run]
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for node_id, state in harness.states.items():
            node = harness.node(node_id)
            if state.status.value == "succeeded":
                raw = state.result or {}
                entry = {
                    "role": str(raw.get("role", node_id)),
                    "status": "completed",
                    "summary": str(raw.get("summary", "")),
                    "evidence_refs": list(raw.get("evidence_refs", [])),
                    "artifact_refs": list(raw.get("artifact_refs", [])),
                    "verified": True,
                    "usage": dict(raw.get("usage", {})),
                    "error_class": None,
                    "run_id": child_runs[node.node_id].run_id
                    if node.node_id in child_runs
                    else None,
                }
            elif state.status.value == "skipped":
                entry = {
                    "role": node_id,
                    "status": "skipped",
                    "summary": state.last_error,
                    "evidence_refs": [],
                    "artifact_refs": [],
                    "verified": False,
                    "usage": {},
                    "error_class": "DependencyFailed",
                    "run_id": None,
                }
            else:
                entry = {
                    "role": node_id,
                    "status": "failed",
                    "summary": "",
                    "evidence_refs": [],
                    "artifact_refs": [],
                    "verified": False,
                    "usage": {},
                    "error_class": state.last_error[:120] or "NodeFailed",
                    "run_id": child_runs[node_id].run_id if node_id in child_runs else None,
                }
            results[node_id] = entry
        return results

    async def _finalize_child(
        self, node: TaskNode, child: Run, raw: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(raw.get("status", "completed"))
        verified = bool(raw.get("verified", False))
        if status == "completed" and verified:
            transition_run(child, RunStatus.VERIFYING)
            self.run_store.save(child, expected_status_version=child.status_version - 1)
            transition_run(
                child, RunStatus.COMPLETED, result_ref=str(raw.get("result_ref", "")) or None
            )
            raw = {**raw, "status": "completed", "verified": True}
        else:
            transition_run(child, RunStatus.FAILED, stop_reason="child_result_unverified")
            raw = {**raw, "status": "failed", "verified": False}
        self.run_store.save(child, expected_status_version=child.status_version - 1)
        return raw

    async def _progress_event(self, event_type: str, payload: dict[str, Any]) -> None:
        log.debug("graph event %s %s", event_type, payload)
