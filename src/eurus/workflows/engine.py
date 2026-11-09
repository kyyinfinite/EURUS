from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from eurus.automation.engine import AutomationEngine
from eurus.automation.notifications import Notification, NotificationSink
from eurus.storage.database import Database, DatabasePath
from eurus.storage.jobs import Job
from eurus.workflows.definition import (
    WorkflowDefinition,
    WorkflowValidationError,
    evaluate_condition,
    resolve_template,
)
from eurus.workflows.states import (
    TERMINAL_NODE_STATUSES,
    NodeStatus,
    WorkflowStatus,
)
from eurus.workflows.store import NodeState, WorkflowInstance, WorkflowStore

log = logging.getLogger(__name__)

MAX_NODE_ATTEMPTS = 3
MAX_COMPENSATION_NODES = 16


class WorkflowEventPublisher(Protocol):
    def __call__(
        self,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, Any],
    ) -> None: ...


class SpecialistLookup(Protocol):
    def specialist_for_role(self, role: str) -> Any | None: ...


class SubworkflowLauncher(Protocol):
    def launch_subworkflow(
        self,
        workflow_id: str,
        owner_id: str,
        *,
        version: int | None,
        inputs: Mapping[str, Any],
        parent_instance_id: str,
        node_id: str,
        goal_id: str | None,
    ) -> str: ...


@dataclass(slots=True)
class WorkflowTickReport:
    instances_advanced: int = 0
    jobs_submitted: int = 0
    instances_completed: int = 0
    instances_failed: int = 0
    notes: list[str] = field(default_factory=list)


