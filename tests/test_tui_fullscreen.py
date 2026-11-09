import asyncio
from io import StringIO

import pytest

from eurus.config import load_config
from eurus.tui.terminal import layout_mode
from eurus.tui.theme import Theme
from eurus.tui.transcript import KEEP_LINES, MAX_LINES, TranscriptSink


def test_sink_splits_lines_and_keeps_partial_tail():
    sink = TranscriptSink()
    sink.write("one\ntw")
    sink.write("o\nthree")
    assert sink.lines == ["one", "two", "three"]
    sink.write("\n")
    assert sink.lines[-1] == ""


def test_sink_strips_osc_private_modes_and_carriage_returns():
    sink = TranscriptSink()
    sink.write("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ \x1b[?25lok\r\n")
    assert sink.lines[0] == "link ok"
    sink.write("\x1b]2;title\x07done")
    assert sink.lines[-1] == "done"


def test_sink_notifies_clears_and_caps():
    sink = TranscriptSink()
    seen = []
    sink.on_change = lambda: seen.append(sink.version)
    sink.write("a\n")
    assert seen == [1]
    sink.clear()
    assert sink.lines == [""] and seen == [1, 2]
    sink.write("x\n" * (MAX_LINES + 10))
    assert len(sink.lines) <= MAX_LINES and len(sink.lines) >= KEEP_LINES
    assert not sink.isatty() and sink.encoding == "utf-8"


def test_layout_mode_resolution():
    assert layout_mode({}, tty=True) == "fullscreen"
    assert layout_mode({}, tty=False) == "inline"
    assert layout_mode({}, tty=True) == "fullscreen"
    assert layout_mode({"EURUS_LAYOUT": "FullScreen"}, tty=True) == "fullscreen"
    assert layout_mode({"EURUS_LAYOUT": "never"}, tty=True) == "inline"
    assert layout_mode({"EURUS_LAYOUT": "inline"}, tty=True) == "inline"
    assert layout_mode({"EURUS_LAYOUT": "tiles"}, tty=True) == "fullscreen"


def build_ui(tmp_path, monkeypatch):
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from eurus.tui.fullscreen import FullscreenUI, attach
    from eurus.tui.session import InteractiveSession

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    monkeypatch.delenv("EURUS_THEME", raising=False)
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    session = InteractiveSession(config)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        renderer, sink = attach(session)
        ui = FullscreenUI(session, renderer, sink)
    session.picker = ui
    return ui, sink, session


def text_of(fragments):
    return "".join(part[1] for part in fragments)


def test_transcript_follows_bottom_and_scrolls(tmp_path, monkeypatch):
    ui, sink, _ = build_ui(tmp_path, monkeypatch)
    for number in range(100):
        sink.write(f"line {number}\n")
    rendered = text_of(ui._render_transcript())
    assert "line 99" in rendered and "line 0\n" not in rendered
    ui._scroll(-30)
    assert not ui.follow
    assert "line 99" not in text_of(ui._render_transcript())
    ui._scroll(10_000)
    assert ui.follow and "line 99" in text_of(ui._render_transcript())


def test_dispatch_runs_command_into_transcript(tmp_path, monkeypatch):
    ui, sink, _ = build_ui(tmp_path, monkeypatch)

    async def go():
        ui.dispatch("/theme list")
        await ui.task

    asyncio.run(go())
    assert "midnight" in "\n".join(sink.lines)


def test_theme_command_opens_live_preview_picker(tmp_path, monkeypatch):
    ui, _, session = build_ui(tmp_path, monkeypatch)

    async def go():
        ui.dispatch("/theme")
        for _ in range(300):
            if ui.mode == "theme" and ui.overlay is not None:
                break
            await asyncio.sleep(0.01)
        assert ui.mode == "theme" and ui.overlay is not None
        assert any(item.action == "theme-name" for item in ui.overlay.items)
        before = session.renderer.theme
        ui._overlay_theme_move(1)
        assert session.renderer.theme != before or ui.overlay.index == 1
        ui._overlay_escape()
        assert ui.mode == "chat"
        assert session.renderer.theme == before

    asyncio.run(go())


