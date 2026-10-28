"""B2 regression tests: resume must restore the conversation history snapshot.

Before the fix, checkpoints stored no conversation history, so a resumed run
lost every model/tool exchange from the previous execution and only received a
short text directive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eurus.agent.loop import Agent
from eurus.models import Message
from eurus.providers.testing import FakeTransport, openai_stream, scripted_router
from eurus.runtime.engine import AgentEngine
from eurus.runtime.models import Run, RunStatus
from eurus.runtime.resume import RecoveryManager
from eurus.runtime.state_machine import transition_run
from eurus.storage.runs import RunStore
from eurus.tools.base import Tool, ToolContext, ToolRegistry


def _registry() -> ToolRegistry:
    async def noop(arguments, context):
        return "ok"

    tool = Tool(
        "read_value",
        "read a value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        noop,
    )
    return ToolRegistry([tool])


def _agent(streams) -> Agent:
    router, _ = scripted_router(streams=streams)
    return Agent(
        router,
        _registry(),
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
        stream=True,
    )


@pytest.mark.asyncio
async def test_checkpoint_contains_history_snapshot(tmp_path):
    run_store = RunStore(tmp_path / "runtime.sqlite3")
    agent = _agent(
        [
            openai_stream("", [("t1", "read_value", {"value": "a"})]),
            openai_stream("final answer"),
        ]
    )
    engine = AgentEngine(agent, run_store=run_store)
    await engine.run("objective", owner_id="test")

    checkpoint = run_store.latest_checkpoint(agent.run_id)
    assert checkpoint is not None
    history = checkpoint["state"].get("history")
    assert isinstance(history, list) and history, "checkpoint must carry a history snapshot"
    roles = [message.get("role") for message in history]
    assert "assistant" in roles
    assert "tool" in roles
    user_texts = [
        message.get("content", "") for message in history if message.get("role") == "user"
    ]
    assert any("objective" in text for text in user_texts)


def _interrupted_run_with_history(run_store: RunStore) -> Run:
    run = Run(owner_id="op", objective="resume me")
    run_store.create(run)
    transition_run(run, RunStatus.RUNNING)
    run_store.save(run, expected_status_version=run.status_version - 1)
    history = [
        Message.system("sys").to_dict(),
        Message.user("resume me").to_dict(),
        Message.assistant("earlier work done", ()).to_dict(),
    ]
    run_store.save_checkpoint(
        run.run_id,
        {
            "objective": run.objective,
            "iteration": 3,
            "tool_execution": "settled",
            "side_effect_status": "settled",
            "history": history,
        },
        checkpoint_type="runtime",
    )
    transition_run(run, RunStatus.INTERRUPTED, stop_reason="crashed")
    run_store.save(run, expected_status_version=run.status_version - 1)
    return run


def test_resume_seeds_agent_history_from_checkpoint(tmp_path):
    run_store = RunStore(tmp_path / "runtime.sqlite3")
    run = _interrupted_run_with_history(run_store)
    transport_holder: list[FakeTransport] = []

    def factory(resumed: Run) -> AgentEngine:
        router, transport = scripted_router(streams=[openai_stream("resumed answer")])
        transport_holder.append(transport)
        agent = Agent(
            router,
            _registry(),
            ToolContext(workspace=Path(".")),
            system_prompt="sys",
        )
        return AgentEngine(agent, run_store=run_store)

    manager = RecoveryManager(run_store)
    result = asyncio.run(manager.resume(run.run_id, factory))
    assert result.run.status == RunStatus.COMPLETED

    transport = transport_holder[0]
    calls = transport.calls
    assert calls, "the model must have been called during resume"
    messages = calls[0].payload["messages"]
    assistant_contents = [
        message.get("content") or "" for message in messages if message.get("role") == "assistant"
    ]
    assert any("earlier work done" in text for text in assistant_contents), (
        "restored history from the checkpoint must be replayed to the model"
    )


def test_seed_agent_history_replaces_pristine_history(tmp_path):
    from eurus.runtime.resume import _seed_agent_history

    agent = _agent(streams=[openai_stream("done")])
    seeded = _seed_agent_history(
        agent,
        [
            Message.system("old sys"),
            Message.user("previous objective"),
            Message.assistant("earlier work done", ()),
        ],
    )
    assert seeded is True
    roles = [message.role for message in agent.history]
    assert roles == ["system", "user", "assistant"]
    assert "sys" in agent.history[0].content, "current system prompt must be preserved"
    assert agent.history[2].content == "earlier work done"


def test_seed_agent_history_appends_to_existing_conversation(tmp_path):
    from eurus.runtime.resume import _seed_agent_history

    agent = _agent(streams=[openai_stream("done")])
    agent.history.append(Message.user("existing context"))
    seeded = _seed_agent_history(
        agent,
        [
            Message.system("old sys"),
            Message.user("previous objective"),
            Message.assistant("earlier work done", ()),
        ],
    )
    assert seeded is True
    roles = [message.role for message in agent.history]
    assert roles == ["system", "user", "user", "assistant"]
    assert agent.history[-1].content == "earlier work done"


def test_seed_agent_history_ignores_missing_or_corrupt_snapshot(tmp_path):
    from eurus.runtime.resume import _seed_agent_history

    agent = _agent(streams=[openai_stream("done")])
    assert _seed_agent_history(agent, [Message.system("only system")]) is False
    assert _seed_agent_history(agent, []) is False
    before = list(agent.history)
    corrupt: Any = "not a list"
    assert _seed_agent_history(agent, corrupt) is False
    assert agent.history == before
