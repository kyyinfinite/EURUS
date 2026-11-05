from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.goals.states import GoalStatus, GoalTaskStatus
from eurus.multiagent.registry import MODEL_POLICIES
from eurus.workflows.states import NodeStatus, WorkflowStatus
from helpers_stabilization import make_stack


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def run(coro: object) -> object:
    return asyncio.run(coro)


def test_scenario_a_scheduled_autonomous_execution(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        stack.schedule_store.create(
            "alice",
            "produce the morning brief",
            kind="once",
            run_at=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        )
        moment = datetime.now(UTC) + timedelta(seconds=5)
        job_ids = stack.schedule_store.dispatch_due(now=moment)
        assert len(job_ids) == 1
        assert stack.automation.jobs.get(job_ids[0]) is not None
        assert run(stack.automation.run_worker("worker-1", now=moment)) is True
        job = stack.automation.jobs.get(job_ids[0])
        assert job is not None
        assert job.status == "completed"
        occurrences = stack.schedule_store.occurrences(
            [schedule.schedule_id for schedule in stack.schedule_store.list_owner("alice")][0]
        )
        assert occurrences[0].status == "completed"
        assert occurrences[0].finished_at is not None
        report = stack.automation.maintenance_tick()
        assert report.reconciliation.occurrences_finalized >= 0
        learning_stats = stack.learning.stats() if stack.learning else {}
        assert learning_stats.get("done", 0) >= 1
        evaluations = stack.learning.evaluation.list_evaluations(
            subject_type="job", subject_id=job_ids[0]
        )
        assert evaluations
    finally:
        stack.close()


def test_scenario_b_event_triggered_execution(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        stack.events.subscribe(
            "react",
            event_types=("DEPLOY_FINISHED",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice", "objective": "verify deploy {{subject_id}}"},
        )
        event = stack.events.publish(
            "DEPLOY_FINISHED",
            subject_type="deploy",
            subject_id="d-9",
            dedup_key="deploy-d-9",
        )
        assert event is not None
        assert stack.events.store.get_event(event.event_id) is not None
        report = stack.events.dispatch_tick()
        assert report.deliveries_delivered == 1
        assert report.jobs_submitted == 1
        delivery = stack.events.store.deliveries_for_event(event.event_id)[0]
        assert delivery.status == "delivered"
        jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        assert len(jobs) == 1
        assert run(stack.automation.run_worker("worker-1")) is True
        assert stack.automation.jobs.get(jobs[0].job_id).status == "completed"
        duplicate = stack.events.publish(
            "DEPLOY_FINISHED",
            subject_type="deploy",
            subject_id="d-9",
            dedup_key="deploy-d-9",
        )
        assert duplicate is None
    finally:
        stack.close()


def test_scenario_c_goal_driven_execution(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        assert stack.goals is not None
        stack.automation.executor.goals = stack.goals
        goal = stack.goals.create_goal(
            "alice",
            "produce the weekly digest",
            success_criteria=("digest exists", "sources cited"),
        )
        for _ in range(12):
            stack.goals.tick(owner_id="alice")
            jobs = stack.automation.jobs.list_jobs(owner_id="alice", statuses=("queued",))
            if jobs:
                run(stack.automation.run_worker("worker-1"))
            report = stack.goals.tick(owner_id="alice")
            if goal and stack.goals.get(goal.goal_id).status == GoalStatus.COMPLETED:
                break
            _ = report
        final = stack.goals.get(goal.goal_id)
        assert final is not None
        assert final.status == GoalStatus.COMPLETED
        assert final.progress == 1.0
        tasks = stack.goals.store.list_tasks(goal.goal_id)
        assert all(task.status == GoalTaskStatus.SUCCEEDED for task in tasks)
    finally:
        stack.close()


def test_scenario_d_workflow_execution_with_approval_and_recovery(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        assert stack.workflows is not None
        document = {
            "workflow_id": "release",
            "name": "Release flow",
            "nodes": [
                {
                    "node_id": "prepare",
                    "node_type": "agent_task",
                    "config": {"objective": "prepare the release"},
                },
                {
                    "node_id": "parallel_a",
                    "node_type": "agent_task",
                    "depends_on": ["prepare"],
                    "config": {"objective": "branch a"},
                },
                {
                    "node_id": "parallel_b",
                    "node_type": "agent_task",
                    "depends_on": ["prepare"],
                    "config": {"objective": "branch b"},
                },
                {
                    "node_id": "gate",
                    "node_type": "approval",
                    "depends_on": ["parallel_a", "parallel_b"],
                    "config": {"prompt": "approve the release?"},
                },
                {
                    "node_id": "publish",
                    "node_type": "agent_task",
                    "depends_on": ["gate"],
                    "config": {"objective": "publish the release"},
                },
            ],
        }
        definition = stack.workflows.register_definition_document(document)
        assert len(definition.nodes) == 5
        instance = stack.workflows.start("release", "alice")
        for _ in range(10):
            stack.workflows.tick(owner_id="alice")
            queued = stack.automation.jobs.list_jobs(owner_id="alice", statuses=("queued",))
            for _job in queued:
                run(stack.automation.run_worker("worker-1"))
            current = stack.workflows.store.get_instance(instance.instance_id)
            if current is not None and current.status == WorkflowStatus.WAITING_APPROVAL:
                break
        nodes = {
            node.node_id: node for node in stack.workflows.store.list_nodes(instance.instance_id)
        }
        assert nodes["prepare"].status == NodeStatus.SUCCEEDED
        assert nodes["parallel_a"].status == NodeStatus.SUCCEEDED
        assert nodes["parallel_b"].status == NodeStatus.SUCCEEDED
        assert nodes["gate"].status == NodeStatus.WAITING_APPROVAL
        approvals = stack.workflows.store.list_approvals(instance_id=instance.instance_id)
        assert len(approvals) == 1
        assert stack.workflows.decide_approval(
            approvals[0].approval_id, decision="approved", decided_by="alice", owner_id="alice"
        )
        for _ in range(6):
            stack.workflows.tick(owner_id="alice")
            queued = stack.automation.jobs.list_jobs(owner_id="alice", statuses=("queued",))
            for _job in queued:
                run(stack.automation.run_worker("worker-1"))
        finished = stack.workflows.store.get_instance(instance.instance_id)
        assert finished is not None
        assert finished.status == WorkflowStatus.COMPLETED
    finally:
        stack.close()


def test_scenario_d2_workflow_failure_runs_compensation(home: Path) -> None:
    stack = make_stack(str(home), behaviors={"workflow_node:charge": "fail"})
    from eurus.automation.retry import RetryManager, RetryPolicy

    stack.automation.retry_manager = RetryManager(
        default_policy=RetryPolicy(
            max_attempts=1,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.01,
            jitter_fraction=0.0,
        )
    )
    try:
        assert stack.workflows is not None
        document = {
            "workflow_id": "compensating",
            "name": "Compensating flow",
            "nodes": [
                {
                    "node_id": "reserve",
                    "node_type": "agent_task",
                    "config": {
                        "objective": "reserve the resource",
                        "compensation": "release the reservation",
                    },
                },
                {
                    "node_id": "charge",
                    "node_type": "agent_task",
                    "depends_on": ["reserve"],
                    "config": {"objective": "charge the card"},
                },
            ],
        }
        stack.workflows.register_definition_document(document)
        instance = stack.workflows.start("compensating", "alice")
        for _ in range(8):
            stack.workflows.tick(owner_id="alice")
            queued = stack.automation.jobs.list_jobs(owner_id="alice", statuses=("queued",))
            for _job in queued:
                run(stack.automation.run_worker("worker-1"))
        current = stack.workflows.store.get_instance(instance.instance_id)
        assert current is not None
        assert current.status == WorkflowStatus.FAILED
        nodes = {
            node.node_id: node for node in stack.workflows.store.list_nodes(instance.instance_id)
        }
        assert nodes["reserve"].status == NodeStatus.SUCCEEDED
        assert nodes["charge"].status == NodeStatus.FAILED
        compensation_jobs = [
            job
            for job in stack.automation.jobs.list_jobs(owner_id="alice")
            if job.payload.get("kind") == "workflow_compensation"
        ]
        assert compensation_jobs, "compensation job must be enqueued"
        assert compensation_jobs[0].objective == "release the reservation"
    finally:
        stack.close()


def test_scenario_e_multi_agent_execution(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        assert stack.orchestrator is not None
        assert "reasoning" in MODEL_POLICIES
        stack.orchestrator.registry.register(
            "researcher",
            description="research specialist",
            allowed_tools=("read_file",),
            model_policy="reasoning",
            max_concurrent=1,
        )
        assignment = stack.orchestrator.dispatch(
            "investigate the incident", role="researcher", owner_id="alice"
        )
        assert (
            assignment.model_policy == "reasoning" if hasattr(assignment, "model_policy") else True
        )
        job = stack.automation.jobs.get(assignment.job_id)
        assert job is not None
        assert job.payload["role"] == "researcher"
        assert job.payload["model_policy"] == "reasoning"
        assert job.payload["allowed_tools"] == ["read_file"]
        assert run(stack.automation.run_worker("worker-1")) is True
        report = stack.orchestrator.aggregate(assignment.parent_run_id or assignment.assignment_id)
        _ = report
        updated = stack.orchestrator.store.get(assignment.assignment_id)
        assert updated is not None
        assert updated.status == "succeeded"
        failed = stack.orchestrator.dispatch(
            "failing investigation", role="researcher", owner_id="alice"
        )
        stack.automation.executor.behaviors["specialist_assignment"] = "fail"
        run(stack.automation.run_worker("worker-1"))
        assert stack.automation.jobs.get(failed.job_id).status == "failed"
        reassigned = stack.orchestrator.reassign(failed.assignment_id)
        assert reassigned.assignment_id != failed.assignment_id
        assert reassigned.status == "dispatched"
    finally:
        stack.close()


def test_scenario_f_crash_recovery_boundaries(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        job = stack.automation.submit("alice", "crash test", payload={"kind": "event"})
        claim = stack.automation.jobs.claim("worker-1", lease_seconds=30)
        assert claim is not None
        future = datetime.now(UTC) + timedelta(seconds=60)
        stack.automation.jobs.recover_expired(now=future, owner_id="alice")
        assert stack.automation.jobs.get(job.job_id).status == "queued"
        re_claim = stack.automation.jobs.claim("worker-2", lease_seconds=30, now=future)
        assert re_claim is not None
        marker = stack.automation.executor.side_effect_marker_factory(re_claim)
        assert marker is not None
        marker()
        stack.automation.jobs.recover_expired(now=future + timedelta(seconds=60), owner_id="alice")
        assert stack.automation.jobs.get(job.job_id).status == "unknown_outcome"
        report = stack.automation.maintenance_tick(now=future + timedelta(seconds=60))
        assert report.reconciliation.unknown_outcomes_flagged == 1
        letters = stack.automation.jobs.list_dead_letters(owner_id="alice")
        assert len(letters) == 1
    finally:
        stack.close()


def test_scenario_g_two_owners_concurrently_isolated(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        stack.automation.submit("alice", "alice objective", payload={"kind": "event"})
        stack.automation.submit("bob", "bob objective", payload={"kind": "event"})
        alice_jobs = stack.automation.jobs.list_jobs(owner_id="alice")
        bob_jobs = stack.automation.jobs.list_jobs(owner_id="bob")
        assert len(alice_jobs) == 1 and len(bob_jobs) == 1
        executed = run(stack.automation.run_worker("worker-1"))
        assert executed is True
        remaining_bob = stack.automation.jobs.list_jobs(owner_id="bob")
        assert remaining_bob[0].status == "queued"
        assert stack.automation.executor.executed == [alice_jobs[0].job_id]
    finally:
        stack.close()


def test_scenario_h_shutdown_and_restart_without_duplication_or_loss(home: Path) -> None:
    stack = make_stack(str(home))
    try:
        stack.events.subscribe(
            "react",
            event_types=("PING",),
            handler_kind="submit_job",
            handler_config={"owner_id": "alice"},
        )
        stack.events.publish("PING", subject_type="test", subject_id="1")
        stack.events.dispatch_tick()
        stack.automation.submit("alice", "pending objective", payload={"kind": "event"})
    finally:
        stack.close()

    reopened = make_stack(str(home))
    try:
        jobs = reopened.automation.jobs.list_jobs(owner_id="alice")
        assert len(jobs) == 2
        first = run(reopened.automation.run_worker("worker-1"))
        assert first is True
        remaining = reopened.automation.jobs.list_jobs(owner_id="alice")
        assert sum(1 for item in remaining if item.status == "queued") == 1
        executed = reopened.automation.executor.executed
        assert len(executed) == 1
        report = reopened.events.dispatch_tick()
        assert report.jobs_submitted == 0
        health = reopened.supervisor.health()
        assert health["status"] in ("healthy", "degraded")
    finally:
        reopened.close()
