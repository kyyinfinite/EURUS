from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.storage.database import Database
from eurus.workflows.definition import WorkflowValidationError
from eurus.workflows.engine import WorkflowEngine
from eurus.workflows.states import NodeStatus, WorkflowStatus

EVENTS: list[tuple[str, str]] = []


class NodeExecutor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []
        self.behaviors: dict[str, str] = {}

    async def execute(self, job: Any, context: Any) -> str:
        payload = job.payload or {}
        node_id = str(payload.get("node_id"))
        self.executed.append((node_id, job.job_id))
        behavior = self.behaviors.get(node_id)
        if behavior == "fail":
            raise TimeoutError(f"node {node_id} failed")
        if payload.get("node_type") == "tool_task":
            return f"tool result {payload.get('arguments', {}).get('query', '')}"
        return f"done {node_id}"


def build(tmp_path: Path, *, behaviors: dict[str, str] | None = None):
    database = Database(tmp_path / "workflows.sqlite3")
    executor = NodeExecutor()
    if behaviors:
        executor.behaviors = behaviors
    retry = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.01, jitter_fraction=0.0
        )
    )
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    workflows = WorkflowEngine(
        database,
        automation,
        event_publisher=lambda event_type, subject_type, subject_id, payload: EVENTS.append(
            (event_type, subject_id)
        ),
    )
    return workflows, automation, executor, database


async def drain(
    workflows: WorkflowEngine, automation: AutomationEngine, *, rounds: int = 15
) -> None:
    for _ in range(rounds):
        workflows.tick()
        worked = False
        for _ in range(10):
            if await automation.run_worker():
                worked = True
            else:
                break
        if not worked:
            await asyncio.sleep(0.02)


def register_pipeline(workflows: WorkflowEngine) -> None:
    workflows.register_definition(
        "report-pipeline",
        "Report pipeline",
        [
            {
                "node_id": "fetch",
                "node_type": "agent_task",
                "config": {"objective": "Fetch data for {{inputs.topic}}"},
            },
            {
                "node_id": "check",
                "node_type": "condition",
                "depends_on": ["fetch"],
                "config": {"path": "nodes.fetch.output", "op": "contains", "value": "done"},
            },
            {
                "node_id": "write",
                "node_type": "agent_task",
                "depends_on": ["check"],
                "only_if": [{"node": "check", "output": "true"}],
                "config": {"objective": "Write the report from {{nodes.fetch.output}}"},
            },
            {
                "node_id": "skip_branch",
                "node_type": "agent_task",
                "depends_on": ["check"],
                "only_if": [{"node": "check", "output": "false"}],
                "config": {"objective": "Handle missing data"},
            },
        ],
        required_inputs=("topic",),
    )


async def test_workflow_dag_execution_with_branching(tmp_path: Path) -> None:
    workflows, automation, executor, _ = build(tmp_path)
    register_pipeline(workflows)
    instance = workflows.start("report-pipeline", "owner", inputs={"topic": "eurus"})
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["fetch"].status == NodeStatus.SUCCEEDED
    assert nodes["check"].status == NodeStatus.SUCCEEDED
    assert nodes["check"].output == "true"
    assert nodes["write"].status == NodeStatus.SUCCEEDED
    assert nodes["skip_branch"].status == NodeStatus.SKIPPED
    assert any(event == "WORKFLOW_COMPLETED" for event, _ in EVENTS)
    assert ("fetch", nodes["fetch"].job_id) in executor.executed


async def test_workflow_branch_taken_when_condition_false(tmp_path: Path) -> None:
    workflows, automation, executor, _ = build(tmp_path)
    workflows.register_definition(
        "conditional",
        "Conditional",
        [
            {
                "node_id": "source",
                "node_type": "transform",
                "config": {"set": {"mode": "compact"}},
            },
            {
                "node_id": "check",
                "node_type": "condition",
                "depends_on": ["source"],
                "config": {"path": "vars.mode", "op": "equals", "value": "full"},
            },
            {
                "node_id": "full_path",
                "node_type": "agent_task",
                "depends_on": ["check"],
                "only_if": [{"node": "check", "output": "true"}],
                "config": {"objective": "Full path"},
            },
            {
                "node_id": "compact_path",
                "node_type": "agent_task",
                "depends_on": ["check"],
                "only_if": [{"node": "check", "output": "false"}],
                "config": {"objective": "Compact path"},
            },
        ],
    )
    instance = workflows.start("conditional", "owner")
    await drain(workflows, automation)
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["check"].output == "false"
    assert nodes["full_path"].status == NodeStatus.SKIPPED
    assert nodes["compact_path"].status == NodeStatus.SUCCEEDED


