from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
import tracemalloc
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eurus.agent.events import AgentEvent
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.tasks import DurableTask, TaskStore
from eurus.models import Message
from eurus.runtime.application import AgentRuntime
from eurus.runtime.event_bridge import AgentEventBridge
from eurus.sessions import SessionStore


def timed(operation: Callable[[int], object], count: int) -> dict[str, float]:
    started = time.perf_counter()
    for index in range(count):
        operation(index)
    elapsed = time.perf_counter() - started
    return {"ops": count, "seconds": round(elapsed, 4), "per_second": round(count / elapsed, 1)}


def bench_store(directory: Path, count: int) -> dict[str, Any]:
    store = TaskStore(directory / "store.sqlite3")
    tasks: list[DurableTask] = []

    def create(index: int) -> None:
        tasks.append(store.create("1", "s", f"objective {index}"))

    results: dict[str, Any] = {"create": timed(create, count)}
    results["claim"] = timed(lambda index: store.claim(tasks[index].task_id), count)
    results["transition"] = timed(
        lambda index: store.transition(tasks[index].task_id, "verifying"), count
    )
    results["complete"] = timed(
        lambda index: store.complete(tasks[index].task_id, {"result": "ok"}), count
    )
    results["claim_update"] = timed(lambda index: store.claim_update("bench", str(index)), count)
    results["count_by_status"] = timed(lambda index: store.count_by_status(), min(count, 200))
    return results


def bench_store_scale(directory: Path, rows: int) -> dict[str, Any]:
    import sqlite3
    import uuid

    store = TaskStore(directory / "scale.sqlite3")
    now = "2026-01-01T00:00:00+00:00"
    statuses = ("completed", "completed", "completed", "failed", "cancelled")
    with sqlite3.connect(store.path) as db:
        db.executemany(
            "INSERT INTO gateway_tasks (task_id, owner_id, session_id, objective, status, "
            "state_json, created_at, updated_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    uuid.uuid4().hex,
                    str(index % 50),
                    "s",
                    "objective",
                    statuses[index % len(statuses)],
                    "{}",
                    f"2026-01-01T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                    now,
                    "",
                )
                for index in range(rows)
            ),
        )
        db.executemany(
            "INSERT INTO gateway_updates(platform, update_id, received_at) VALUES (?, ?, ?)",
            (("telegram", str(index), now) for index in range(rows)),
        )
    live = store.create("7", "s", "live")
    store.transition(live.task_id, "running")
    results: dict[str, Any] = {"rows": rows}
    results["list_owner"] = timed(lambda index: store.list_owner("7", 10), 50)
    results["count_by_status"] = timed(lambda index: store.count_by_status(), 50)
    results["list_by_status_failed"] = timed(lambda index: store.list_by_status("failed", 10), 50)
    started = time.perf_counter()
    store.interrupt_orphaned()
    results["interrupt_orphaned_seconds"] = round(time.perf_counter() - started, 4)
    results["claim_update_new"] = timed(lambda index: store.claim_update("fresh", str(index)), 100)
    return results


async def bench_bridge(subscribers: int, events: int) -> dict[str, Any]:
    bridge = AgentEventBridge(default_capacity=64, max_subscribers=subscribers + 1)
    for _ in range(subscribers):
        bridge.subscribe("session")
    event = AgentEvent.text_delta("x")
    started = time.perf_counter()
    for _ in range(events):
        await bridge.publish("session", event)
    elapsed = time.perf_counter() - started
    return {
        "subscribers": subscribers,
        "events": events,
        "seconds": round(elapsed, 4),
        "events_per_second": round(events / elapsed, 1),
        "dropped": bridge.dropped_events,
    }


async def bench_manager(
    directory: Path, concurrency: int, tasks: int, hold: float
) -> dict[str, Any]:
    store = TaskStore(directory / f"manager-{concurrency}.sqlite3")

    async def runner(task: DurableTask) -> str:
        if hold:
            await asyncio.sleep(hold)
        return "ok"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    manager = BackgroundTaskManager(store, runner, notify, max_concurrent=concurrency)
    started = time.perf_counter()
    submitted = 0
    rejected = 0
    while submitted < tasks:
        try:
            await manager.submit("1", f"s{submitted}", "work", notify_chat_id="1")
            submitted += 1
        except RuntimeError:
            rejected += 1
            await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await asyncio.gather(*manager.active.values(), return_exceptions=True)
    elapsed = time.perf_counter() - started
    leaked = len(manager.active) + len(manager.cancelled)
    await manager.shutdown()
    return {
        "concurrency": concurrency,
        "tasks": tasks,
        "hold_seconds": hold,
        "seconds": round(elapsed, 4),
        "tasks_per_second": round(tasks / elapsed, 1),
        "capacity_rejections": rejected,
        "leaked_entries": leaked,
    }


