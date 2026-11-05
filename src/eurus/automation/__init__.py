from typing import Any

__all__ = [
    "AutomationEngine",
    "AutomationWorkerPool",
]


def __getattr__(name: str) -> Any:
    if name == "AutomationWorkerPool":
        from eurus.automation.worker import AutomationWorkerPool

        return AutomationWorkerPool
    if name == "AutomationEngine":
        from eurus.automation.engine import AutomationEngine

        return AutomationEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
