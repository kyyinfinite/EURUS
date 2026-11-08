from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_WORKFLOW_NODES = 64
MAX_NODE_DEPENDENCIES = 16
NODE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")

CONDITION_OPERATORS = frozenset(
    {"equals", "not_equals", "contains", "gt", "lt", "gte", "lte", "exists"}
)
ACTION_NODE_TYPES = frozenset({"agent_task", "tool_task", "specialist_task", "subworkflow"})
STRUCTURAL_NODE_TYPES = frozenset({"parallel", "join"})
INSTANT_NODE_TYPES = frozenset({"condition", "validation", "transform", "notification"})
DEFERRED_NODE_TYPES = frozenset({"delay", "schedule"})
SUPPORTED_NODE_TYPES = frozenset(
    ACTION_NODE_TYPES
    | STRUCTURAL_NODE_TYPES
    | INSTANT_NODE_TYPES
    | DEFERRED_NODE_TYPES
    | {"approval"}
)


class WorkflowValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NodeTypeSpec:
    node_type: str
    required_config: tuple[str, ...]
    optional_config: tuple[str, ...] = ()
    produces_output: bool = True


NODE_TYPE_SPECS: Mapping[str, NodeTypeSpec] = {
    spec.node_type: spec
    for spec in (
        NodeTypeSpec(
            "agent_task", ("objective",), ("allowed_tools", "max_steps", "success_criteria")
        ),
        NodeTypeSpec("tool_task", ("tool",), ("arguments", "success_criteria")),
        NodeTypeSpec("specialist_task", ("role", "objective"), ("max_steps", "success_criteria")),
        NodeTypeSpec("condition", ("path", "op"), ("value",)),
        NodeTypeSpec("parallel", ()),
        NodeTypeSpec("join", ()),
        NodeTypeSpec("delay", ("seconds",)),
        NodeTypeSpec("schedule", ("run_at", "objective"), ("allowed_tools", "max_steps")),
        NodeTypeSpec("approval", ("prompt",), ("timeout_seconds",)),
        NodeTypeSpec("notification", ("message",)),
        NodeTypeSpec("validation", ("assertions",), ("mode",)),
        NodeTypeSpec("transform", ("set",)),
        NodeTypeSpec("subworkflow", ("workflow_id",), ("inputs", "required_inputs")),
    )
}


