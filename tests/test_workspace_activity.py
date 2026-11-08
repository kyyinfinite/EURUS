import asyncio

from eurus.agent.events import AgentEvent
from eurus.models import ToolCall, ToolResult, Usage
from eurus.tui.workspace.activity import ActivityFeed


def build_feed():
    feed = ActivityFeed()
    seen = []
    feed.on_change = lambda: seen.append(len(feed.items))
    return feed, seen


def test_step_creates_running_thinking_indicator():
    feed, _ = build_feed()
    feed.on_event(AgentEvent.step_started(1))
    assert len(feed.items) == 1
    item = feed.items[0]
    assert item.kind == "thinking" and item.status == "running"
    feed.on_event(AgentEvent.step_started(2))
    assert len(feed.items) == 1


def test_tool_call_and_result_lifecycle():
    feed, _ = build_feed()
    feed.on_event(
        AgentEvent.tool_started(ToolCall(id="c1", name="shell", arguments={"command": "ls"}))
    )
    item = feed.items[-1]
    assert item.status == "running"
    assert item.tool_call_id == "c1"
    assert "shell" in item.title
    feed.on_event(
        AgentEvent.tool_finished(
            ToolResult(call_id="c1", name="shell", output="file.txt\nother.txt", is_error=False)
        )
    )
    assert item.status == "ok"
    assert item.duration >= 0
    assert "file.txt" in item.detail


def test_tool_error_status():
    feed, _ = build_feed()
    feed.on_event(
        AgentEvent.tool_started(ToolCall(id="c2", name="read_file", arguments={"path": "a.py"}))
    )
    feed.on_event(
        AgentEvent.tool_finished(
            ToolResult(call_id="c2", name="read_file", output="missing", is_error=True)
        )
    )
    assert feed.items[-1].status == "error"


def test_file_tool_events_carry_paths():
    feed, _ = build_feed()
    feed.on_event(
        AgentEvent.tool_started(ToolCall(id="r1", name="read_file", arguments={"path": "src/a.py"}))
    )
    assert feed.items[-1].kind == "file_read"
    assert feed.items[-1].path == "src/a.py"
    feed.on_event(
        AgentEvent.tool_started(
            ToolCall(id="w1", name="edit_file", arguments={"path": "src/a.py", "old_string": "x"})
        )
    )
    assert feed.items[-1].kind == "file_edit"
    assert "src/a.py" in feed.items[-1].title


def test_done_event_completes_thinking_and_shows_usage():
    feed, _ = build_feed()
    feed.on_event(AgentEvent.step_started(1))
    feed.on_event(
        AgentEvent.finished("completed", Usage(input_tokens=30, output_tokens=12), "all set")
    )
    kinds = [item.kind for item in feed.items]
    assert "done" in kinds
    assert feed.items[-1].usage_text == "42 tokens"
    assert feed.items[0].status == "ok"


def test_lesson_plan_and_recovery_events():
    feed, _ = build_feed()
    feed.on_event(AgentEvent.lesson_saved("prefer explicit imports"))
    feed.on_event(AgentEvent.plan_updated("phase 1 done", step=2))
    feed.on_event(AgentEvent.recovery_attempted("retry after timeout", step=3))
    titles = [item.title for item in feed.items]
    assert any("lesson" in title for title in titles)
    assert any("plan" in title for title in titles)
    assert any("recovery" in title for title in titles)
    assert feed.items[-1].status == "error"


def test_approval_notes():
    feed, _ = build_feed()
    feed.on_note("approval", "shell", "command=rm -rf")
    assert feed.items[-1].status == "pending"
    feed.on_note("approval_result", "shell", "allowed once")
    assert feed.items[-1].status == "allowed"
    feed.on_note("denied", "shell", "denied by owner")
    assert feed.items[-1].status == "denied"


def test_sub_agent_activity_is_distinguishable():
    feed, _ = build_feed()
    feed.note_sub_agent("sub-agent: tests", "running pytest")
    item = feed.items[-1]
    assert item.kind == "sub_agent"
    assert item.depth == 1


def test_feed_navigation_and_expand():
    feed, _ = build_feed()
    for index in range(5):
        feed.on_event(
            AgentEvent.tool_started(
                ToolCall(id=f"c{index}", name="shell", arguments={"command": f"cmd{index}"})
            )
        )
    feed.move(-1)
    feed.move(-1)
    assert feed.selected == 2
    assert not feed.follow
    feed.toggle_expand()
    assert feed.items[2].expanded
    feed.move(100)
    assert feed.follow
    rows = feed.visible_rows(10)
    assert any(index >= 0 for _, index in rows)
    assert any(index < 0 for _, index in rows)


def test_feed_caps_history():
    feed, _ = build_feed()
    for index in range(500):
        feed.on_event(
            AgentEvent.tool_started(
                ToolCall(id=f"c{index}", name="shell", arguments={"command": "x"})
            )
        )
    assert len(feed.items) <= 400


def test_feed_ignores_streaming_events():
    feed, _ = build_feed()
    feed.on_event(AgentEvent.text_delta("streaming text"))
    feed.on_event(AgentEvent.text_reset())
    feed.on_event(AgentEvent.model_completed(1, None, "stop"))
    assert feed.items == []


def test_session_bridge_receives_real_events(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.test_workspace_render import build_running_app, teardown

    class FakeRouter:
        async def aclose(self) -> None:
            return None

    class FakeAgent:
        def __init__(self) -> None:
            from eurus.models import Message

            self.history = [Message.system("system")]

        async def run(self, text):
            from eurus.models import Message

            self.history.append(Message.user(text))
            yield AgentEvent.step_started(1)
            yield AgentEvent.tool_started(
                ToolCall(id="t1", name="read_file", arguments={"path": "notes.md"})
            )
            yield AgentEvent.tool_finished(
                ToolResult(call_id="t1", name="read_file", output="contents")
            )
            yield AgentEvent.text_delta("done working")
            self.history.append(Message.assistant("done working"))
            yield AgentEvent.finished("completed", Usage(10, 5))

    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built
    session.config = replace(
        session.config, agent=replace(session.config.agent, model="groq/llama")
    )
    monkeypatch.setattr("eurus.tui.session.build_router", lambda config: FakeRouter())
    monkeypatch.setattr(
        "eurus.tui.session.build_agent", lambda config, router, **kwargs: FakeAgent()
    )
    session.activity_sink = ui.activity

    async def go():
        ui.dispatch("inspect the workspace")
        await ui.task

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)
    kinds = [item.kind for item in ui.activity.items]
    assert "thinking" in kinds
    assert "file_read" in kinds
    assert "done" in kinds
    statuses = [item.status for item in ui.activity.items if item.tool_call_id == "t1"]
    assert statuses == ["ok"]
    rendered = ui._render_activity()
    text = "".join(part[1] for part in rendered)
    assert "read_file notes.md" in text
    assert "ACTIVITY" in text