class StubAgent:
    def __init__(self, workspace: Path) -> None:
        self.history = [Message.system("system")]
        self.context = SimpleNamespace(workspace=workspace)

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta("ok")
        self.history.append(Message.assistant("ok"))


async def bench_runtime(directory: Path, runs: int, sessions: int) -> dict[str, Any]:
    runtime = AgentRuntime(
        lambda session_id: StubAgent(directory),
        SessionStore(directory / "runtime.sqlite3"),
        max_sessions=32,
    )

    async def one(index: int) -> None:
        async for _ in runtime.run(f"session-{index % sessions}", "hello"):
            pass

    started = time.perf_counter()
    for index in range(runs):
        await one(index)
    elapsed = time.perf_counter() - started
    health = runtime.health()
    await runtime.close()
    return {
        "runs": runs,
        "sessions": sessions,
        "seconds": round(elapsed, 4),
        "runs_per_second": round(runs / elapsed, 1),
        "cached_sessions_after": health["cached_sessions"],
        "session_locks_after": health["session_locks"],
        "active_runs_after": health["active_runs"],
    }


async def memory_profile(directory: Path, tasks: int) -> dict[str, Any]:
    store = TaskStore(directory / "memory.sqlite3")

    async def runner(task: DurableTask) -> str:
        return "ok"

    async def notify(conversation_id: str, text: str) -> None:
        return None

    manager = BackgroundTaskManager(store, runner, notify, max_concurrent=4)
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    done = 0
    while done < tasks:
        try:
            await manager.submit("1", "s", "work", notify_chat_id="1")
            done += 1
        except RuntimeError:
            await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await asyncio.gather(*manager.active.values(), return_exceptions=True)
    await asyncio.sleep(0)
    current, peak = tracemalloc.get_traced_memory()
    differences = tracemalloc.take_snapshot().compare_to(baseline, "lineno")
    delta = sum(item.size_diff for item in differences)
    tracemalloc.stop()
    leftover_tasks = sum(
        1 for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()
    )
    await manager.shutdown()
    return {
        "tasks": tasks,
        "traced_current_kib": round(current / 1024, 1),
        "traced_peak_kib": round(peak / 1024, 1),
        "retained_kib_after": round(delta / 1024, 1),
        "retained_bytes_per_task": round(delta / tasks, 1),
        "asyncio_tasks_left": leftover_tasks,
    }


async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        report: dict[str, Any] = {"python": sys.version.split()[0]}
        report["store"] = bench_store(directory, args.store_ops)
        report["scale"] = bench_store_scale(directory, args.scale_rows)
        report["bridge"] = [await bench_bridge(count, args.events) for count in (1, 10, 100)]
        report["manager"] = [
            await bench_manager(directory, concurrency, args.tasks, hold)
            for concurrency in (1, 2, 8)
            for hold in (0.0, 0.01)
        ]
        report["runtime"] = await bench_runtime(directory, args.runs, args.sessions)
        report["memory"] = await memory_profile(directory, args.tasks)
        return report


def render(report: dict[str, Any]) -> str:
    lines = [f"python {report['python']}"]
    for name, item in report["store"].items():
        lines.append(f"store.{name}: {item['per_second']} ops/s ({item['ops']} ops)")
    scale = report["scale"]
    lines.append(
        f"scale rows={scale['rows']}: list_owner={scale['list_owner']['per_second']}/s "
        f"count_by_status={scale['count_by_status']['per_second']}/s "
        f"list_by_status={scale['list_by_status_failed']['per_second']}/s "
        f"interrupt_orphaned={scale['interrupt_orphaned_seconds']}s "
        f"claim_update={scale['claim_update_new']['per_second']}/s"
    )
    for item in report["bridge"]:
        lines.append(
            f"bridge subscribers={item['subscribers']}: {item['events_per_second']} events/s "
            f"dropped={item['dropped']}"
        )
    for item in report["manager"]:
        lines.append(
            f"manager concurrency={item['concurrency']} hold={item['hold_seconds']}: "
            f"{item['tasks_per_second']} tasks/s rejections={item['capacity_rejections']} "
            f"leaked={item['leaked_entries']}"
        )
    runtime = report["runtime"]
    lines.append(
        f"runtime: {runtime['runs_per_second']} runs/s cached={runtime['cached_sessions_after']} "
        f"locks={runtime['session_locks_after']} active={runtime['active_runs_after']}"
    )
    memory = report["memory"]
    lines.append(
        f"memory: peak={memory['traced_peak_kib']} KiB retained={memory['retained_kib_after']} KiB "
        f"({memory['retained_bytes_per_task']} B/task) tasks_left={memory['asyncio_tasks_left']}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Eurus gateway benchmarks")
    parser.add_argument("--store-ops", type=int, default=300)
    parser.add_argument("--events", type=int, default=20000)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--runs", type=int, default=300)
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--scale-rows", type=int, default=100000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run_all(args))
    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
