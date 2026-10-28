import asyncio

import pytest
from rich.console import Console

from eurus.agent.events import AgentEvent
from eurus.config import load_config
from eurus.models import Message, Usage
from eurus.tui import InteractiveSession, TerminalRenderer


class FakeRouter:
    async def aclose(self) -> None:
        return None


class FakeAgent:
    def __init__(self) -> None:
        self.history = [Message.system("system")]

    async def run(self, text):
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta("working")
        self.history.append(Message.assistant("done"))
        yield AgentEvent.finished("completed", Usage(10, 5))


def make_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={"agent": {"model": "groq/llama"}},
    )
    console = Console(record=True, width=60, force_terminal=False)
    session = InteractiveSession(config, TerminalRenderer(console))
    monkeypatch.setattr("eurus.tui.session.build_router", lambda config: FakeRouter())
    monkeypatch.setattr(
        "eurus.tui.session.build_agent", lambda config, router, **kwargs: FakeAgent()
    )
    return session, console


def test_tui_turn_runs_through_the_shared_runtime(tmp_path, monkeypatch):
    session, console = make_session(tmp_path, monkeypatch)

    async def scenario():
        runtime = session._runtime_for_turn()
        _, queue = runtime.event_bridge.subscribe(session.session_id)
        await session.submit("hello there")
        kinds = []
        while not queue.empty():
            kinds.append(queue.get_nowait().event.kind)
        assert runtime.active_runs == 0
        assert runtime.agents == {}
        await session.close()
        return kinds

    kinds = asyncio.run(scenario())
    assert kinds == ["text", "done"]
    assert len(session.history) == 2
    assert [m.content for m in session.session_store.load_messages(session.session_id)] == [
        "hello there",
        "done",
    ]


def test_tui_cancelled_turn_does_not_persist_partial_history(tmp_path, monkeypatch):
    session, _ = make_session(tmp_path, monkeypatch)

    class HangingAgent(FakeAgent):
        async def run(self, text):
            self.history.append(Message.user(text))
            yield AgentEvent.text_delta("start")
            await asyncio.sleep(30)

    monkeypatch.setattr(
        "eurus.tui.session.build_agent", lambda config, router, **kwargs: HangingAgent()
    )

    async def scenario():
        task = asyncio.create_task(session.submit("slow request"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session._runtime_for_turn().active_runs == 0
        await session.close()

    asyncio.run(scenario())
    assert session.session_store.load_messages(session.session_id) == []
    assert session.retry_prompt == "slow request"


def test_settings_command_prints_table_without_picker(tmp_path, monkeypatch):
    session, console = make_session(tmp_path, monkeypatch)
    asyncio.run(session.submit("/settings"))
    output = console.export_text()
    assert "theme" in output and "markdown" in output and "sidebar" in output
    assert "stream preview" in output


def test_set_pref_persists_and_updates_renderer(tmp_path, monkeypatch):
    from eurus.tui import prefs

    session, _ = make_session(tmp_path, monkeypatch)
    assert session.prefs.markdown is True
    session.set_pref("markdown", False)
    assert session.renderer.markdown_enabled is False
    loaded = prefs.load(home=tmp_path)
    assert loaded.markdown is False
    session.set_pref("bogus", True)
    assert session.prefs.sidebar is True


def test_workspace_command_shows_paths_and_restart_note(tmp_path, monkeypatch):
    session, console = make_session(tmp_path, monkeypatch)
    asyncio.run(session.submit("/workspace"))
    output = console.export_text()
    assert "workspace" in output
    assert "restart Eurus" in output