def test_exit_command_stops_session(tmp_path, monkeypatch):
    ui, _, session = build_ui(tmp_path, monkeypatch)

    async def go():
        ui.dispatch("/exit")
        await ui.task

    asyncio.run(go())
    assert not session.running


def test_confirm_and_ask_settle_with_user_answers(tmp_path, monkeypatch):
    from eurus.tui.fullscreen import INTERRUPT

    ui, _, _ = build_ui(tmp_path, monkeypatch)

    async def yes():
        asyncio.get_running_loop().call_soon(ui._settle, True)
        return await ui.confirm("Allow once?", "allow", "deny", ("a", "y"))

    async def interrupted():
        asyncio.get_running_loop().call_soon(ui._settle, INTERRUPT)
        try:
            await ui.confirm("Allow once?", "allow", "deny", ("a", "y"))
        except KeyboardInterrupt:
            return "interrupted"
        return "no"

    async def answer():
        asyncio.get_running_loop().call_soon(ui._settle, "groq")
        return await ui.ask_options("provider>", ["groq", "openai"])

    assert asyncio.run(yes()) is True
    assert asyncio.run(interrupted()) == "interrupted"
    assert asyncio.run(answer()) == "groq"
    assert ui.mode == "chat"


def test_welcome_replaced_by_transcript(tmp_path, monkeypatch):
    ui, sink, _ = build_ui(tmp_path, monkeypatch)
    assert ui._is_welcome()
    welcome = text_of(ui._render_main())
    assert "THINK / ANALYZE / EXECUTE" in welcome
    assert "█" in welcome or "▀" in welcome
    sink.write("user message\n")
    assert not ui._is_welcome()
    assert "user message" in text_of(ui._render_main())
    sink.clear()
    assert ui._is_welcome()


def test_palette_opens_closes_and_searches(tmp_path, monkeypatch):
    ui, _, _ = build_ui(tmp_path, monkeypatch)
    assert ui.mode == "palette"
    labels = [item.label for item in ui.overlay.matches()]
    assert "Chat" in labels and "New Session" in labels and "/memory" in labels
    ui.search_buffer.text = "new se"
    assert [item.label for item in ui.overlay.matches()] == ["New Session"]
    ui._close_overlay()
    assert ui.mode == "chat" and ui.overlay is None
    ui._open_palette()
    assert ui.mode == "palette" and ui.overlay is not None


def test_palette_selection_dispatches_clear(tmp_path, monkeypatch):
    ui, sink, _ = build_ui(tmp_path, monkeypatch)
    sink.write("old conversation\n")

    async def go():
        ui.search_buffer.text = "new se"
        ui._overlay_enter()
        assert ui.mode == "chat"
        await ui.task

    asyncio.run(go())
    joined = "\n".join(sink.lines)
    assert "old conversation" not in joined
    assert "Conversation cleared" in joined


def test_palette_sample_fills_composer(tmp_path, monkeypatch):
    ui, _, _ = build_ui(tmp_path, monkeypatch)

    async def go():
        ui.search_buffer.text = "explain"
        ui.overlay.index = 0
        item = ui.overlay.selected()
        assert item.action == "sample"
        ui._overlay_enter()
        return item

    item = asyncio.run(go())
    assert ui.mode == "chat"
    assert ui.buffer.text == item.payload


def test_settings_toggle_persists(tmp_path, monkeypatch):
    ui, _, session = build_ui(tmp_path, monkeypatch)
    ui.open_settings()
    assert ui.mode == "settings"
    labels = [item.label for item in ui.overlay.matches()]
    assert {"Theme", "Glyphs", "Markdown", "Stream preview"} <= set(labels)
    assert "Sidebar" not in labels
    assert session.prefs.markdown is True
    ui._toggle_setting("markdown")
    assert session.prefs.markdown is False
    assert ui.renderer.markdown_enabled is False
    saved = (tmp_path / "ui.json").read_text(encoding="utf-8")
    assert '"markdown": false' in saved
    ui._toggle_setting("markdown")
    assert session.prefs.markdown is True