async def test_workflow_parallel_and_join(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "fanout",
        "Fan out",
        [
            {"node_id": "fan", "node_type": "parallel"},
            {
                "node_id": "left",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "Left branch"},
            },
            {
                "node_id": "right",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "Right branch"},
            },
            {
                "node_id": "sync",
                "node_type": "join",
                "depends_on": ["left", "right"],
            },
            {
                "node_id": "final",
                "node_type": "agent_task",
                "depends_on": ["sync"],
                "config": {"objective": "Combine {{nodes.left.output}} and {{nodes.right.output}}"},
            },
        ],
    )
    instance = workflows.start("fanout", "owner")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["final"].status == NodeStatus.SUCCEEDED
    final_job = automation.jobs.get(nodes["final"].job_id or "")
    assert final_job is not None
    assert "done left" in final_job.objective
    assert "done right" in final_job.objective


async def test_workflow_validation_node_fails_instance(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "checked",
        "Checked",
        [
            {"node_id": "work", "node_type": "agent_task", "config": {"objective": "Do work"}},
            {
                "node_id": "verify",
                "node_type": "validation",
                "depends_on": ["work"],
                "config": {
                    "assertions": [
                        {"path": "nodes.work.output", "op": "equals", "value": "impossible"}
                    ]
                },
            },
        ],
    )
    instance = workflows.start("checked", "owner")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.FAILED
    assert final.error is not None
    assert "verify" in final.error


async def test_workflow_node_failure_retries_then_fails(tmp_path: Path) -> None:
    workflows, automation, executor, _ = build(tmp_path, behaviors={"flaky": "fail"})
    workflows.register_definition(
        "flaky-flow",
        "Flaky flow",
        [
            {"node_id": "flaky", "node_type": "agent_task", "config": {"objective": "Flaky step"}},
            {
                "node_id": "after",
                "node_type": "agent_task",
                "depends_on": ["flaky"],
                "config": {"objective": "After"},
            },
        ],
    )
    instance = workflows.start("flaky-flow", "owner")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.FAILED
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["flaky"].status == NodeStatus.FAILED
    assert nodes["after"].status == NodeStatus.SKIPPED
    flaky_attempts = [job for node, job in executor.executed if node == "flaky"]
    assert len(flaky_attempts) >= 2


async def test_workflow_retry_recovers_after_failure(tmp_path: Path) -> None:
    workflows, automation, executor, _ = build(tmp_path)
    workflows.register_definition(
        "self-heal",
        "Self heal",
        [
            {"node_id": "first", "node_type": "agent_task", "config": {"objective": "First"}},
        ],
    )
    instance = workflows.start("self-heal", "owner")
    executor.behaviors["first"] = "fail"
    workflows.tick()
    await automation.run_worker()
    executor.behaviors.pop("first")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED


async def test_workflow_approval_gate(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "gated",
        "Gated",
        [
            {"node_id": "prepare", "node_type": "agent_task", "config": {"objective": "Prepare"}},
            {
                "node_id": "gate",
                "node_type": "approval",
                "depends_on": ["prepare"],
                "config": {"prompt": "Approve publication?"},
            },
            {
                "node_id": "publish",
                "node_type": "agent_task",
                "depends_on": ["gate"],
                "config": {"objective": "Publish"},
            },
        ],
    )
    instance = workflows.start("gated", "owner")
    workflows.tick()
    await automation.run_worker()
    workflows.tick()
    approvals = workflows.store.list_approvals(pending=True)
    assert len(approvals) == 1
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["gate"].status == NodeStatus.WAITING_APPROVAL
    assert nodes["publish"].status == NodeStatus.PENDING
    current = workflows.store.get_instance(instance.instance_id)
    assert current is not None
    assert current.status == WorkflowStatus.WAITING_APPROVAL
    assert workflows.decide_approval(
        approvals[0].approval_id, decision="approved", decided_by="owner"
    )
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["gate"].status == NodeStatus.SUCCEEDED
    assert nodes["publish"].status == NodeStatus.SUCCEEDED
    assert workflows.decide_approval(
        approvals[0].approval_id, decision="approved", decided_by="owner"
    )


