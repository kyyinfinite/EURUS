from typing import Any

__all__ = [
    "AgentOrchestrator",
    "SpecialistRegistry",
    "Specialist",
    "Assignment",
    "AssignmentMailbox",
    "MessageStore",
]


def __getattr__(name: str) -> Any:
    if name == "AgentOrchestrator":
        from eurus.multiagent.orchestrator import AgentOrchestrator

        return AgentOrchestrator
    if name == "SpecialistRegistry":
        from eurus.multiagent.registry import SpecialistRegistry

        return SpecialistRegistry
    if name == "Specialist":
        from eurus.multiagent.registry import Specialist

        return Specialist
    if name == "Assignment":
        from eurus.multiagent.orchestrator import Assignment

        return Assignment
    if name == "AssignmentMailbox":
        from eurus.multiagent.communication import AssignmentMailbox

        return AssignmentMailbox
    if name == "MessageStore":
        from eurus.multiagent.communication import MessageStore

        return MessageStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
