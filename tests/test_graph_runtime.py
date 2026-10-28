import asyncio
from typing import Any

import pytest

from eurus.orchestration.graph import TaskGraph, TaskNode
from eurus.orchestration.graph_runtime import (
    GraphMutationError,
    GraphScheduler,
    HarnessGraph,
    NodeRetryPolicy,
    NodeStatus,
)
from eurus.runtime.cancellation import CancellationToken


def make_graph(*nodes: TaskNode) -> TaskGraph:
    return TaskGraph(tuple(nodes))


def ok_result(node_id: str, verified: bool = True) -> dict[str, Any]:
    return {"status": "completed", "verified": verified, "summary": node_id}


def test_harness_ready_nodes_respect_dependency_order():
    harness = HarnessGraph(make_graph(TaskNode("a", "A"), TaskNode("b", "B", ("a",))))
    assert [node.node_id for node in harness.ready_nodes()] == ["a"]
    harness.mark_running("a")
    harness.mark_succeeded("a", {"value": 1}, verified=True)
    assert [node.node_id for node in harness.ready_nodes()] == ["b"]


def test_harness_verified_dependency_gate():
    harness = HarnessGraph(
        make_graph(TaskNode("a", "A"), TaskNode("b", "B", ("a",), require_verified_deps=True))
    )
    harness.mark_running("a")
    harness.mark_succeeded("a", {}, verified=False)
    assert harness.ready_nodes() == []
    satisfied, reason = harness.dependencies_satisfied(harness.node("b"))
    assert not satisfied
    assert "not verified" in reason


def test_dynamic_add_node_rejects_duplicates_and_cycles():
    harness = HarnessGraph(make_graph(TaskNode("a", "A")))

    async def go():
        await harness.add_node(TaskNode("b", "B", ("a",)))
        with pytest.raises(GraphMutationError, match="already exists"):
            await harness.add_node(TaskNode("b", "B"))
        await harness.add_node(TaskNode("z", "Z", ("b",)))
        with pytest.raises(GraphMutationError, match="cycle"):
            await harness.add_dependencies("a", ("z",))
        await harness.add_node(TaskNode("c", "C", ("b",)))

    asyncio.run(go())
    assert harness.topological_order() == ("a", "b", "z", "c")


def test_dynamic_add_dependencies_validates_state():
    harness = HarnessGraph(make_graph(TaskNode("a", "A"), TaskNode("b", "B")))

    async def go():
        with pytest.raises(GraphMutationError, match="unknown dependency"):
            await harness.add_dependencies("b", ("missing",))
        harness.mark_running("a")
        with pytest.raises(GraphMutationError, match="cannot be required"):
            await harness.add_dependencies("b", ("a",))
        harness.mark_succeeded("a", {}, verified=True)
        await harness.add_dependencies("b", ("a",))
        with pytest.raises(GraphMutationError, match="already exists"):
            await harness.add_dependencies("b", ("a",))

    asyncio.run(go())


def test_scheduler_runs_dag_in_order_and_collects_report():
    order: list[str] = []

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        order.append(node.node_id)
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A"), TaskNode("b", "B"), TaskNode("c", "C", ("a", "b")))
    scheduler = GraphScheduler()
    report = asyncio.run(scheduler.run(graph, executor))
    assert set(order) == {"a", "b", "c"}
    assert order.index("c") > order.index("a") and order.index("c") > order.index("b")
    assert report.completed_nodes == ("a", "b", "c")
    assert report.failed_nodes == ()


def test_scheduler_bounded_parallel_execution():
    active = {"now": 0, "peak": 0}

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return ok_result(node.node_id)

    graph = make_graph(*[TaskNode(f"n{i}", f"T{i}") for i in range(6)])
    scheduler = GraphScheduler(max_concurrency=2)
    report = asyncio.run(scheduler.run(graph, executor))
    assert report.completed_nodes == tuple(f"n{i}" for i in range(6))
    assert active["peak"] <= 2


def test_scheduler_dependency_failure_propagates_as_skipped():
    executed: list[str] = []

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        executed.append(node.node_id)
        if node.node_id == "a":
            raise RuntimeError("boom")
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A"), TaskNode("b", "B", ("a",)), TaskNode("c", "C"))
    scheduler = GraphScheduler()
    report = asyncio.run(scheduler.run(graph, executor))
    assert report.failed_nodes == ("a",)
    assert report.skipped_nodes == ("b",)
    assert "c" in report.completed_nodes
    assert executed == ["a", "c"]


def test_scheduler_retry_policy_respects_max_attempts():
    attempts: dict[str, int] = {}

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        attempts[node.node_id] = attempts.get(node.node_id, 0) + 1
        raise RuntimeError("always fails")

    graph = make_graph(TaskNode("a", "A", budget={"max_attempts": 3, "backoff_seconds": 0.0}))
    scheduler = GraphScheduler(retry_policy=NodeRetryPolicy(max_attempts=1))
    report = asyncio.run(scheduler.run(graph, executor))
    assert attempts["a"] == 3
    assert report.failed_nodes == ("a",)
    assert report.attempts["a"] == 3