@dataclass(slots=True)
class NodeSpec:
    node_id: str
    node_type: str
    depends_on: tuple[str, ...] = ()
    only_if: tuple[Mapping[str, str], ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not NODE_ID_PATTERN.match(self.node_id):
            raise WorkflowValidationError(f"invalid node id: {self.node_id!r}")
        if self.node_type not in SUPPORTED_NODE_TYPES:
            raise WorkflowValidationError(
                f"unsupported node type {self.node_type!r} on node {self.node_id}"
            )
        if len(self.depends_on) > MAX_NODE_DEPENDENCIES:
            raise WorkflowValidationError(
                f"node {self.node_id} exceeds {MAX_NODE_DEPENDENCIES} dependencies"
            )
        for dependency in self.depends_on:
            if not isinstance(dependency, str) or not dependency.strip():
                raise WorkflowValidationError(f"node {self.node_id} has an invalid dependency")
        for condition in self.only_if:
            if not isinstance(condition, Mapping) or set(condition) != {"node", "output"}:
                raise WorkflowValidationError(f"node {self.node_id} has an invalid only_if entry")
        spec = NODE_TYPE_SPECS[self.node_type]
        config = self.config
        if not isinstance(config, Mapping):
            raise WorkflowValidationError(f"node {self.node_id} config must be a mapping")
        for key in spec.required_config:
            if key not in config or config[key] in (None, "", (), {}):
                raise WorkflowValidationError(f"node {self.node_id} requires config key {key!r}")
        self._validate_config_values()

    def _validate_config_values(self) -> None:
        config = self.config
        if self.node_type == "tool_task":
            tool = str(config["tool"])
            if not re.match(r"^[a-z0-9_]{1,64}$", tool):
                raise WorkflowValidationError(
                    f"node {self.node_id} has an invalid tool name {tool!r}"
                )
        if self.node_type in {"agent_task", "schedule", "specialist_task"}:
            objective = str(config.get("objective", ""))
            if not objective.strip():
                raise WorkflowValidationError(f"node {self.node_id} objective is empty")
            if len(objective) > 8000:
                raise WorkflowValidationError(f"node {self.node_id} objective is too long")
        if self.node_type == "specialist_task":
            role = str(config["role"])
            if not re.match(r"^[a-z0-9][a-z0-9_.\-]{0,63}$", role):
                raise WorkflowValidationError(f"node {self.node_id} has an invalid role {role!r}")
        if self.node_type == "condition":
            if config["op"] not in CONDITION_OPERATORS:
                raise WorkflowValidationError(
                    f"node {self.node_id} has an unknown condition operator"
                )
            if config["op"] != "exists" and "value" not in config:
                raise WorkflowValidationError(f"node {self.node_id} condition requires a value")
        if self.node_type == "delay":
            seconds = config["seconds"]
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                raise WorkflowValidationError(f"node {self.node_id} delay seconds must be a number")
            if not 0 < float(seconds) <= 2_592_000:
                raise WorkflowValidationError(f"node {self.node_id} delay seconds out of range")
        if self.node_type == "schedule":
            run_at = config["run_at"]
            if not isinstance(run_at, str) or not re.match(
                r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", run_at
            ):
                raise WorkflowValidationError(
                    f"node {self.node_id} run_at must be an ISO-8601 datetime"
                )
        if self.node_type == "approval":
            timeout = config.get("timeout_seconds", 0)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0:
                raise WorkflowValidationError(
                    f"node {self.node_id} timeout_seconds must be a non-negative integer"
                )
        if self.node_type == "validation":
            assertions = config["assertions"]
            if not isinstance(assertions, list) or not assertions:
                raise WorkflowValidationError(
                    f"node {self.node_id} requires a non-empty assertions list"
                )
            for assertion in assertions:
                if not isinstance(assertion, Mapping) or "path" not in assertion:
                    raise WorkflowValidationError(
                        f"node {self.node_id} has an assertion without a path"
                    )
                if assertion.get("op", "equals") not in CONDITION_OPERATORS:
                    raise WorkflowValidationError(
                        f"node {self.node_id} has an assertion with an unknown operator"
                    )
            mode = config.get("mode", "all")
            if mode not in {"all", "any"}:
                raise WorkflowValidationError(
                    f"node {self.node_id} validation mode must be all or any"
                )
        if self.node_type == "transform":
            assignments = config["set"]
            if not isinstance(assignments, Mapping) or not assignments:
                raise WorkflowValidationError(
                    f"node {self.node_id} requires a non-empty set mapping"
                )
            for key in assignments:
                if not re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9_.\-]{0,63}$", str(key)):
                    raise WorkflowValidationError(
                        f"node {self.node_id} has an invalid transform key {key!r}"
                    )
        if self.node_type == "subworkflow":
            workflow_id = str(config["workflow_id"])
            if not re.match(r"^[a-z0-9][a-z0-9_.\-]{0,127}$", workflow_id):
                raise WorkflowValidationError(f"node {self.node_id} has an invalid workflow_id")
        allowed_tools = config.get("allowed_tools")
        if allowed_tools is not None and (
            not isinstance(allowed_tools, list)
            or not all(
                isinstance(item, str) and re.match(r"^[a-z0-9_]{1,64}$", item)
                for item in allowed_tools
            )
        ):
            raise WorkflowValidationError(
                f"node {self.node_id} allowed_tools must be a list of tool names"
            )


