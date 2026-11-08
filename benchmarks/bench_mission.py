from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eurus.agent.review_loop import ReviewLoop
from eurus.agent.supervisor import MainSupervisor
from eurus.agent.task_decomposer import TaskDecomposer
from eurus.automation.engine import AutomationEngine
from eurus.automation.retry import RetryManager, RetryPolicy
from eurus.multiagent.artifacts import ArtifactStore
from eurus.multiagent.contracts import ReviewFinding, TaskSpec
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import SpecialistRegistry
from eurus.multiagent.task_graph import TaskGraphStore
from eurus.storage.database import Database

OWNER = "bench-owner"


class _SilentExecutor:
    async def execute(self, job, context) -> str:
        return "ok"


def _stack(database: Database) -> tuple[AutomationEngine, AgentOrchestrator]:
    automation = AutomationEngine(
        database,
        _SilentExecutor(),
        retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
        poll_seconds=300.0,
    )
    return automation, AgentOrchestrator(database, automation)


def _layered_plan(layers: int, width: int) -> list[dict]:
    drafts: list[dict] = []
    for layer in range(layers):
        for slot in range(width):
            key = f"l{layer}_s{slot}"
            draft = {"task_key": key, "objective": f"layer {layer} slot {slot} work"}
            if layer > 0:
                draft["depends_on"] = [f"l{layer - 1}_s{slot}" for slot in range(width)]
            drafts.append(draft)
    return drafts


def bench_plan_dag(repeats: int) -> dict[str, float]:
    results: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite3")
        store = TaskGraphStore(database)
        missions = _mission_store(database)
        mission = missions.create_mission("benchmark mission", owner_id=OWNER)
        decomposer = TaskDecomposer(known_roles=("coder",))
        drafts = _layered_plan(layers=4, width=4)
        for _ in range(repeats):
            started = time.perf_counter()
            plan = decomposer.decompose("benchmark", mission_id=mission.mission_id, tasks=drafts)
            for spec in plan.tasks:
                with contextlib.suppress(Exception):
                    store.add_task(spec, owner_id=OWNER)
            results.append((time.perf_counter() - started) * 1000)
    return {"plan_dag_16tasks_ms": statistics.median(results)}


def bench_promote_ready(repeats: int) -> dict[str, float]:
    results: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite3")
        store = TaskGraphStore(database)
        missions = _mission_store(database)
        mission = missions.create_mission("scheduling benchmark", owner_id=OWNER)
        plan = TaskDecomposer().decompose(
            "benchmark", mission_id=mission.mission_id, tasks=_layered_plan(5, 5)
        )
        for spec in plan.tasks:
            store.add_task(spec, owner_id=OWNER)
        store.update_status(plan.tasks[0].task_id, "succeeded", owner_id=OWNER)
        for _ in range(repeats):
            started = time.perf_counter()
            store.promote_ready(mission.mission_id, owner_id=OWNER)
            results.append((time.perf_counter() - started) * 1000)
    return {"promote_ready_25tasks_ms": statistics.median(results)}


def bench_dispatch_ready(repeats: int) -> dict[str, float]:
    results: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite3")
        automation, orchestrator = _stack(database)
        registry = SpecialistRegistry(database)
        registry.register("coder", allowed_tools=["read_file"], max_concurrent=32)
        supervisor = MainSupervisor(database, orchestrator)
        mission = supervisor.create_mission("dispatch benchmark", owner_id=OWNER)
        supervisor.plan_mission(
            mission.mission_id,
            owner_id=OWNER,
            tasks=[
                {
                    "task_key": f"task-{index}",
                    "objective": f"work {index}",
                    "preferred_role": "coder",
                }
                for index in range(10)
            ],
        )
        supervisor.start_mission(mission.mission_id, owner_id=OWNER)
        for _ in range(repeats):
            for assignment in orchestrator.store.list_assignments(
                owner_id=OWNER, mission_id=mission.mission_id, limit=100
            ):
                orchestrator.store.update(
                    assignment.assignment_id, status="cancelled", owner_id=OWNER
                )
            for task in supervisor.tasks.list_tasks(mission.mission_id, owner_id=OWNER):
                store = supervisor.tasks
                store.update_status(task.task_id, "ready", owner_id=OWNER, clear_assignment=True)
            started = time.perf_counter()
            supervisor.dispatch_ready(mission.mission_id, owner_id=OWNER, limit=10)
            results.append((time.perf_counter() - started) * 1000)
    return {"dispatch_ready_10tasks_ms": statistics.median(results)}


def bench_artifact_roundtrip(repeats: int) -> dict[str, float]:
    results: list[float] = []
    payload = "x" * 8192
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite3")
        store = ArtifactStore(database, Path(tmp) / "artifacts")
        for _ in range(repeats):
            started = time.perf_counter()
            record = store.publish(
                payload,
                owner_id=OWNER,
                mission_id="m-bench",
                artifact_type="report",
            )
            store.read(record.artifact_id, owner_id=OWNER)
            store.verify_integrity(record.artifact_id, owner_id=OWNER)
            results.append((time.perf_counter() - started) * 1000)
    return {"artifact_roundtrip_8kb_ms": statistics.median(results)}


def bench_review_cycle(repeats: int) -> dict[str, float]:
    results: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite3")
        missions = _mission_store(database)
        tasks = TaskGraphStore(database)
        mission = missions.create_mission("review benchmark", owner_id=OWNER)
        loop = ReviewLoop(database, max_cycles=3)
        for repeat in range(repeats):
            spec = TaskSpec(
                task_id=uuid.uuid4().hex,
                mission_id=mission.mission_id,
                task_key=f"bench-{repeat}",
                objective="bench work",
            )
            tasks.add_task(spec, owner_id=OWNER)
            tasks.update_status(spec.task_id, "succeeded", owner_id=OWNER)
            started = time.perf_counter()
            loop.submit_review(
                owner_id=OWNER,
                mission_id=mission.mission_id,
                task_id=spec.task_id,
                reviewer_assignment_id="rev-bench",
                status="changes_requested",
                findings=[
                    ReviewFinding(
                        finding_id="f-bench",
                        severity="major",
                        category="correctness",
                        evidence="bench finding",
                    )
                ],
            )
            results.append((time.perf_counter() - started) * 1000)
    return {"review_cycle_with_revision_ms": statistics.median(results)}


def _mission_store(database: Database) -> MainSupervisor:
    automation = AutomationEngine(
        database,
        _SilentExecutor(),
        retry_manager=RetryManager(default_policy=RetryPolicy(max_attempts=1)),
        poll_seconds=300.0,
    )
    return MainSupervisor(database, AgentOrchestrator(database, automation))


SCENARIOS = {
    "plan_dag": bench_plan_dag,
    "promote_ready": bench_promote_ready,
    "dispatch_ready": bench_dispatch_ready,
    "artifact_roundtrip": bench_artifact_roundtrip,
    "review_cycle": bench_review_cycle,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="Write results to this path")
    parser.add_argument(
        "--only",
        nargs="*",
        choices=sorted(SCENARIOS),
        default=None,
        help="Run a subset of scenarios",
    )
    args = parser.parse_args()

    selected = SCENARIOS
    if args.only:
        selected = {name: SCENARIOS[name] for name in args.only}

    results: dict[str, float] = {}
    for scenario in selected.values():
        results.update(scenario(args.repeats))

    rendered = json.dumps(results, indent=2, sort_keys=True)
    print(rendered)
    if args.json is not None:
        args.json.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