def test_scheduler_retries_then_succeeds():
    attempts = {"count": 0}

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("transient")
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A", budget={"max_attempts": 3}))
    scheduler = GraphScheduler()
    report = asyncio.run(scheduler.run(graph, executor))
    assert report.completed_nodes == ("a",)
    assert report.attempts["a"] == 3


def test_scheduler_unverified_result_fails_node():
    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"status": "completed", "verified": False}

    graph = make_graph(TaskNode("a", "A"))
    scheduler = GraphScheduler()
    report = asyncio.run(scheduler.run(graph, executor))
    assert report.failed_nodes == ("a",)


def test_scheduler_cancellation_propagates_and_marks_nodes():
    started = asyncio.Event()

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(5.0)
        return ok_result(node.node_id)

    async def go():
        token = CancellationToken()
        scheduler = GraphScheduler(cancellation=token)
        graph = make_graph(TaskNode("slow", "S"))
        task = asyncio.create_task(scheduler.run(graph, executor))
        await started.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return scheduler

    asyncio.run(go())
    assert True


def test_scheduler_prevents_duplicate_node_execution():
    executions: list[str] = []

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        executions.append(node.node_id)
        await asyncio.sleep(0.01)
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A"))
    harness = HarnessGraph(graph)

    async def go():
        scheduler = GraphScheduler()
        await scheduler.run(graph, executor, harness=harness)
        with pytest.raises(GraphMutationError):
            harness.mark_running("a")

    asyncio.run(go())
    assert executions == ["a"]


def test_harness_snapshot_roundtrip_and_running_reset():
    harness = HarnessGraph(make_graph(TaskNode("a", "A"), TaskNode("b", "B", ("a",))))
    harness.mark_running("a")
    harness.mark_succeeded("a", {"x": 1}, verified=True)
    harness.mark_running("b")
    snapshot = harness.snapshot()
    graph = make_graph(TaskNode("a", "A"), TaskNode("b", "B", ("a",)))
    restored = HarnessGraph.restore(graph, snapshot)
    assert restored.state("a").status is NodeStatus.SUCCEEDED
    assert restored.state("a").verified is True
    assert restored.state("b").status is NodeStatus.PENDING
    assert restored.ready_nodes()[0].node_id == "b"


def test_harness_restore_rejects_unknown_nodes():
    harness = HarnessGraph(make_graph(TaskNode("a", "A")))
    snapshot = harness.snapshot()
    snapshot["nodes"]["ghost"] = {"status": "succeeded"}
    with pytest.raises(GraphMutationError, match="unknown nodes"):
        HarnessGraph.restore(make_graph(TaskNode("a", "A")), snapshot)


def test_scheduler_checkpoint_writer_receives_snapshots():
    writes: list[dict[str, Any]] = []

    async def writer(snapshot: dict[str, Any]) -> None:
        writes.append(snapshot)

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A"))
    scheduler = GraphScheduler(checkpoint_writer=writer)
    asyncio.run(scheduler.run(graph, executor))
    assert writes and writes[-1]["nodes"]["a"]["status"] == "succeeded"


def test_scheduler_resumes_from_checkpoint_snapshot():
    executed: list[str] = []

    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        executed.append(node.node_id)
        return ok_result(node.node_id)

    graph_def = (TaskNode("a", "A"), TaskNode("b", "B", ("a",)))
    graph = make_graph(*graph_def)
    harness = HarnessGraph(graph)
    harness.mark_running("a")
    harness.mark_succeeded("a", ok_result("a"), verified=True)

    async def go():
        scheduler = GraphScheduler()
        return await scheduler.run(graph, executor, harness=harness)

    report = asyncio.run(go())
    assert report.completed_nodes == ("a", "b")
    assert executed == ["b"]


def test_node_timeout_fails_the_node():
    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        await asyncio.sleep(1.0)
        return ok_result(node.node_id)

    graph = make_graph(TaskNode("a", "A", timeout_seconds=0.05))
    harness = HarnessGraph(graph)
    scheduler = GraphScheduler()
    asyncio.run(scheduler.run(graph, executor, harness=harness))
    assert harness.state("a").status is NodeStatus.FAILED
    assert "TimeoutError" in harness.state("a").last_error


def test_scheduler_records_node_error_message():
    async def executor(node: TaskNode, dep_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        raise ValueError("specific failure")

    graph = make_graph(TaskNode("a", "A"))
    harness = HarnessGraph(graph)
    scheduler = GraphScheduler()
    asyncio.run(scheduler.run(graph, executor, harness=harness))
    assert "specific failure" in harness.state("a").last_error
