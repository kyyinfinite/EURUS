from typing import Any

__all__ = ["GoalEngine", "GoalStore", "GoalStatus"]


def __getattr__(name: str) -> Any:
    if name == "GoalEngine":
        from eurus.goals.engine import GoalEngine

        return GoalEngine
    if name == "GoalStore":
        from eurus.goals.store import GoalStore

        return GoalStore
    if name == "GoalStatus":
        from eurus.goals.states import GoalStatus

        return GoalStatus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
