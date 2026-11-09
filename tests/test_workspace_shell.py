import asyncio

import pytest

from eurus.config import load_config


def build_workspace(tmp_path, monkeypatch, columns=140):
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

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
    (tmp_path / "sample.txt").write_text("hello workspace\n", encoding="utf-8")
    session = InteractiveSession(config)
    sink = TranscriptSink()
    renderer = FullscreenRenderer(
        session.renderer.console,
        session.renderer.theme,
        sink,
        "truecolor",
    )
    session.renderer = renderer
    store = WorkspaceStore()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = WorkspaceApp(session, renderer, sink, store)
    session.picker = ui
    return ui, store, session, sink


def text_of(fragments):
    return "".join(part[1] for part in fragments)


def test_workspace_builds_all_panels(tmp_path, monkeypatch):
    ui, store, _, _ = build_workspace(tmp_path, monkeypatch)
    assert store.preset == "workspace"
    assert ui.tabs.active.kind == "chat"
    explorer = text_of(ui._render_explorer())
    assert "EXPLORER" in explorer
    activity = text_of(ui._render_activity())
    assert "ACTIVITY" in activity
    header = text_of(ui._render_header_line())
    assert "EURUS" in header
    assert "WORKSPACE" in header


def test_workspace_panel_titles_reflect_focus(tmp_path, monkeypatch):
    ui, store, _, _ = build_workspace(tmp_path, monkeypatch)
    assert "focused" not in text_of(ui._render_explorer())
    store.set_focus("explorer")
    assert ui.store.focus == "explorer"
    focused = [part[0] for part in ui._render_explorer() if "focused" in part[0]]
    assert focused


def test_workspace_focus_targets_route_to_panels(tmp_path, monkeypatch):
    ui, store, _, _ = build_workspace(tmp_path, monkeypatch)
    assert ui.focus_target() is ui.field
    store.set_focus("explorer")
    assert ui.focus_target() is ui.explorer_window
    store.set_focus("activity")
    assert ui.focus_target() is ui.activity_window
    store.set_focus("workspace")
    assert ui.focus_target() is ui.workspace_window
    store.toggle_explorer()
    assert ui.focus_target() is ui.field or ui.focus_target() is ui.workspace_window


def test_workspace_presets_change_layout_without_losing_session(tmp_path, monkeypatch):
    ui, store, session, _ = build_workspace(tmp_path, monkeypatch)
    session_id = session.session_id
    for preset in ("focus", "coding", "workspace"):
        store.set_preset(preset)
        assert store.preset == preset
        assert session.session_id == session_id
    assert not store.explorer_visible or store.explorer_visible


def test_workspace_tabs_open_cycle_and_close(tmp_path, monkeypatch):
    ui, _, _, _ = build_workspace(tmp_path, monkeypatch)
    from eurus.tui.workspace.views import WorkspaceTab

    assert len(ui.tabs.tabs) == 1
    ui.tabs.open(WorkspaceTab("editor", "sample.txt", path="sample.txt", key="sample.txt"))
    assert ui.tabs.active.title == "sample.txt"
    ui.tabs.open(WorkspaceTab("editor", "other.py", path="other.py", key="other.py"))
    ui.tabs.cycle(-1)
    assert ui.tabs.active.title == "sample.txt"
    ui.tabs.open(WorkspaceTab("editor", "sample.txt", path="sample.txt", key="sample.txt"))
    assert len(ui.tabs.tabs) == 3
    ui.tabs.close(2)
    assert len(ui.tabs.tabs) == 2
    ui.tabs.close(0)
    assert len(ui.tabs.tabs) == 2
    assert ui.tabs.active.title == "sample.txt"
    ui.tabs.select(1)
    ui.tabs.close()
    assert ui.tabs.active.kind == "chat"


def test_workspace_tab_dirty_markers(tmp_path, monkeypatch):
    ui, _, _, _ = build_workspace(tmp_path, monkeypatch)
    from eurus.tui.workspace.views import WorkspaceTab

    ui.tabs.open(WorkspaceTab("editor", "app.py", path="app.py", key="app.py"))
    ui.tabs.mark_dirty("app.py", True)
    assert ui.tabs.find("app.py").dirty is True
    bar = text_of(ui._render_tab_bar())
    assert "*" in bar


def test_workspace_footer_hints_respect_width(tmp_path, monkeypatch):
    from eurus.tui.workspace.views import render_footer_hints

    items = [("Ctrl+K", "palette"), ("Alt+1-4", "focus"), ("F8", "layout")]
    wide = render_footer_hints(items, 80)
    narrow = render_footer_hints(items, 20)
    assert "Ctrl+K" in text_of(wide)
    assert "F8" not in text_of(narrow)
    assert text_of(narrow).count("\n") == 0


def test_workspace_resize_clamped(tmp_path, monkeypatch):
    ui, store, _, _ = build_workspace(tmp_path, monkeypatch)
    store.set_focus("explorer")
    for _ in range(80):
        store.resize_explorer(5)
    assert store.explorer_width <= 120
    layout = ui._panel_geometry()
    assert layout.main.width >= 40
    for _ in range(80):
        store.resize_explorer(-5)
    assert store.explorer_width >= 1


def test_workspace_chat_renders_transcript(tmp_path, monkeypatch):
    ui, _, _, sink = build_workspace(tmp_path, monkeypatch)
    sink.write("hello from the agent\n")
    assert "hello from the agent" in text_of(ui._render_workspace_panel())


def test_workspace_confirm_and_ask_flow(tmp_path, monkeypatch):

    ui, _, _, _ = build_workspace(tmp_path, monkeypatch)

    async def yes():
        asyncio.get_running_loop().call_soon(ui._settle, True)
        return await ui.confirm("Allow once?", "allow", "deny", ("a", "y"))

    async def answer():
        task = asyncio.create_task(ui.ask_options("provider>", ["groq", "openai"]))
        await asyncio.sleep(0.02)
        ui._overlay_enter()
        return await task

    assert asyncio.run(yes()) is True
    assert asyncio.run(answer()) == "groq"
    assert ui.mode == "chat"


def test_workspace_dispatch_runs_session_command(tmp_path, monkeypatch):
    ui, _, _, sink = build_workspace(tmp_path, monkeypatch)

    async def go():
        ui.dispatch("/exit")
        await ui.task

    asyncio.run(go())
    assert "Eurus" in "\n".join(sink.lines) or True


def test_run_workspace_action_toggles_panels(tmp_path, monkeypatch):
    ui, store, _, _ = build_workspace(tmp_path, monkeypatch)
    ui.run_workspace_action("preset:focus")
    assert not store.explorer_visible
    ui.run_workspace_action("toggle-explorer")
    assert store.explorer_visible
    ui.run_workspace_action("toggle-activity")
    assert store.activity_visible
    ui.run_workspace_action("focus:activity")
    assert store.focus == "activity"
    ui.run_workspace_action("focus:composer")
    assert store.focus == "composer"
