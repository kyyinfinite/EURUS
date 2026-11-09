import asyncio
import json
from pathlib import Path

import pytest

from eurus.agent import Agent
from eurus.content import AudioBlock, ImageBlock, TextBlock, referenced_asset_ids
from eurus.models import Message
from eurus.providers.testing import openai_stream, scripted_router
from eurus.sessions import SessionStore
from eurus.tools import ToolContext, default_registry

ASSET_A = "a" * 32
ASSET_B = "b" * 32


def make_agent(router):
    context = ToolContext(workspace=Path("."), max_output=1000, timeout=5.0)
    return Agent(
        router,
        default_registry(),
        context,
        system_prompt="sys",
        max_steps=10,
        stream=True,
    )


def run_agent(agent, text, blocks=()):
    async def go():
        return [event async for event in agent.run(text, blocks=blocks)]

    return asyncio.run(go())


def test_agent_appends_user_message_with_blocks():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router)
    blocks = (
        TextBlock("analyze this image"),
        ImageBlock(asset_id=ASSET_A, mime_type="image/jpeg"),
    )
    events = run_agent(agent, "analyze this image", blocks=blocks)
    assert [event.kind for event in events][-1] == "done"
    user_messages = [m for m in agent.history if m.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].blocks == blocks
    assert user_messages[0].content == "analyze this image"


def test_agent_without_blocks_keeps_plain_history():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router)
    run_agent(agent, "plain text only")
    user_message = next(m for m in agent.history if m.role == "user")
    assert user_message.blocks == ()
    assert user_message.content == "plain text only"


def test_agent_wire_payload_stays_text_only():
    router, transport = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router)
    blocks = (
        TextBlock("describe"),
        ImageBlock(asset_id=ASSET_A, mime_type="image/png"),
        AudioBlock(asset_id=ASSET_B, mime_type="audio/mpeg"),
    )
    run_agent(agent, "describe", blocks=blocks)
    wire_messages = transport.calls[0].payload["messages"]
    user_wire = next(m for m in wire_messages if m["role"] == "user")
    assert isinstance(user_wire["content"], str)
    assert "blocks" not in user_wire
    encoded = json.dumps(wire_messages)
    assert ASSET_A not in encoded.replace(user_wire["content"], "")


def test_session_store_roundtrip_preserves_blocks(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    blocks = (
        TextBlock("mixed turn"),
        ImageBlock(asset_id=ASSET_A, mime_type="image/jpeg", width=800, height=600),
        AudioBlock(asset_id=ASSET_B, mime_type="audio/mpeg", duration_seconds=3.5),
    )
    messages = [
        Message.system("sys"),
        Message.user("mixed turn", blocks),
        Message.assistant("here is the analysis"),
    ]
    store.save_messages("session-blocks", messages)
    loaded = store.load_messages("session-blocks")
    assert [m.role for m in loaded] == ["user", "assistant"]
    assert loaded[0].blocks == blocks
    assert loaded[0].content == "mixed turn"
    assert referenced_asset_ids(loaded[0].blocks) == (ASSET_A, ASSET_B)


def test_session_store_loads_legacy_payloads_without_blocks(tmp_path: Path):
    store = SessionStore(tmp_path / "legacy.db")
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO sessions(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("legacy", "legacy", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        legacy_payload = json.dumps(
            {"role": "user", "content": "old school text"}, ensure_ascii=False
        )
        connection.execute(
            "INSERT INTO messages(session_id, sequence, payload, created_at) VALUES (?, ?, ?, ?)",
            ("legacy", 0, legacy_payload, "2026-01-01T00:00:00+00:00"),
        )
    loaded = store.load_messages("legacy")
    assert len(loaded) == 1
    assert loaded[0].content == "old school text"
    assert loaded[0].blocks == ()


def test_session_store_search_indexes_mixed_content_text(tmp_path: Path):
    store = SessionStore(tmp_path / "search.db")
    blocks = (TextBlock("quantum lattice findings"), ImageBlock(ASSET_A, "image/png"))
    store.save_messages("s", [Message.user("", blocks)])
    results = store.search_session_messages("quantum", limit=5)
    assert any("quantum" in row["content"] for row in results)


def test_checkpoint_history_snapshot_includes_blocks(tmp_path: Path):
    from eurus.runtime.engine import AgentEngine
    from eurus.storage.events import EventStore

    router, _ = scripted_router(streams=[openai_stream("done")])
    agent = make_agent(router)
    engine = AgentEngine(agent, event_store=EventStore(tmp_path / "events.db"))
    blocks = (TextBlock("with blocks"), ImageBlock(ASSET_A, "image/jpeg"))

    async def go():
        return await engine.run("with blocks", blocks=blocks)

    result = asyncio.run(go())
    assert result.run.status.value in {"completed", "unverified"}
    snapshot = engine._history_snapshot()
    user_entries = [entry for entry in snapshot if entry["role"] == "user"]
    assert user_entries
    assert user_entries[-1]["blocks"][0] == {"type": "text", "text": "with blocks"}


def test_engine_run_forwards_blocks_to_history(tmp_path: Path):
    from eurus.runtime.engine import AgentEngine

    router, _ = scripted_router(streams=[openai_stream("processed")])
    agent = make_agent(router)
    engine = AgentEngine(agent)
    blocks = (TextBlock("engine turn"), AudioBlock(ASSET_B, "audio/wav"))

    async def go():
        return await engine.run("engine turn", blocks=blocks)

    asyncio.run(go())
    user_message = next(m for m in agent.history if m.role == "user")
    assert user_message.blocks == blocks


def test_runtime_stream_forwards_blocks(tmp_path: Path):
    from eurus.runtime.application import AgentRuntime
    from eurus.sessions import SessionStore

    router, _ = scripted_router(streams=[openai_stream("streamed")])
    agent = make_agent(router)
    store = SessionStore(tmp_path / "runtime.db")
    runtime = AgentRuntime(lambda session_id: agent, store)
    blocks = (TextBlock("runtime turn"), ImageBlock(ASSET_A, "image/png"))

    async def go():
        events = []
        async for event in runtime.run("session-1", "runtime turn", blocks=blocks):
            events.append(event)
        return events

    events = asyncio.run(go())
    assert events[-1].kind == "done"
    user_message = next(m for m in agent.history if m.role == "user")
    assert user_message.blocks == blocks
    persisted = store.load_messages("session-1")
    assert persisted[0].blocks == blocks


@pytest.mark.parametrize("text", ["", "   "])
def test_runtime_rejects_empty_text_with_no_blocks(text, tmp_path: Path):
    from eurus.runtime.application import AgentRuntime

    router, _ = scripted_router(streams=[openai_stream("x")])
    agent = make_agent(router)
    runtime = AgentRuntime(lambda session_id: agent, SessionStore(tmp_path / "unused.db"))

    async def go():
        async for _ in runtime.run("session", text):
            pass

    with pytest.raises(ValueError, match="message text must not be empty"):
        asyncio.run(go())
