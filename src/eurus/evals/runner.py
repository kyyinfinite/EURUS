from __future__ import annotations

import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from eurus.agent.factory import build_agent
from eurus.coding.checkpoint import CheckpointStore
from eurus.coding.validation import run_validation
from eurus.config import Config
from eurus.errors import EurusError
from eurus.evals.tasks import EvalTask
from eurus.execution.host import scrubbed_env
from eurus.models import ToolCall
from eurus.providers.router import Router
from eurus.runtime.engine import AgentEngine
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool
from eurus.tools.builtin import default_registry

CHECK_TIMEOUT = 120.0


@dataclass(frozen=True, slots=True)
class EvalResult:
    task: str
    passed: bool
    steps: int
    tokens: int
    seconds: float
    stop_reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


async def _allow(call: ToolCall, tool: Tool) -> bool:
    return True


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def run_task(
    task: EvalTask, config: Config, router: Router, *, lessons: bool = False
) -> EvalResult:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"eurus-eval-{task.name}-") as tmp:
        root = Path(tmp)
        write_files(root, task.files)
        registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
        for name in (
            "recall_memory",
            "session_search",
            "remember_memory",
            "replace_memory",
            "forget_memory",
        ):
            registry.unregister(name)
        agent = build_agent(
            config,
            router,
            registry=registry,
            approver=_allow,
            workspace=root,
            stream=False,
            shell=False,
            lessons=lessons,
        )
        database = root / "eval-runs.sqlite3"
        engine = AgentEngine(
            agent,
            run_store=RunStore(database),
            event_store=EventStore(database),
            attempt_store=ToolAttemptStore(database),
            checkpoint_store=CheckpointStore(home=root),
        )
        steps = 0
        stop_reason = "error"
        detail = ""
        try:
            result = await engine.run(
                task.prompt,
                owner_id="eval",
            )
            for event in result.events:
                if event.kind == "step":
                    steps = event.step
            stop_reason = result.run.stop_reason or stop_reason
        except EurusError as exc:
            detail = str(exc)
        validation = run_validation(
            root, task.check, timeout_seconds=CHECK_TIMEOUT, env=scrubbed_env()
        )
        if not validation.passed and not detail:
            detail = (validation.stdout + validation.stderr).strip()[-300:]
        return EvalResult(
            task.name,
            validation.passed,
            steps,
            agent.usage.total_tokens,
            time.monotonic() - started,
            stop_reason,
            detail,
        )