async def test_workflow_approval_rejection_fails(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "gated-reject",
        "Gated reject",
        [
            {
                "node_id": "gate",
                "node_type": "approval",
                "config": {"prompt": "Approve?"},
            },
        ],
    )
    instance = workflows.start("gated-reject", "owner")
    workflows.tick()
    approvals = workflows.store.list_approvals(pending=True)
    assert len(approvals) == 1
    assert workflows.decide_approval(
        approvals[0].approval_id, decision="rejected", decided_by="owner"
    )
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.FAILED


async def test_workflow_delay_and_schedule_nodes(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "timed",
        "Timed",
        [
            {"node_id": "start", "node_type": "agent_task", "config": {"objective": "Start"}},
            {
                "node_id": "wait",
                "node_type": "delay",
                "depends_on": ["start"],
                "config": {"seconds": 0.05},
            },
            {
                "node_id": "later",
                "node_type": "agent_task",
                "depends_on": ["wait"],
                "config": {"objective": "Later"},
            },
        ],
    )
    instance = workflows.start("timed", "owner")
    workflows.tick()
    await automation.run_worker()
    workflows.tick()
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["wait"].status == NodeStatus.WAITING_SCHEDULE
    current = workflows.store.get_instance(instance.instance_id)
    assert current is not None
    assert current.status == WorkflowStatus.WAITING_SCHEDULE
    await asyncio.sleep(0.06)
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED


async def test_workflow_transform_passes_values(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "shaped",
        "Shaped",
        [
            {
                "node_id": "shape",
                "node_type": "transform",
                "config": {"set": {"summary": "topic is {{inputs.topic}}", "count": 2}},
            },
            {
                "node_id": "use",
                "node_type": "agent_task",
                "depends_on": ["shape"],
                "config": {"objective": "Summarize {{vars.summary}} with {{vars.count}} points"},
            },
        ],
        required_inputs=("topic",),
    )
    instance = workflows.start("shaped", "owner", inputs={"topic": "agents"})
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    assert final.context["vars"]["summary"] == "topic is agents"
    assert final.context["vars"]["count"] == 2


async def test_workflow_tool_task_scopes_tools(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "toolish",
        "Toolish",
        [
            {
                "node_id": "lookup",
                "node_type": "tool_task",
                "config": {"tool": "web_search", "arguments": {"query": "{{inputs.query}}"}},
            },
        ],
        required_inputs=("query",),
    )
    instance = workflows.start("toolish", "owner", inputs={"query": "agent runtimes"})
    workflows.tick()
    jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "workflow_node"
    ]
    assert len(jobs) == 1
    assert jobs[0].payload["allowed_tools"] == ["web_search"]
    assert jobs[0].payload["arguments"]["query"] == "agent runtimes"
    await automation.run_worker()
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["lookup"].status == NodeStatus.SUCCEEDED
    assert "tool result" in nodes["lookup"].output


async def test_workflow_survives_restart(tmp_path: Path) -> None:
    workflows, automation, _, database = build(tmp_path)
    register_pipeline(workflows)
    instance = workflows.start("report-pipeline", "owner", inputs={"topic": "durability"})
    workflows.tick()
    await automation.run_worker()
    database.close()
    reopened = Database(tmp_path / "workflows.sqlite3")
    executor2 = NodeExecutor()
    retry = RetryManager(
        default_policy=RetryPolicy(max_attempts=2, backoff_base_seconds=0.01, jitter_fraction=0.0)
    )
    automation2 = AutomationEngine(reopened, executor2, retry_manager=retry, poll_seconds=0.05)
    workflows2 = WorkflowEngine(reopened, automation2)
    recovered = workflows2.recover()
    assert recovered >= 0
    await drain(workflows2, automation2)
    final = workflows2.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED


