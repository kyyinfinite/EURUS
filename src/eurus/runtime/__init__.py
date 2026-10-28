from importlib import import_module
from typing import Any

__all__ = [
    "AgentEngine",
    "AgentRuntime",
    "BudgetSpec",
    "BudgetTracker",
    "ConcurrentRunUpdate",
    "Database",
    "EventStore",
    "InvalidRunTransition",
    "LoopDecision",
    "RecoveryAction",
    "Run",
    "RunOutcome",
    "RunResult",
    "RunStatus",
    "RunStore",
    "RuntimeEvent",
    "ToolExecutionOutcome",
    "ToolExecutionStatus",
    "VerificationOutcome",
    "VerificationStatus",
    "classify_recovery",
    "decision_for_stop_reason",
    "is_terminal_decision",
    "transition_run",
]

_MODULES = {
    "AgentEngine": "eurus.runtime.engine",
    "AgentRuntime": "eurus.runtime.application",
    "RunResult": "eurus.runtime.engine",
    "BudgetSpec": "eurus.runtime.budgets",
    "BudgetTracker": "eurus.runtime.budgets",
    "Run": "eurus.runtime.models",
    "RunStatus": "eurus.runtime.models",
    "RuntimeEvent": "eurus.runtime.models",
    "RecoveryAction": "eurus.runtime.recovery",
    "classify_recovery": "eurus.runtime.recovery",
    "InvalidRunTransition": "eurus.runtime.state_machine",
    "transition_run": "eurus.runtime.state_machine",
    "ConcurrentRunUpdate": "eurus.storage.runs",
    "RunStore": "eurus.storage.runs",
    "Database": "eurus.storage.database",
    "EventStore": "eurus.storage.events",
    "LoopDecision": "eurus.runtime.contracts",
    "RunOutcome": "eurus.runtime.contracts",
    "ToolExecutionOutcome": "eurus.runtime.contracts",
    "ToolExecutionStatus": "eurus.runtime.contracts",
    "VerificationOutcome": "eurus.runtime.contracts",
    "VerificationStatus": "eurus.runtime.contracts",
    "decision_for_stop_reason": "eurus.runtime.contracts",
    "is_terminal_decision": "eurus.runtime.contracts",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
