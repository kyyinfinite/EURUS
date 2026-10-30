"""Offline benchmark: non-jaringan overhead measurement with a fake provider.

Runs against the real agent/engine/storage code with scripted provider
streams (providers/testing.py), so it measures CPU/SQLite work only.

Usage:
    python benchmarks/offline_bench.py [--repeats 5] [--json PATH]

Scenarios:
    cold_build     build_agent() from scratch on a synthetic workspace
    tui_fresh      TUI baseline: new agent every turn (history N)
    tui_reuse      TUI with per-session reuse (history N)
    engine_turn    AgentEngine run with durable stores (history N)
    prompt_build   agent._prompt() with fitting on a 500-message history
    checkpoint     run_store.save_checkpoint() with a 200-message snapshot
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eurus.agent.context import ContextWindow
from eurus.agent.loop import Agent
from eurus.models import Message
from eurus.providers.testing import openai_message, scripted_router
from eurus.storage.runs import RunStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry

try:
    from eurus.agent.factory import build_agent
    from eurus.config import Config
    from eurus.runtime.engine import AgentEngine

    HAVE_ENGINE = True
except ImportError:  # pragma: no cover
    HAVE_ENGINE = False

HISTORY_SIZES = (0, 50, 200, 500)
REPEAT_TURNS = 5


def _registry() -> ToolRegistry:
    async def handler(arguments, context):
        return "ok"

    return ToolRegistry(
        [
            Tool(
                "noop",
                "no-op tool",
                {"type": "object", "properties": {"n": {"type": "number"}}},
                handler,
            )
        ]
    )


def _history(size: int, offset: int = 0) -> list[Message]:
    messages: list[Message] = []
    for index in range(size):
        if (index + offset) % 2 == 0:
            messages.append(Message.user(f"historical message {index + offset} " + "x" * 120))
        else:
            messages.append(Message.assistant(f"answer {index + offset} " + "y" * 120, ()))
    return messages


def _make_agent(history: list[Message], registry: ToolRegistry) -> Agent:
    router, _ = scripted_router(json_responses=[openai_message("short answer") for _ in range(400)])
    agent = Agent(
        router,
        registry,
        ToolContext(workspace=Path(".")),
        system_prompt="bench system prompt",
        volatile_prompt="Current date: 2026-01-01",
        stream=False,
    )
    agent.history.extend(history)
    return agent


def _run_agent_turn(agent: Agent, text: str) -> None:
    async def go():
        async for _ in agent.run(text):
            pass

    asyncio.run(go())


def bench_cold_build(repeats: int = 5) -> dict[str, float]:
    if not HAVE_ENGINE:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("EURUS_HOME")
        os.environ["EURUS_HOME"] = tmp
        try:
            workspace = Path(tmp) / "ws"
            (workspace / "pkg").mkdir(parents=True)
            for index in range(40):
                (workspace / "pkg" / f"m{index}.py").write_text(
                    f"import os\ndef fn{index}(): pass\n", encoding="utf-8"
                )
            router, _ = scripted_router(json_responses=[])
            config = Config()
            times = []
            for _ in range(repeats):
                started = time.perf_counter()
                build_agent(config, router, workspace=workspace, shell=False, learn=False)
                times.append((time.perf_counter() - started) * 1000)
        finally:
            if previous is None:
                os.environ.pop("EURUS_HOME", None)
            else:
                os.environ["EURUS_HOME"] = previous
    return {"cold_build_agent_ms": statistics.median(times)}


def bench_tui_turns(repeats: int) -> dict[str, float]:
    registry = _registry()
    results: dict[str, float] = {}
    for size in HISTORY_SIZES:
        fresh_times: list[float] = []
        reuse_times: list[float] = []
        base_history = [Message.system("bench system prompt"), *_history(size)]
        for _ in range(repeats):
            # baseline: build a fresh agent for every turn
            started = time.perf_counter()
            agent = _make_agent(_history(size), registry)
            _run_agent_turn(agent, "one short question")
            fresh_times.append((time.perf_counter() - started) * 1000)
            # session cache: build once, then run turns on the same agent
            reused = _make_agent([], registry)
            for turn in range(repeats):
                reused.history = list(base_history)
                started = time.perf_counter()
                _run_agent_turn(reused, f"one short question {turn}")
                reuse_times.append((time.perf_counter() - started) * 1000)
        results[f"tui_fresh_h{size}_ms"] = statistics.median(fresh_times)
        results[f"tui_reuse_h{size}_ms"] = statistics.median(reuse_times)
    return results


def bench_prompt_build(repeats: int = 20) -> dict[str, float]:
    registry = _registry()
    agent = _make_agent(_history(500), registry)
    agent._window = ContextWindow(4000, keep_recent=20)
    times: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        agent._prompt()
        times.append((time.perf_counter() - started) * 1000)
    return {"prompt_build_h500_fit4k_ms": statistics.median(times)}


def bench_checkpoint(repeats: int = 10) -> dict[str, float]:
    messages = _history(200)
    state = {
        "objective": "bench",
        "iteration": 7,
        "side_effect_status": "settled",
        "history": [message.to_dict() for message in messages],
    }
    results: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        store = RunStore(Path(tmp) / "runs.sqlite3")
        from eurus.runtime.models import Run

        run = Run(owner_id="bench", objective="bench")
        store.create(run)
        for _ in range(repeats):
            started = time.perf_counter()
            store.save_checkpoint(run.run_id, state, checkpoint_type="runtime")
            results.append((time.perf_counter() - started) * 1000)
    return {"checkpoint_save_h200_ms": statistics.median(results)}


def bench_engine_turns(repeats: int) -> dict[str, float]:
    if not HAVE_ENGINE:
        return {}
    results: dict[str, float] = {}

    def _engine_run(size: int, repeat: int, **engine_kwargs) -> float:
        with tempfile.TemporaryDirectory() as tmp:
            run_store = RunStore(Path(tmp) / "runs.sqlite3")
            registry = _registry()
            agent = _make_agent(_history(size, offset=repeat), registry)
            engine = AgentEngine(agent, run_store=run_store, **engine_kwargs)
            started = time.perf_counter()
            asyncio.run(engine.run(f"bench objective {repeat}"))
            return (time.perf_counter() - started) * 1000

    def _engine_run_steps(steps: int, size: int, repeat: int, **engine_kwargs) -> float:
        with tempfile.TemporaryDirectory() as tmp:
            run_store = RunStore(Path(tmp) / "runs.sqlite3")
            registry = _registry()
            router, _ = scripted_router(
                json_responses=[
                    *(
                        openai_message("", calls=[(f"c{i}", "noop", {"n": i})])
                        for i in range(steps)
                    ),
                    openai_message("final answer"),
                ]
            )
            agent = Agent(
                router,
                registry,
                ToolContext(workspace=Path(".")),
                system_prompt="bench system prompt",
                volatile_prompt="Current date: 2026-01-01",
                stream=False,
            )
            agent.history.extend(_history(size, offset=repeat))
            engine = AgentEngine(agent, run_store=run_store, **engine_kwargs)
            started = time.perf_counter()
            asyncio.run(engine.run(f"bench objective {repeat}"))
            return (time.perf_counter() - started) * 1000

    for size in HISTORY_SIZES:
        times: list[float] = []
        times_default: list[float] = []
        times_steps6: list[float] = []
        times_steps6_default: list[float] = []
        for repeat in range(repeats):
            times.append(_engine_run(size, repeat, checkpoint_interval_steps=1))
            times_default.append(_engine_run(size, repeat))
            times_steps6.append(_engine_run_steps(6, size, repeat, checkpoint_interval_steps=1))
            times_steps6_default.append(_engine_run_steps(6, size, repeat))
        results[f"engine_turn_h{size}_ms"] = statistics.median(times)
        results[f"engine_default_turn_h{size}_ms"] = statistics.median(times_default)
        results[f"engine_6steps_h{size}_ms"] = statistics.median(times_steps6)
        results[f"engine_6steps_default_h{size}_ms"] = statistics.median(times_steps6_default)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=REPEAT_TURNS)
    parser.add_argument("--json", type=str, default="")
    arguments = parser.parse_args()

    results: dict[str, float] = {}
    results.update(bench_cold_build())
    results.update(bench_tui_turns(arguments.repeats))
    results.update(bench_prompt_build())
    results.update(bench_checkpoint())
    results.update(bench_engine_turns(arguments.repeats))

    width = max(len(name) for name in results)
    print("offline benchmark (median, lower is better)")
    for name, value in sorted(results.items()):
        print(f"  {name:<{width}}  {value:10.2f} ms")
    if arguments.json:
        Path(arguments.json).write_text(
            json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
