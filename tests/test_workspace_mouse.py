import asyncio

from eurus.tui.workspace.clipboard import copy_to_terminal_clipboard, osc52_sequence
from eurus.tui.workspace.mouse import wheel_handler


class FakeOutput:
    def __init__(self):
        self.written = []

    def write(self, text):
        self.written.append(text)

    def flush(self):
        return None


def test_osc52_sequence_encodes_text():
    sequence = osc52_sequence("hello")
    assert sequence.startswith("\x1b]52;c;")
    assert sequence.endswith("\x07")
    import base64

    assert base64.b64decode(sequence[7:-1]).decode() == "hello"


def test_copy_to_terminal_clipboard_writes_sequence():
    output = FakeOutput()
    assert copy_to_terminal_clipboard("data", output) is True
    assert output.written and "\x1b]52;c;" in output.written[0]
    assert copy_to_terminal_clipboard("", output) is False


def test_wheel_handler_dispatches_directions():
    calls = []
    handler = wheel_handler(lambda: calls.append("up"), lambda: calls.append("down"))
    from prompt_toolkit.layout.screen import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType

    class FakeEvent:
        def __init__(self, kind):
            self.event_type = kind
            self.position = Point(0, 0)
            self.button = MouseButton.NONE
            self.modifiers = frozenset()

    handler(FakeEvent(MouseEventType.SCROLL_UP))
    handler(FakeEvent(MouseEventType.SCROLL_DOWN))
    assert calls == ["up", "down"]


def test_mouse_sgr_sequences_reach_panels(tmp_path, monkeypatch):
    from test_workspace_render import build_running_app, teardown

    (tmp_path / "mouse.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        pipe.send_text("\x1b")
        await asyncio.sleep(1.2)
        assert ui.mode == "chat"
        pipe.send_text("\x1b[<0;5;5M")
        await asyncio.sleep(0.3)
        pipe.send_text("\x1b[<0;5;5m")
        await asyncio.sleep(0.5)
        assert store.focus == "explorer"
        assert ui.explorer.selected >= 0
        pipe.send_text("\x1b[<64;5;5M")
        await asyncio.sleep(0.4)
        assert ui.explorer.offset >= 0
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)


def test_mouse_click_opens_file_on_double_click(tmp_path, monkeypatch):
    from test_workspace_render import build_running_app, teardown

    (tmp_path / "target.py").write_text("content = 42\n", encoding="utf-8")
    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built
    explorer = ui.explorer
    index = next(
        (i for i, node in enumerate(explorer.rows) if node.name == "target.py"),
        None,
    )
    assert index is not None

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        pipe.send_text("\x1b")
        await asyncio.sleep(1.2)
        ticks = iter(range(1000))
        monkeypatch.setattr(ui, "_click_clock", lambda: next(ticks) * 0.05)
        first = ui._explorer_row_handler(index)
        from prompt_toolkit.layout.screen import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEventType

        class FakeEvent:
            event_type = MouseEventType.MOUSE_UP
            position = Point(0, 0)
            button = MouseButton.LEFT
            modifiers = frozenset()

        first(FakeEvent())
        assert ui.tabs.active.kind == "chat"
        first(FakeEvent())
        assert ui.tabs.active.kind == "editor"
        assert ui.tabs.active.path == "target.py"
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)


def test_mouse_splitter_drag_resizes_panel(tmp_path, monkeypatch):
    from prompt_toolkit.layout.screen import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType

    from test_workspace_render import build_running_app, teardown

    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        width_before = store.explorer_width

        class FakeEvent:
            event_type = MouseEventType.MOUSE_MOVE
            position = Point(35, 2)
            button = MouseButton.LEFT
            modifiers = frozenset()

        class FakeDown(FakeEvent):
            event_type = MouseEventType.MOUSE_DOWN

        class FakeUp(FakeEvent):
            event_type = MouseEventType.MOUSE_UP

        handler = ui._splitter_handler("explorer")
        handler(FakeDown())
        assert ui._drag.active
        handler(FakeEvent())
        assert store.explorer_width == 35
        assert store.explorer_width != width_before
        handler(FakeUp())
        assert not ui._drag.active
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)


def test_mouse_wheel_scrolls_activity(tmp_path, monkeypatch):
    from eurus.agent.events import AgentEvent
    from eurus.models import ToolCall
    from test_workspace_render import build_running_app, teardown

    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built
    for index in range(12):
        ui.activity.on_event(
            AgentEvent.tool_started(
                ToolCall(id=f"c{index}", name="shell", arguments={"command": "x"})
            )
        )
    handler = ui._activity_row_handler(5)

    class FakeEvent:
        from prompt_toolkit.mouse_events import MouseEventType

        event_type = MouseEventType.SCROLL_UP
        from prompt_toolkit.layout.screen import Point

        position = Point(0, 0)

    class FakeUp(FakeEvent):
        from prompt_toolkit.mouse_events import MouseEventType

        event_type = MouseEventType.SCROLL_UP

    handler(FakeUp())
    assert ui.activity.selected < 11
    teardown(pipe_context, app_context)


def test_terminal_state_restored_after_exit(tmp_path, monkeypatch):
    from test_workspace_render import build_running_app, teardown

    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.5)
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)
    frame = buffer.getvalue()
    assert "\x1b[?1000h" in frame or "\x1b[?1006h" in frame
    assert "\x1b[?1000l" in frame or "\x1b[?1006l" in frame


def test_double_click_tracker_requires_same_target_within_window():
    from eurus.tui.workspace.mouse import DoubleClickTracker

    now = [0.0]
    tracker = DoubleClickTracker(lambda: now[0], window=0.4)
    assert tracker.register(1) is False
    now[0] = 0.1
    assert tracker.register(2) is False
    now[0] = 0.2
    assert tracker.register(2) is True
    now[0] = 0.3
    assert tracker.register(2) is False
    now[0] = 0.9
    assert tracker.register(2) is False
