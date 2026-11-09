import asyncio

import pytest

from eurus.config import load_config


def build_running_app(tmp_path, monkeypatch, columns=140, rows=34):
    pytest.importorskip("prompt_toolkit")
    from io import StringIO

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    from eurus.tui.fullscreen_renderer import FullscreenRenderer
    from eurus.tui.session import InteractiveSession
    from eurus.tui.transcript import TranscriptSink
    from eurus.tui.workspace.shell import WorkspaceApp
    from eurus.tui.workspace.store import WorkspaceStore

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    monkeypatch.delenv("EURUS_THEME", raising=False)
    from dataclasses import replace

    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    config = replace(config, permissions=replace(config.permissions, workspace=str(tmp_path)))
    session = InteractiveSession(config)
    sink = TranscriptSink()
    sink.write("welcome to the workspace\n")
    renderer = FullscreenRenderer(session.renderer.console, session.renderer.theme, sink, "256")
    session.renderer = renderer
    store = WorkspaceStore()
    pipe_context = create_pipe_input()
    pipe = pipe_context.__enter__()
    buffer = StringIO()

    width, height = columns, rows

    class FixedSize:
        columns = width
        rows = height

    output = Vt100_Output(
        buffer,
        lambda: FixedSize(),
        term="xterm-256color",
    )
    app_context = create_app_session(input=pipe, output=output)
    app_context.__enter__()
    ui = WorkspaceApp(session, renderer, sink, store)
    session.picker = ui
    return ui, store, session, sink, pipe, buffer, pipe_context, app_context


def teardown(pipe_context, app_context):
    app_context.__exit__(None, None, None)
    pipe_context.__exit__(None, None, None)


def test_workspace_renders_full_frame(tmp_path, monkeypatch):
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
    assert "EURUS" in frame
    assert "EXPLORER" in frame
    assert "ACTIVITY" in frame
    assert "Chat" in frame
    assert "welcome to the workspace" in frame
    assert "Ctrl+K" in frame


def test_workspace_key_feed_switches_focus_and_presets(tmp_path, monkeypatch):
    built = build_running_app(tmp_path, monkeypatch)
    ui, store, session, sink, pipe, buffer, pipe_context, app_context = built

    async def go():
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.4)
        pipe.send_text("\x1b")
        await asyncio.sleep(1.5)
        assert ui.mode == "chat"
        pipe.send_text("\x1b2")
        await asyncio.sleep(0.2)
        assert store.focus == "explorer"
        pipe.send_text("\x1bOQ")
        await asyncio.sleep(0.2)
        assert not store.explorer_visible
        pipe.send_text("\x1bOQ")
        await asyncio.sleep(0.2)
        assert store.explorer_visible
        pipe.send_text("\x1b[19~")
        await asyncio.sleep(0.2)
        assert store.preset == "coding"
        pipe.send_text("\x1b1")
        await asyncio.sleep(0.2)
        assert store.focus == "composer"
        ui.app.exit()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(go())
    finally:
        teardown(pipe_context, app_context)
    frame = buffer.getvalue()
    assert "CODING" in frame