def test_stream_preview_renders_and_clears(tmp_path, monkeypatch):
    from eurus.tui.state import AgentState

    ui, _, _ = build_ui(tmp_path, monkeypatch)
    assert ui.renderer.stream_view is None
    with ui.renderer.generating() as view:
        view.state = AgentState.RUNNING
        view.feed("tokens streaming right now")
        rendered = text_of(ui._render_stream())
        assert "RUNNING" in rendered
        assert "tokens streaming right now" in rendered
        assert ui._stream_height() == 2
        view.paused = True
        assert "approval pending" in text_of(ui._render_stream())
    assert ui.renderer.stream_view is None


def test_status_includes_mode_and_version(tmp_path, monkeypatch):
    ui, _, _ = build_ui(tmp_path, monkeypatch)
    parts = ui._status(130)
    flat = "".join(part for part, _ in parts)
    assert "Safe Mode" in flat
    assert "EURUS v" in flat
    assert sum(len(part) for part, _ in parts) <= 130


def test_ask_options_supports_raw_and_escape(tmp_path, monkeypatch):
    ui, _, _ = build_ui(tmp_path, monkeypatch)

    async def raw():
        task = asyncio.create_task(ui.ask_options("model>", ["alpha", "beta"]))
        await asyncio.sleep(0.02)
        ui.search_buffer.text = "n"
        ui._overlay_enter()
        return await task

    async def cancel():
        task = asyncio.create_task(ui.ask_options("model>", ["alpha"]))
        await asyncio.sleep(0.02)
        ui._overlay_escape()
        return await task

    assert asyncio.run(raw()) == "n"
    assert asyncio.run(cancel()) is None
    assert ui.mode == "chat" and ui.overlay is None


def test_transcript_keeps_colors_with_forced_terminal():
    from rich.console import Console
    from rich.text import Text

    sink = TranscriptSink()
    console = Console(
        file=sink,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        width=40,
        highlight=False,
    )
    console.print(Text("hello", style="bold #d08a6a"))
    raw = sink.lines[0]
    assert "38;2;208;138;106" in raw


def test_full_turn_streams_renders_and_finishes(tmp_path, monkeypatch):
    from eurus.agent.events import AgentEvent
    from eurus.models import Message, Usage

    class FakeRouter:
        async def aclose(self) -> None:
            return None

    class FakeAgent:
        def __init__(self) -> None:
            self.history = [Message.system("system")]

        async def run(self, text):
            self.history.append(Message.user(text))
            yield AgentEvent.text_delta("all ")
            yield AgentEvent.text_delta("done")
            self.history.append(Message.assistant("all done"))
            yield AgentEvent.finished("completed", Usage(10, 5))

    ui, sink, session = build_ui(tmp_path, monkeypatch)
    from dataclasses import replace

    session.config = replace(
        session.config, agent=replace(session.config.agent, model="groq/llama")
    )
    monkeypatch.setattr("eurus.tui.session.build_router", lambda config: FakeRouter())
    monkeypatch.setattr(
        "eurus.tui.session.build_agent", lambda config, router, **kwargs: FakeAgent()
    )

    async def go():
        ui.dispatch("hello there")
        await ui.task

    asyncio.run(go())
    joined = "\n".join(sink.lines)
    assert "hello there" in joined
    assert "all done" in joined
    assert ui.renderer.stream_view is None
    assert session.state.value == "idle"
    assert len(session.history) == 2


def test_welcome_art_renders_with_color_system():
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from rich.console import Console

    from eurus.tui.brand import welcome

    buffer = StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        width=60,
        height=20,
        highlight=False,
    )
    console.print(welcome(60, 20, Theme(), color=True))
    raw = buffer.getvalue()
    assert "THINK" in raw
    lines = raw.split("\n")
    first_mark = next(line for line in lines if "\u2588" in line or "\u2580" in line)
    parsed = to_formatted_text(ANSI(first_mark))
    assert any("fg:" in style or "#" in style for style, _ in parsed)