async def test_workflow_recover_syncs_completed_jobs(tmp_path: Path) -> None:
    workflows, automation, executor, database = build(tmp_path)
    workflows.register_definition(
        "recoverable",
        "Recoverable",
        [
            {"node_id": "one", "node_type": "agent_task", "config": {"objective": "One"}},
            {
                "node_id": "two",
                "node_type": "agent_task",
                "depends_on": ["one"],
                "config": {"objective": "Two"},
            },
        ],
    )
    instance = workflows.start("recoverable", "owner")
    workflows.tick()
    await automation.run_worker()
    node_one = workflows.store.get_node(instance.instance_id, "one")
    assert node_one is not None
    assert node_one.status == NodeStatus.SUCCEEDED
    database.close()
    reopened = Database(tmp_path / "workflows.sqlite3")
    executor2 = NodeExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=2, backoff_base_seconds=0.01))
    automation2 = AutomationEngine(reopened, executor2, retry_manager=retry, poll_seconds=0.05)
    workflows2 = WorkflowEngine(reopened, automation2)
    workflows2.tick()
    node_two = workflows2.store.get_node(instance.instance_id, "two")
    assert node_two is not None
    assert node_two.status == NodeStatus.RUNNING
    assert node_two.job_id is not None
    claimed = automation2.jobs.claim("sim-worker", lease_seconds=60)
    assert claimed is not None
    assert claimed.job_id == node_two.job_id
    assert (
        automation2.jobs.finish(
            claimed.job_id,
            "sim-worker",
            claimed.lease_version,
            status="completed",
            result="done two",
        )
        is True
    )
    workflows2.recover()
    node_two = workflows2.store.get_node(instance.instance_id, "two")
    assert node_two is not None
    assert node_two.status == NodeStatus.SUCCEEDED


async def test_workflow_cancel_requests_job_cancellation(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "cancellable",
        "Cancellable",
        [
            {"node_id": "work", "node_type": "agent_task", "config": {"objective": "Work"}},
        ],
    )
    instance = workflows.start("cancellable", "owner")
    workflows.tick()
    nodes = {node.node_id: node for node in workflows.store.list_nodes(instance.instance_id)}
    assert nodes["work"].job_id is not None
    cancelled = workflows.cancel(instance.instance_id, "owner")
    assert cancelled is not None
    assert cancelled.status == WorkflowStatus.CANCELLED
    job = automation.jobs.get(nodes["work"].job_id)
    assert job is not None
    assert job.status == "cancelled" or job.cancel_requested


