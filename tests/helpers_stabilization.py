from __future__ import annotations

from typing import Any

from eurus.automation.executor import AgentJobExecutor, ExecutionContext
from eurus.cli.autonomy import AutonomyStack, build_stack
from eurus.config import Config
from eurus.storage.database import Database
from eurus.storage.jobs import Job


class ScriptedExecutor(AgentJobExecutor):
    def __init__(
        self,
        config: Config,
        *,
        database: Database | None = None,
        trajectory_store: Any = None,
        side_effect_marker_factory: Any = None,
    ) -> None:
        super().__init__(
            config,
            database=database,
            trajectory_store=trajectory_store,
            side_effect_marker_factory=side_effect_marker_factory,
        )
        self.behaviors: dict[str, str] = {}
        self.executed: list[str] = []
        self.marked: list[str] = []
        self.goals: Any = None

    async def execute(self, job: Job, context: ExecutionContext) -> str:
        kind = str(job.payload.get("kind", job.objective))
        node_id = str(job.payload.get("node_id", ""))
        key = f"{kind}:{node_id}" if node_id else kind
        self.executed.append(job.job_id)
        behavior = self.behaviors.get(key, self.behaviors.get(kind, "ok"))
        if behavior == "fail":
            raise TimeoutError(f"{kind} failed")
        if behavior == "mutate":
            marker = (
                self.side_effect_marker_factory(job)
                if self.side_effect_marker_factory is not None
                else None
            )
            if marker is not None:
                marker()
                self.marked.append(job.job_id)
        if kind == "goal_plan" and self.goals is not None:
            goal_id = str(job.payload.get("goal_id"))
            self.goals.capture_plan(
                goal_id,
                {
                    "tasks": [
                        {"title": "gather sources", "success_criteria": "sources listed"},
                        {
                            "title": "compose digest",
                            "detail": "write the digest",
                            "depends_on": ["gather sources"],
                            "success_criteria": "digest written",
                        },
                    ]
                },
            )
            return "plan submitted"
        return f"done {kind}"


def make_stack(
    monkeypatch_home: str,
    *,
    owner_id: str = "alice",
    behaviors: dict[str, str] | None = None,
    poll_seconds: float = 0.05,
) -> AutonomyStack:
    from eurus.automation import engine as engine_module

    _ = engine_module.AgentJobExecutor
    import eurus.automation.executor as executor_module

    config = Config()
    scripted = ScriptedExecutor(config)
    if behaviors:
        scripted.behaviors = behaviors
    original = executor_module.AgentJobExecutor

    def factory(*args: Any, **kwargs: Any) -> ScriptedExecutor:
        scripted.database = kwargs.get("database")
        scripted.trajectory_store = kwargs.get("trajectory_store")
        scripted.side_effect_marker_factory = kwargs.get("side_effect_marker_factory")
        return scripted

    executor_module.AgentJobExecutor = factory
    try:
        stack = build_stack(config, workers=1, poll_seconds=poll_seconds, owner_id=owner_id)
    finally:
        executor_module.AgentJobExecutor = original
    assert stack.automation.executor is scripted
    return stack