class WorkflowEngine:
    def __init__(
        self,
        database: Database | DatabasePath,
        automation: AutomationEngine,
        *,
        event_publisher: WorkflowEventPublisher | None = None,
        specialist_lookup: SpecialistLookup | None = None,
        notifications: NotificationSink | None = None,
        subworkflow_launcher: SubworkflowLauncher | None = None,
        max_node_attempts: int = MAX_NODE_ATTEMPTS,
        max_concurrent_node_jobs: int = 8,
    ) -> None:
        if not 1 <= max_node_attempts <= 10:
            raise ValueError("max_node_attempts must be between 1 and 10")
        if not 1 <= max_concurrent_node_jobs <= 64:
            raise ValueError("max_concurrent_node_jobs must be between 1 and 64")
        self.store = WorkflowStore(database)
        self.automation = automation
        self.event_publisher = event_publisher
        self.specialist_lookup = specialist_lookup
        self.notifications = notifications
        self.subworkflow_launcher = subworkflow_launcher
        self.max_node_attempts = max_node_attempts
        self.max_concurrent_node_jobs = max_concurrent_node_jobs
        automation.on_job_finished.append(self._on_job_finished)

    def register_definition(
        self,
        workflow_id: str,
        name: str,
        nodes: Sequence[Mapping[str, Any]],
        *,
        owner_id: str = "local",
        required_inputs: Sequence[str] = (),
        workflow_id_explicit: str | None = None,
    ) -> WorkflowDefinition:
        identifier = workflow_id_explicit or workflow_id
        version = self.store.latest_version(identifier) + 1
        definition = WorkflowDefinition(
            workflow_id=identifier,
            name=name,
            nodes=[_node_from_document(node) for node in nodes],
            owner_id=owner_id,
            version=version,
            required_inputs=tuple(required_inputs),
        )
        definition.validate()
        return self.store.save_definition(definition)

    def register_definition_document(self, document: Mapping[str, Any]) -> WorkflowDefinition:
        definition = WorkflowDefinition.from_json(document)
        definition.validate()
        existing = self.store.get_definition(definition.workflow_id)
        if existing is not None and existing.to_json()["nodes"] == definition.to_json()["nodes"]:
            return existing
        definition.version = self.store.latest_version(definition.workflow_id) + 1
        return self.store.save_definition(definition)

    def get_definition(
        self, workflow_id: str, *, version: int | None = None, owner_id: str | None = None
    ) -> WorkflowDefinition | None:
        return self.store.get_definition(workflow_id, version=version, owner_id=owner_id)

    def list_definitions(
        self, *, owner_id: str | None = None, limit: int = 100
    ) -> list[WorkflowDefinition]:
        return self.store.list_definitions(owner_id=owner_id, limit=limit)

    def start(
        self,
        workflow_id: str,
        owner_id: str,
        *,
        version: int | None = None,
        inputs: Mapping[str, Any] | None = None,
        goal_id: str | None = None,
        parent_instance_id: str | None = None,
        instance_id: str | None = None,
    ) -> WorkflowInstance:
        definition = self.store.get_definition(workflow_id, version=version)
        if definition is None:
            raise KeyError(f"workflow definition {workflow_id!r} not found")
        if definition.status != "active":
            raise WorkflowValidationError(f"workflow definition {workflow_id!r} is not active")
        provided = dict(inputs or {})
        missing = [name for name in definition.required_inputs if name not in provided]
        if missing:
            raise WorkflowValidationError(f"missing required inputs: {', '.join(missing)}")
        instance = self.store.create_instance(
            definition,
            owner_id,
            inputs=provided,
            goal_id=goal_id,
            parent_instance_id=parent_instance_id,
            instance_id=instance_id,
        )
        self._publish(
            "WORKFLOW_STARTED",
            "workflow_instance",
            instance.instance_id,
            {
                "workflow_id": workflow_id,
                "version": definition.version,
            },
        )
        return instance

    def tick(self, *, owner_id: str | None = None, limit: int = 20) -> WorkflowTickReport:
        report = WorkflowTickReport()
        active = self.store.list_instances(
            owner_id=owner_id,
            statuses=(
                WorkflowStatus.PENDING,
                WorkflowStatus.RUNNING,
                WorkflowStatus.WAITING_APPROVAL,
                WorkflowStatus.WAITING_SCHEDULE,
            ),
            limit=limit,
        )
        for instance in active:
            partial = self.advance_instance(instance.instance_id, owner_id=owner_id)
            report.instances_advanced += partial.instances_advanced
            report.jobs_submitted += partial.jobs_submitted
            report.instances_completed += partial.instances_completed
            report.instances_failed += partial.instances_failed
            report.notes.extend(partial.notes)
        return report

    def advance_instance(
        self, instance_id: str, *, owner_id: str | None = None
    ) -> WorkflowTickReport:
        report = WorkflowTickReport()
        instance = self.store.get_instance(instance_id, owner_id=owner_id)
        if instance is None or instance.status in (WorkflowStatus.PAUSED,):
            return report
        definition = self.store.get_definition(instance.workflow_id, version=instance.version)
        if definition is None:
            failed = self.store.transition_instance(
                instance_id,
                WorkflowStatus.FAILED,
                error=f"definition {instance.workflow_id} unavailable",
            )
            self._publish(
                "WORKFLOW_FAILED",
                "workflow_instance",
                instance_id,
                {
                    "reason": "definition_unavailable",
                },
            )
            report.instances_failed += 1
            report.notes.append(f"instance {instance_id} failed: definition missing")
            _ = failed
            return report
        if instance.status == WorkflowStatus.PENDING:
            instance = self.store.transition_instance(instance_id, WorkflowStatus.RUNNING)
        nodes = self.store.list_nodes(instance_id)
        by_id = {node.node_id: node for node in nodes}
        self._enforce_approval_timeouts(instance, definition, by_id, report)
        nodes = self.store.list_nodes(instance_id)
        by_id = {node.node_id: node for node in nodes}
        self._refresh_waiting_nodes(instance, definition, by_id, report)
        nodes = self.store.list_nodes(instance_id)
        by_id = {node.node_id: node for node in nodes}
        running = sum(
            1
            for node in nodes
            if node.status
            in (NodeStatus.RUNNING, NodeStatus.WAITING_APPROVAL, NodeStatus.WAITING_SCHEDULE)
        )
        capacity = max(0, self.max_concurrent_node_jobs - running)
        for node in nodes:
            if capacity <= 0:
                break
            if node.status != NodeStatus.PENDING:
                continue
            decision = self._dependency_decision(node, definition, by_id)
            if decision == "blocked":
                continue
            if decision == "skip":
                self.store.update_node(instance_id, node.node_id, status=NodeStatus.SKIPPED)
                self._sync_node_context(instance, node.node_id, NodeStatus.SKIPPED, "", None)
                report.instances_advanced += 1
                continue
            spec = definition.node(node.node_id)
            if spec is None:
                self.store.update_node(
                    instance_id,
                    node.node_id,
                    status=NodeStatus.FAILED,
                    error="node missing from definition",
                )
                continue
            if self._dispatch_node(instance, definition, spec, node, report):
                capacity -= 1
                instance = self.store.get_instance(instance_id) or instance
        self._finalize_if_done(instance_id, definition, report)
        self._refresh_instance_status(instance_id)
        return report

    def pause(self, instance_id: str, owner_id: str) -> WorkflowInstance | None:
        instance = self.store.get_instance(instance_id, owner_id=owner_id)
        if instance is None:
            return None
        if instance.status in (WorkflowStatus.PAUSED,):
            return instance
        if instance.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        ):
            return instance
        return self.store.transition_instance(instance_id, WorkflowStatus.PAUSED)

    def resume(self, instance_id: str, owner_id: str) -> WorkflowInstance | None:
        instance = self.store.get_instance(instance_id, owner_id=owner_id)
        if instance is None or instance.status != WorkflowStatus.PAUSED:
            return instance
        return self.store.transition_instance(instance_id, WorkflowStatus.RUNNING)

    def cancel(self, instance_id: str, owner_id: str) -> WorkflowInstance | None:
        instance = self.store.get_instance(instance_id, owner_id=owner_id)
        if instance is None:
            return None
        if instance.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        ):
            return instance
        for node in self.store.list_nodes(instance_id):
            if node.status in (NodeStatus.RUNNING, NodeStatus.WAITING_SCHEDULE) and node.job_id:
                self.automation.jobs.request_cancel(node.job_id, owner_id=owner_id)
            if node.status not in TERMINAL_NODE_STATUSES:
                self.store.update_node(instance_id, node.node_id, status=NodeStatus.CANCELLED)
        updated = self.store.transition_instance(
            instance_id, WorkflowStatus.CANCELLED, error="cancelled by owner"
        )
        self._run_compensation(instance_id, owner_id)
        self._publish("WORKFLOW_CANCELLED", "workflow_instance", instance_id, {})
        return updated

    def decide_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str = "",
        owner_id: str | None = None,
    ) -> bool:
        approval = self.store.decide_approval(
            approval_id, decision=decision, decided_by=decided_by, note=note, owner_id=owner_id
        )
        if approval is None:
            return False
        node = self.store.get_node(approval.instance_id, approval.node_id)
        if node is None or node.status != NodeStatus.WAITING_APPROVAL:
            return True
        if decision == "approved":
            self.store.update_node(
                approval.instance_id,
                approval.node_id,
                status=NodeStatus.SUCCEEDED,
                output=f"approved by {decided_by}",
            )
            self._sync_node_context(
                self.store.get_instance(approval.instance_id),
                approval.node_id,
                NodeStatus.SUCCEEDED,
                f"approved by {decided_by}",
                None,
            )
            self._publish(
                "APPROVAL_GRANTED",
                "workflow_approval",
                approval_id,
                {
                    "instance_id": approval.instance_id,
                    "node_id": approval.node_id,
                },
            )
        else:
            self.store.update_node(
                approval.instance_id,
                approval.node_id,
                status=NodeStatus.FAILED,
                error=f"rejected by {decided_by}",
            )
            self._sync_node_context(
                self.store.get_instance(approval.instance_id),
                approval.node_id,
                NodeStatus.FAILED,
                "",
                f"rejected by {decided_by}",
            )
            self._publish(
                "APPROVAL_REJECTED",
                "workflow_approval",
                approval_id,
                {
                    "instance_id": approval.instance_id,
                    "node_id": approval.node_id,
                },
            )
        self.advance_instance(approval.instance_id, owner_id=owner_id)
        return True

    def recover(self, *, owner_id: str | None = None) -> int:
        recovered = 0
        instances = self.store.list_instances(
            owner_id=owner_id,
            statuses=(
                WorkflowStatus.RUNNING,
                WorkflowStatus.WAITING_APPROVAL,
                WorkflowStatus.WAITING_SCHEDULE,
            ),
            limit=200,
        )
        for instance in instances:
            changed = False
            for node in self.store.list_nodes(instance.instance_id):
                if node.status != NodeStatus.RUNNING or not node.job_id:
                    continue
                job = self.automation.jobs.get(node.job_id)
                if job is None or job.owner_id != instance.owner_id:
                    continue
                if job.status == "completed":
                    self._complete_node(instance.instance_id, node.node_id, job.result)
                    changed = True
                elif job.status in ("failed", "cancelled", "unknown_outcome"):
                    self._fail_node(
                        instance.instance_id,
                        node.node_id,
                        job.error or job.result or job.status,
                    )
                    changed = True
            if changed:
                recovered += 1
                self.advance_instance(instance.instance_id, owner_id=instance.owner_id)
        return recovered

    def _dispatch_node(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        spec: Any,
        node: NodeState,
        report: WorkflowTickReport,
    ) -> bool:
        node_type = spec.node_type
        context = instance.context
        if node_type in ("parallel", "join"):
            self.store.update_node(instance.instance_id, node.node_id, status=NodeStatus.SUCCEEDED)
            self._sync_node_context(instance, node.node_id, NodeStatus.SUCCEEDED, "", None)
            report.instances_advanced += 1
            return True
        if node_type == "condition":
            outcome = evaluate_condition(
                path=str(spec.config["path"]),
                op=str(spec.config["op"]),
                value=resolve_template(spec.config.get("value"), context),
                context=context,
            )
            self.store.update_node(
                instance.instance_id,
                node.node_id,
                status=NodeStatus.SUCCEEDED,
                output="true" if outcome else "false",
            )
            self._sync_node_context(
                instance,
                node.node_id,
                NodeStatus.SUCCEEDED,
                "true" if outcome else "false",
                None,
            )
            report.instances_advanced += 1
            return True
        if node_type == "validation":
            mode = str(spec.config.get("mode", "all"))
            results = [
                evaluate_condition(
                    path=str(assertion["path"]),
                    op=str(assertion.get("op", "equals")),
                    value=resolve_template(assertion.get("value"), context),
                    context=context,
                )
                for assertion in spec.config["assertions"]
            ]
            passed = all(results) if mode == "all" else any(results)
            if passed:
                self.store.update_node(
                    instance.instance_id,
                    node.node_id,
                    status=NodeStatus.SUCCEEDED,
                    output=f"{sum(results)}/{len(results)}",
                )
                self._sync_node_context(
                    instance,
                    node.node_id,
                    NodeStatus.SUCCEEDED,
                    f"{sum(results)}/{len(results)}",
                    None,
                )
                report.instances_advanced += 1
                return True
            self.store.update_node(
                instance.instance_id,
                node.node_id,
                status=NodeStatus.FAILED,
                error=f"validation failed: {results}",
            )
            self._sync_node_context(
                instance,
                node.node_id,
                NodeStatus.FAILED,
                "",
                f"validation failed: {results}",
            )
            return True
        if node_type == "transform":
            resolved = {
                key: resolve_template(value, context) for key, value in spec.config["set"].items()
            }
            vars_namespace = dict(context.get("vars", {}))
            vars_namespace.update(resolved)
            context = dict(context)
            context["vars"] = vars_namespace
            self.store.save_context(instance.instance_id, context)
            self.store.update_node(
                instance.instance_id,
                node.node_id,
                status=NodeStatus.SUCCEEDED,
                output="; ".join(f"{key}={_text(value)}" for key, value in resolved.items())[:4000],
            )
            self._sync_node_context(
                instance,
                node.node_id,
                NodeStatus.SUCCEEDED,
                "transform applied",
                None,
            )
            report.instances_advanced += 1
            return True
        if node_type == "notification":
            message = str(resolve_template(spec.config["message"], context))
            if self.notifications is not None:
                notification = Notification(
                    kind="workflow_notification",
                    title=f"workflow:{instance.workflow_id}/{node.node_id}",
                    body=message[:2000],
                    owner_id=instance.owner_id,
                    correlation_id=instance.instance_id,
                )
                self._deliver_notification(notification)
            self.store.update_node(
                instance.instance_id,
                node.node_id,
                status=NodeStatus.SUCCEEDED,
                output="notified",
            )
            self._sync_node_context(instance, node.node_id, NodeStatus.SUCCEEDED, "notified", None)
            report.instances_advanced += 1
            return True
        if node_type == "approval":
            self.store.update_node(
                instance.instance_id, node.node_id, status=NodeStatus.WAITING_APPROVAL
            )
            self.store.create_approval(
                instance.instance_id,
                node.node_id,
                prompt=str(resolve_template(spec.config["prompt"], context)),
            )
            report.instances_advanced += 1
            return True
        if node_type == "subworkflow":
            launcher = self.subworkflow_launcher
            if launcher is None:
                self.store.update_node(
                    instance.instance_id,
                    node.node_id,
                    status=NodeStatus.FAILED,
                    error="subworkflow launcher unavailable",
                )
                self._sync_node_context(
                    instance,
                    node.node_id,
                    NodeStatus.FAILED,
                    "",
                    "subworkflow launcher unavailable",
                )
                return True
            child_inputs = resolve_template(spec.config.get("inputs", {}), context)
            child_id = launcher.launch_subworkflow(
                str(spec.config["workflow_id"]),
                instance.owner_id,
                version=None,
                inputs=child_inputs if isinstance(child_inputs, Mapping) else {},
                parent_instance_id=instance.instance_id,
                node_id=node.node_id,
                goal_id=instance.goal_id,
            )
            self.store.update_node(
                instance.instance_id,
                node.node_id,
                status=NodeStatus.RUNNING,
                job_id=None,
            )
            self.store.set_child_instance(instance.instance_id, child_id)
            report.instances_advanced += 1
            return True
        return self._submit_node_job(instance, definition, spec, node, report)

    def _submit_node_job(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        spec: Any,
        node: NodeState,
        report: WorkflowTickReport,
    ) -> bool:
        node_type = spec.node_type
        context = instance.context
        config = spec.config
        payload: dict[str, Any] = {
            "kind": "workflow_node",
            "workflow_instance_id": instance.instance_id,
            "node_id": node.node_id,
            "node_type": node_type,
            "mode": "workflow",
        }
        objective: str
        available_at: datetime | None = None
        allowed_tools: list[str] | None = None
        max_attempts = self.max_node_attempts
        if node_type == "agent_task":
            objective = str(resolve_template(config["objective"], context))
            allowed_tools = config.get("allowed_tools")
            payload["allowed_tools"] = allowed_tools or []
            payload["max_steps"] = int(config.get("max_steps", 20))
            payload["success_criteria"] = str(config.get("success_criteria", ""))
        elif node_type == "specialist_task":
            objective = str(resolve_template(config["objective"], context))
            payload["role"] = str(config["role"])
            payload["max_steps"] = int(config.get("max_steps", 20))
            specialist = (
                self.specialist_lookup.specialist_for_role(str(config["role"]))
                if self.specialist_lookup is not None
                else None
            )
            if specialist is not None:
                payload["allowed_tools"] = list(specialist.allowed_tools)
                payload["max_steps"] = min(
                    int(config.get("max_steps", specialist.max_iterations)),
                    specialist.max_iterations,
                )
                payload["max_runtime_seconds"] = specialist.max_runtime_seconds
                payload["role"] = specialist.role
            max_attempts = 1
        elif node_type == "tool_task":
            tool = str(config["tool"])
            objective = (
                f"Run the {tool} tool and report its result. Call exactly one {tool} tool call."
            )
            payload["allowed_tools"] = [tool]
            payload["tool"] = tool
            payload["arguments"] = resolve_template(config.get("arguments", {}), context)
            payload["max_steps"] = 4
        elif node_type == "delay":
            seconds = float(config["seconds"])
            available_at = datetime.now(UTC) + timedelta(seconds=seconds)
            objective = (
                f"Resume workflow {instance.workflow_id} after a {int(seconds)} second delay."
            )
            payload["max_steps"] = 1
            max_attempts = 1
        elif node_type == "schedule":
            run_at_text = str(config["run_at"])
            parsed = datetime.fromisoformat(run_at_text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            available_at = parsed
            objective = str(resolve_template(config["objective"], context))
            payload["allowed_tools"] = config.get("allowed_tools", [])
            payload["max_steps"] = int(config.get("max_steps", 20))
        else:
            return False
        job_id = f"wf-{instance.instance_id[:16]}-{node.node_id[:24]}-{node.attempts + 1}"
        job = self._enqueue(
            instance.owner_id,
            objective,
            payload=payload,
            job_id=job_id,
            available_at=available_at,
            max_attempts=max_attempts,
            priority=(
                int(config.get("priority", 5)) if isinstance(config.get("priority"), int) else 5
            ),
        )
        if job is None:
            return False
        status = NodeStatus.WAITING_SCHEDULE if available_at is not None else NodeStatus.RUNNING
        self.store.update_node(
            instance.instance_id,
            node.node_id,
            status=status,
            job_id=job.job_id,
            increment_attempts=True,
        )
        report.jobs_submitted += 1
        report.instances_advanced += 1
        return True

    def _enqueue(
        self,
        owner_id: str,
        objective: str,
        *,
        payload: dict[str, Any],
        job_id: str,
        available_at: datetime | None,
        max_attempts: int,
        priority: int,
    ) -> Job | None:
        existing = self.automation.jobs.get(job_id)
        if existing is not None:
            if existing.status in ("queued", "running"):
                return existing
            return None
        try:
            return self.automation.submit(
                owner_id,
                objective,
                payload=payload,
                source="workflow",
                priority=priority,
                max_attempts=max_attempts,
                available_at=available_at,
                correlation_id=job_id,
                job_id=job_id,
            )
        except sqlite3.IntegrityError:
            existing = self.automation.jobs.get(job_id)
            if existing is not None and existing.status in ("queued", "running"):
                return existing
            return None

    def _dependency_decision(
        self, node: NodeState, definition: WorkflowDefinition, by_id: Mapping[str, NodeState]
    ) -> str:
        spec = definition.node(node.node_id)
        if spec is None:
            return "skip"
        for dependency in spec.depends_on:
            dependency_state = by_id.get(dependency)
            if dependency_state is None or dependency_state.status == NodeStatus.PENDING:
                return "blocked"
            if dependency_state.status == NodeStatus.RUNNING:
                return "blocked"
            if dependency_state.status in (
                NodeStatus.WAITING_APPROVAL,
                NodeStatus.WAITING_SCHEDULE,
            ):
                return "blocked"
            if dependency_state.status in (
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.SKIPPED,
            ):
                return "skip"
        for condition in spec.only_if:
            dependency_state = by_id.get(str(condition["node"]))
            if dependency_state is None:
                return "skip"
            if dependency_state.output != str(condition["output"]):
                return "skip"
        return "ready"

    def _enforce_approval_timeouts(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        by_id: Mapping[str, NodeState],
        report: WorkflowTickReport,
    ) -> None:
        specs = {spec.node_id: spec for spec in definition.nodes}
        for node in by_id.values():
            if node.status != NodeStatus.WAITING_APPROVAL:
                continue
            spec = specs.get(node.node_id)
            if spec is None or spec.node_type != "approval":
                continue
            raw_timeout = spec.config.get("timeout_seconds")
            try:
                timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
            except (TypeError, ValueError):
                timeout_seconds = None
            if timeout_seconds is None or timeout_seconds <= 0:
                continue
            approvals = self.store.list_approvals(
                instance_id=instance.instance_id, pending=True, limit=500
            )
            pending = [approval for approval in approvals if approval.node_id == node.node_id]
            if not pending:
                continue
            approval = pending[-1]
            age = (datetime.now(UTC) - approval.requested_at).total_seconds()
            if age <= timeout_seconds:
                continue
            timed_out = self.decide_approval(
                approval.approval_id,
                decision="rejected",
                decided_by="approval_timeout",
                note=(f"approval timed out after {int(age)}s (limit {int(timeout_seconds)}s)"),
                owner_id=instance.owner_id,
            )
            if timed_out:
                report.notes.append(
                    f"instance {instance.instance_id} node {node.node_id} "
                    f"approval timed out after {int(age)}s"
                )

    def _refresh_waiting_nodes(
        self,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        by_id: Mapping[str, NodeState],
        report: WorkflowTickReport,
    ) -> None:
        for node in list(by_id.values()):
            if node.status != NodeStatus.RUNNING:
                continue
            if node.node_type != "subworkflow":
                continue
            child_id = instance.child_instance_id
            if not child_id:
                continue
            child = self.store.get_instance(child_id)
            if child is None:
                self._fail_node(instance.instance_id, node.node_id, "subworkflow instance missing")
                continue
            if child.status == WorkflowStatus.COMPLETED:
                self._complete_node(
                    instance.instance_id,
                    node.node_id,
                    child.context.get("vars", {}).get("result", "subworkflow completed"),
                )
            elif child.status in (WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
                self._fail_node(
                    instance.instance_id,
                    node.node_id,
                    f"subworkflow {child.status.value}: {child.error or ''}",
                )

    def _refresh_instance_status(self, instance_id: str) -> None:
        instance = self.store.get_instance(instance_id)
        if instance is None or instance.status not in (
            WorkflowStatus.RUNNING,
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.WAITING_SCHEDULE,
        ):
            return
        nodes = self.store.list_nodes(instance_id)
        statuses = {node.status for node in nodes}
        if NodeStatus.WAITING_APPROVAL in statuses and NodeStatus.RUNNING not in statuses:
            if instance.status != WorkflowStatus.WAITING_APPROVAL:
                self.store.transition_instance(instance_id, WorkflowStatus.WAITING_APPROVAL)
        elif NodeStatus.RUNNING in statuses:
            if instance.status != WorkflowStatus.RUNNING:
                self.store.transition_instance(instance_id, WorkflowStatus.RUNNING)
        elif (
            NodeStatus.WAITING_SCHEDULE in statuses
            and instance.status != WorkflowStatus.WAITING_SCHEDULE
        ):
            self.store.transition_instance(instance_id, WorkflowStatus.WAITING_SCHEDULE)

    def _finalize_if_done(
        self, instance_id: str, definition: WorkflowDefinition, report: WorkflowTickReport
    ) -> None:
        instance = self.store.get_instance(instance_id)
        if instance is None:
            return
        if instance.status not in (
            WorkflowStatus.RUNNING,
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.WAITING_SCHEDULE,
        ):
            return
        nodes = self.store.list_nodes(instance_id)
        if not nodes:
            return
        if any(node.status not in TERMINAL_NODE_STATUSES for node in nodes):
            return
        if any(node.status in (NodeStatus.FAILED, NodeStatus.CANCELLED) for node in nodes):
            failed = self.store.transition_instance(
                instance_id,
                WorkflowStatus.FAILED,
                error="; ".join(
                    f"{node.node_id}: {node.error or node.status.value}"
                    for node in nodes
                    if node.status in (NodeStatus.FAILED, NodeStatus.CANCELLED)
                )[:4000],
            )
            self._run_compensation(instance_id, instance.owner_id)
            self._publish(
                "WORKFLOW_FAILED",
                "workflow_instance",
                instance_id,
                {
                    "workflow_id": instance.workflow_id,
                },
            )
            report.instances_failed += 1
            report.notes.append(f"instance {instance_id} failed")
            _ = failed
            return
        outputs = {
            node.node_id: {"status": node.status.value, "output": node.output} for node in nodes
        }
        context = dict(instance.context)
        context["nodes"] = outputs
        self.store.save_context(instance_id, context)
        self.store.transition_instance(instance_id, WorkflowStatus.COMPLETED)
        self._publish(
            "WORKFLOW_COMPLETED",
            "workflow_instance",
            instance_id,
            {
                "workflow_id": instance.workflow_id,
                "goal_id": instance.goal_id or "",
            },
        )
        report.instances_completed += 1
        report.notes.append(f"instance {instance_id} completed")

    def _run_compensation(self, instance_id: str, owner_id: str) -> None:
        instance = self.store.get_instance(instance_id)
        if instance is None:
            return
        definition = self.store.get_definition(instance.workflow_id, version=instance.version)
        if definition is None:
            return
        compensated = 0
        for node in self.store.list_nodes(instance_id):
            if compensated >= MAX_COMPENSATION_NODES:
                break
            if node.status != NodeStatus.SUCCEEDED:
                continue
            spec = definition.node(node.node_id)
            if spec is None:
                continue
            compensation = spec.config.get("compensation")
            if not isinstance(compensation, str) or not compensation.strip():
                continue
            self._enqueue(
                owner_id,
                str(resolve_template(compensation, instance.context)),
                payload={
                    "kind": "workflow_compensation",
                    "workflow_instance_id": instance_id,
                    "node_id": node.node_id,
                    "mode": "workflow",
                },
                job_id=f"wfc-{instance_id[:16]}-{node.node_id[:24]}",
                available_at=None,
                max_attempts=1,
                priority=7,
            )
            compensated += 1

    def _on_job_finished(self, job: Job, status: str, result: str, error: str | None) -> None:
        payload = job.payload or {}
        if payload.get("kind") not in ("workflow_node",):
            return
        instance_id = str(payload.get("workflow_instance_id", ""))
        node_id = str(payload.get("node_id", ""))
        if not instance_id or not node_id:
            return
        instance = self.store.get_instance(instance_id)
        if instance is None or instance.owner_id != job.owner_id:
            log.warning(
                "ignoring workflow job %s for instance %s: owner mismatch",
                job.job_id,
                instance_id,
            )
            return
        node = self.store.get_node(instance_id, node_id)
        if node is None or node.status in TERMINAL_NODE_STATUSES:
            return
        if status == "completed":
            self._complete_node(instance_id, node_id, result)
        elif status == "failed":
            self._fail_node(instance_id, node_id, error or result)
        elif status == "cancelled":
            self.store.update_node(instance_id, node_id, status=NodeStatus.CANCELLED)
        elif status == "unknown_outcome":
            self._fail_node(instance_id, node_id, f"unknown outcome: {error or result}")
        self.advance_instance(instance_id, owner_id=instance.owner_id)

    def _complete_node(self, instance_id: str, node_id: str, result: str) -> None:
        self.store.update_node(
            instance_id, node_id, status=NodeStatus.SUCCEEDED, output=result[:20000]
        )
        self._sync_node_context(
            self.store.get_instance(instance_id), node_id, NodeStatus.SUCCEEDED, result, None
        )

    def _fail_node(self, instance_id: str, node_id: str, error: str) -> None:
        node = self.store.get_node(instance_id, node_id)
        if node is None:
            return
        if node.status in TERMINAL_NODE_STATUSES:
            return
        self.store.update_node(instance_id, node_id, status=NodeStatus.FAILED, error=error[:4000])
        self._sync_node_context(
            self.store.get_instance(instance_id), node_id, NodeStatus.FAILED, "", error
        )

    def _sync_node_context(
        self,
        instance: WorkflowInstance | None,
        node_id: str,
        status: NodeStatus,
        output: str,
        error: str | None,
    ) -> None:
        instance_id = instance.instance_id if instance is not None else ""
        if not instance_id:
            return
        fresh = self.store.get_instance(instance_id)
        if fresh is None:
            return
        context = dict(fresh.context)
        nodes_namespace = dict(context.get("nodes", {}))
        nodes_namespace[node_id] = {
            "status": status.value,
            "output": output[:20000],
            "error": error or "",
        }
        context["nodes"] = nodes_namespace
        self.store.save_context(instance_id, context)

    def _deliver_notification(self, notification: Notification) -> None:
        import asyncio

        try:
            delivered = self.notifications.deliver(notification) if self.notifications else None
        except Exception:
            log.exception("workflow notification delivery failed")
            return
        if asyncio.iscoroutine(delivered):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                delivered.close()
                return
            loop.create_task(delivered)

    def _publish(
        self, event_type: str, subject_type: str, subject_id: str, payload: dict[str, Any]
    ) -> None:
        if self.event_publisher is None:
            return
        try:
            self.event_publisher(event_type, subject_type, subject_id, payload)
        except Exception:
            log.exception("workflow event publication failed")


def _node_from_document(node: Mapping[str, Any]) -> Any:
    from eurus.workflows.definition import NodeSpec

    return NodeSpec(
        node_id=str(node["node_id"]),
        node_type=str(node["node_type"]),
        depends_on=tuple(str(dep) for dep in node.get("depends_on", ())),
        only_if=tuple(dict(entry) for entry in node.get("only_if", ())),
        config=dict(node.get("config", {})),
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