async def test_workflow_pause_resume(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.register_definition(
        "pausable",
        "Pausable",
        [
            {"node_id": "first", "node_type": "agent_task", "config": {"objective": "First"}},
            {
                "node_id": "second",
                "node_type": "agent_task",
                "depends_on": ["first"],
                "config": {"objective": "Second"},
            },
        ],
    )
    instance = workflows.start("pausable", "owner")
    paused = workflows.pause(instance.instance_id, "owner")
    assert paused is not None
    assert paused.status == WorkflowStatus.PAUSED
    report = workflows.tick()
    assert report.instances_advanced == 0
    assert report.jobs_submitted == 0
    resumed = workflows.resume(instance.instance_id, "owner")
    assert resumed is not None
    assert resumed.status == WorkflowStatus.RUNNING
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED


async def test_workflow_compensation_runs_on_failure(tmp_path: Path) -> None:
    workflows, automation, executor, _ = build(tmp_path, behaviors={"broken": "fail"})
    workflows.register_definition(
        "compensating",
        "Compensating",
        [
            {
                "node_id": "provision",
                "node_type": "agent_task",
                "config": {
                    "objective": "Provision resource",
                    "compensation": "Tear down the provisioned resource",
                },
            },
            {
                "node_id": "broken",
                "node_type": "agent_task",
                "depends_on": ["provision"],
                "config": {"objective": "Broken step"},
            },
        ],
    )
    instance = workflows.start("compensating", "owner")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.FAILED
    compensation_jobs = [
        job
        for job in automation.jobs.list_jobs(limit=20)
        if (job.payload or {}).get("kind") == "workflow_compensation"
    ]
    assert len(compensation_jobs) == 1
    assert compensation_jobs[0].job_id.startswith("wfc-")
    assert "Tear down" in compensation_jobs[0].objective


async def test_workflow_versioning_pins_instance(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    first = workflows.register_definition(
        "versioned",
        "Versioned",
        [{"node_id": "only", "node_type": "agent_task", "config": {"objective": "V1"}}],
    )
    assert first.version == 1
    instance = workflows.start("versioned", "owner")
    second = workflows.register_definition(
        "versioned",
        "Versioned",
        [
            {"node_id": "only", "node_type": "agent_task", "config": {"objective": "V2"}},
            {
                "node_id": "extra",
                "node_type": "agent_task",
                "depends_on": ["only"],
                "config": {"objective": "Extra"},
            },
        ],
    )
    assert second.version == 2
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    assert final.version == 1
    node_count = len(workflows.store.list_nodes(instance.instance_id))
    assert node_count == 1


async def test_workflow_required_inputs_enforced(tmp_path: Path) -> None:
    workflows, _, _, _ = build(tmp_path)
    register_pipeline(workflows)
    with pytest.raises(WorkflowValidationError):
        workflows.start("report-pipeline", "owner", inputs={})
    with pytest.raises(KeyError):
        workflows.start("missing-workflow", "owner")


async def test_workflow_definition_validation_rejects_bad_graphs(tmp_path: Path) -> None:
    workflows, _, _, _ = build(tmp_path)
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "cyclic",
            "Cyclic",
            [
                {
                    "node_id": "a",
                    "node_type": "agent_task",
                    "depends_on": ["b"],
                    "config": {"objective": "A"},
                },
                {
                    "node_id": "b",
                    "node_type": "agent_task",
                    "depends_on": ["a"],
                    "config": {"objective": "B"},
                },
            ],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "unknown-dep",
            "Unknown dep",
            [
                {
                    "node_id": "a",
                    "node_type": "agent_task",
                    "depends_on": ["ghost"],
                    "config": {"objective": "A"},
                }
            ],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "bad-type",
            "Bad type",
            [{"node_id": "a", "node_type": "quantum", "config": {}}],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "no-objective",
            "No objective",
            [{"node_id": "a", "node_type": "agent_task", "config": {}}],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "duplicate",
            "Duplicate",
            [
                {"node_id": "a", "node_type": "parallel"},
                {"node_id": "a", "node_type": "join"},
            ],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "self-dep",
            "Self dep",
            [
                {
                    "node_id": "a",
                    "node_type": "agent_task",
                    "depends_on": ["a"],
                    "config": {"objective": "A"},
                }
            ],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "bad-tool",
            "Bad tool",
            [{"node_id": "a", "node_type": "tool_task", "config": {"tool": "not a tool!"}}],
        )
    with pytest.raises(WorkflowValidationError):
        workflows.register_definition(
            "orphan-only-if",
            "Orphan only if",
            [
                {"node_id": "a", "node_type": "parallel"},
                {
                    "node_id": "b",
                    "node_type": "agent_task",
                    "only_if": [{"node": "a", "output": "true"}],
                    "config": {"objective": "B"},
                },
            ],
        )


async def test_workflow_owner_isolation(tmp_path: Path) -> None:
    workflows, _, _, _ = build(tmp_path)
    register_pipeline(workflows)
    instance = workflows.start("report-pipeline", "alice", inputs={"topic": "x"})
    assert workflows.store.get_instance(instance.instance_id, owner_id="bob") is None
    assert workflows.pause(instance.instance_id, "bob") is None
    assert workflows.cancel(instance.instance_id, "bob") is None
    assert workflows.store.get_instance(instance.instance_id, owner_id="alice") is not None


async def test_workflow_notification_node(tmp_path: Path) -> None:
    delivered: list[str] = []

    class RecordingSink:
        async def deliver(self, notification: Any) -> None:
            delivered.append(notification.body)

    database = Database(tmp_path / "notify.sqlite3")
    executor = NodeExecutor()
    retry = RetryManager(default_policy=RetryPolicy(max_attempts=1))
    automation = AutomationEngine(database, executor, retry_manager=retry, poll_seconds=0.05)
    workflows = WorkflowEngine(database, automation, notifications=RecordingSink())
    workflows.register_definition(
        "notify-flow",
        "Notify flow",
        [
            {"node_id": "work", "node_type": "agent_task", "config": {"objective": "Work"}},
            {
                "node_id": "notify",
                "node_type": "notification",
                "depends_on": ["work"],
                "config": {"message": "Workflow finished with {{nodes.work.output}}"},
            },
        ],
    )
    instance = workflows.start("notify-flow", "owner")
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    await asyncio.sleep(0.05)
    assert any("done work" in body for body in delivered)


async def test_workflow_concurrency_capacity(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)
    workflows.max_concurrent_node_jobs = 2
    workflows.register_definition(
        "wide",
        "Wide",
        [
            {"node_id": "fan", "node_type": "parallel"},
            {
                "node_id": "a",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "A"},
            },
            {
                "node_id": "b",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "B"},
            },
            {
                "node_id": "c",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "C"},
            },
            {
                "node_id": "d",
                "node_type": "agent_task",
                "depends_on": ["fan"],
                "config": {"objective": "D"},
            },
        ],
    )
    instance = workflows.start("wide", "owner")
    workflows.tick()
    dispatched = [
        node
        for node in workflows.store.list_nodes(instance.instance_id)
        if node.status in (NodeStatus.RUNNING, NodeStatus.WAITING_SCHEDULE)
    ]
    assert len(dispatched) <= 2
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED


