from typing import Any

__all__ = [
    "WorkflowEngine",
    "WorkflowStore",
    "WorkflowDefinition",
    "WorkflowInstance",
    "WorkflowStatus",
    "NodeStatus",
    "WorkflowValidationError",
]


def __getattr__(name: str) -> Any:
    if name == "WorkflowEngine":
        from eurus.workflows.engine import WorkflowEngine

        return WorkflowEngine
    if name == "WorkflowStore":
        from eurus.workflows.store import WorkflowStore

        return WorkflowStore
    if name == "WorkflowDefinition":
        from eurus.workflows.definition import WorkflowDefinition

        return WorkflowDefinition
    if name == "WorkflowInstance":
        from eurus.workflows.store import WorkflowInstance

        return WorkflowInstance
    if name in ("WorkflowStatus", "NodeStatus"):
        from eurus.workflows import states

        return getattr(states, name)
    if name == "WorkflowValidationError":
        from eurus.workflows.definition import WorkflowValidationError

        return WorkflowValidationError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