@dataclass(slots=True)
class WorkflowDefinition:
    workflow_id: str
    name: str
    nodes: list[NodeSpec]
    owner_id: str = "local"
    version: int = 1
    required_inputs: tuple[str, ...] = ()
    status: str = "active"

    def node(self, node_id: str) -> NodeSpec | None:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "version": self.version,
            "owner_id": self.owner_id,
            "required_inputs": list(self.required_inputs),
            "status": self.status,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "depends_on": list(node.depends_on),
                    "only_if": [dict(entry) for entry in node.only_if],
                    "config": dict(node.config),
                }
                for node in self.nodes
            ],
        }

    @classmethod
    def from_json(cls, document: Mapping[str, Any]) -> WorkflowDefinition:
        nodes = [
            NodeSpec(
                node_id=str(raw["node_id"]),
                node_type=str(raw["node_type"]),
                depends_on=tuple(str(dep) for dep in raw.get("depends_on", ())),
                only_if=tuple(dict(entry) for entry in raw.get("only_if", ())),
                config=dict(raw.get("config", {})),
            )
            for raw in document.get("nodes", [])
        ]
        return cls(
            workflow_id=str(document["workflow_id"]),
            name=str(document["name"]),
            nodes=nodes,
            owner_id=str(document.get("owner_id", "local")),
            version=int(document.get("version", 1)),
            required_inputs=tuple(str(item) for item in document.get("required_inputs", ())),
            status=str(document.get("status", "active")),
        )

    def validate(self) -> None:
        if not re.match(r"^[a-z0-9][a-z0-9_.\-]{0,127}$", self.workflow_id):
            raise WorkflowValidationError(f"invalid workflow id: {self.workflow_id!r}")
        if not self.name.strip() or len(self.name) > 200:
            raise WorkflowValidationError("workflow name must be 1-200 characters")
        if not self.nodes:
            raise WorkflowValidationError("workflow requires at least one node")
        if len(self.nodes) > MAX_WORKFLOW_NODES:
            raise WorkflowValidationError(f"workflow exceeds {MAX_WORKFLOW_NODES} nodes")
        identifiers: set[str] = set()
        for node in self.nodes:
            if node.node_id in identifiers:
                raise WorkflowValidationError(f"duplicate node id: {node.node_id}")
            identifiers.add(node.node_id)
            node.validate()
        for node in self.nodes:
            for dependency in node.depends_on:
                if dependency not in identifiers:
                    raise WorkflowValidationError(
                        f"node {node.node_id} depends on unknown node {dependency!r}"
                    )
                if dependency == node.node_id:
                    raise WorkflowValidationError(f"node {node.node_id} depends on itself")
            for condition in node.only_if:
                if condition["node"] not in identifiers:
                    raise WorkflowValidationError(
                        f"node {node.node_id} only_if references unknown node"
                    )
        self._reject_cycles()
        self._reject_orphan_only_if()

    def _reject_cycles(self) -> None:
        graph: dict[str, tuple[str, ...]] = {node.node_id: node.depends_on for node in self.nodes}
        state: dict[str, int] = {node_id: 0 for node_id in graph}
        for start in graph:
            if state[start] != 0:
                continue
            stack = [(start, iter(graph[start]))]
            state[start] = 1
            while stack:
                node_id, iterator = stack[-1]
                advanced = False
                for dependency in iterator:
                    if state.get(dependency, 0) == 1:
                        raise WorkflowValidationError("workflow graph contains a cycle")
                    if state.get(dependency, 0) == 0:
                        state[dependency] = 1
                        stack.append((dependency, iter(graph[dependency])))
                        advanced = True
                        break
                if not advanced:
                    state[node_id] = 2
                    stack.pop()

    def _reject_orphan_only_if(self) -> None:
        for node in self.nodes:
            for condition in node.only_if:
                if condition["node"] not in node.depends_on:
                    raise WorkflowValidationError(
                        f"node {node.node_id} only_if must reference a dependency"
                    )


def definition_from_document(document: Mapping[str, Any]) -> WorkflowDefinition:
    definition = WorkflowDefinition.from_json(document)
    definition.validate()
    return definition


def resolve_template(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        matches = TEMPLATE_PATTERN.findall(value)
        if len(matches) == 1 and TEMPLATE_PATTERN.sub("", value).strip() == "":
            return lookup_path(context, matches[0])
        return TEMPLATE_PATTERN.sub(
            lambda match: _stringify(lookup_path(context, match.group(1))), value
        )
    if isinstance(value, Mapping):
        return {key: resolve_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_template(item, context) for item in value]
    return value


def lookup_path(context: Mapping[str, Any], path: str) -> Any:
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
            continue
        raise WorkflowValidationError(f"context path {path!r} cannot be resolved")
    return current


def evaluate_condition(*, path: str, op: str, value: Any, context: Mapping[str, Any]) -> bool:
    if op == "exists":
        try:
            lookup_path(context, path)
        except WorkflowValidationError:
            return False
        return True
    actual = lookup_path(context, path)
    if op == "equals":
        return bool(_coerce(actual) == _coerce(value))
    if op == "not_equals":
        return bool(_coerce(actual) != _coerce(value))
    if op == "contains":
        return str(value) in str(actual)
    if op in {"gt", "lt", "gte", "lte"}:
        left = _numeric(actual)
        right = _numeric(value)
        if op == "gt":
            return left > right
        if op == "lt":
            return left < right
        if op == "gte":
            return left >= right
        return left <= right
    raise WorkflowValidationError(f"unknown operator {op!r}")


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        try:
            return float(value) if "." in value or "e" in value.lower() else int(value)
        except ValueError:
            return value
    return value


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError as exc:
        raise WorkflowValidationError(f"value {value!r} is not numeric") from exc


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