async def test_workflow_subworkflow(tmp_path: Path) -> None:
    workflows, automation, _, _ = build(tmp_path)

    class Launcher:
        def launch_subworkflow(
            self,
            workflow_id: str,
            owner_id: str,
            *,
            version: int | None,
            inputs: Any,
            parent_instance_id: str,
            node_id: str,
            goal_id: str | None,
        ) -> str:
            child = workflows.start(
                workflow_id,
                owner_id,
                inputs=inputs,
                parent_instance_id=parent_instance_id,
                goal_id=goal_id,
            )
            return child.instance_id

    workflows.subworkflow_launcher = Launcher()
    workflows.register_definition(
        "child-flow",
        "Child flow",
        [
            {
                "node_id": "child_work",
                "node_type": "agent_task",
                "config": {"objective": "Child work"},
            }
        ],
    )
    workflows.register_definition(
        "parent-flow",
        "Parent flow",
        [
            {"node_id": "prep", "node_type": "agent_task", "config": {"objective": "Prep"}},
            {
                "node_id": "sub",
                "node_type": "subworkflow",
                "depends_on": ["prep"],
                "config": {
                    "workflow_id": "child-flow",
                    "inputs": {"topic": "{{nodes.prep.output}}"},
                },
            },
            {
                "node_id": "post",
                "node_type": "agent_task",
                "depends_on": ["sub"],
                "config": {"objective": "Post"},
            },
        ],
    )
    instance = workflows.start("parent-flow", "owner")
    workflows.tick()
    await automation.run_worker()
    workflows.tick()
    parent = workflows.store.get_instance(instance.instance_id)
    assert parent is not None
    assert parent.child_instance_id is not None
    sub_node = workflows.store.get_node(instance.instance_id, "sub")
    assert sub_node is not None
    assert sub_node.status == NodeStatus.RUNNING
    await drain(workflows, automation)
    final = workflows.store.get_instance(instance.instance_id)
    assert final is not None
    assert final.status == WorkflowStatus.COMPLETED
    child = workflows.store.get_instance(parent.child_instance_id)
    assert child is not None
    assert child.status == WorkflowStatus.COMPLETED
    post_node = workflows.store.get_node(instance.instance_id, "post")
    assert post_node is not None
    assert post_node.status == NodeStatus.SUCCEEDED
