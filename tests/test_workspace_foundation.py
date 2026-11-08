import pytest

from eurus.tui.workspace import workspace_mode, workspace_setting
from eurus.tui.workspace.geometry import (
    MIN_ACTIVITY_WIDTH,
    MIN_EXPLORER_WIDTH,
    MIN_MAIN_WIDTH,
    PanelFrame,
    compute_layout,
    workspace_supported,
)
from eurus.tui.workspace.store import WorkspaceStore


def test_store_notifies_subscribers_on_every_mutation():
    store = WorkspaceStore()
    seen = []
    unsubscribe = store.subscribe(lambda: seen.append(store.version))
    store.set_focus("explorer")
    store.toggle_explorer()
    store.toggle_activity()
    store.resize_explorer(2)
    store.set_preset("coding")
    unsubscribe()
    store.set_focus("composer")
    assert len(seen) == 5
    assert store.version == 6


def test_store_ignores_invalid_focus_and_preset():
    store = WorkspaceStore()
    store.set_focus("nonsense")
    store.set_preset("nonsense")
    assert store.focus == "composer"
    assert store.preset == "workspace"


def test_store_cycle_focus_skips_hidden_panels():
    store = WorkspaceStore()
    store.toggle_explorer()
    store.toggle_activity()
    assert store.cycle_focus(1) == "workspace"
    assert store.cycle_focus(1) == "composer"


def test_store_focus_leaves_hidden_panel_on_toggle():
    store = WorkspaceStore()
    store.set_focus("explorer")
    store.toggle_explorer()
    assert store.focus == "workspace"


def test_store_presets_drive_visibility():
    store = WorkspaceStore()
    store.set_preset("focus")
    assert not store.explorer_visible and not store.activity_visible
    store.set_preset("coding")
    assert store.explorer_visible and store.activity_visible
    assert store.cycle_preset() == "focus"
    assert store.preset == "focus"


def test_geometry_full_workspace_has_no_overlap():
    store = WorkspaceStore()
    layout = compute_layout(160, 40, store)
    assert layout.explorer is not None
    assert layout.activity is not None
    panels = layout.panels
    for index, first in enumerate(panels):
        for second in panels[index + 1 :]:
            assert not frames_overlap(first, second)
    assert layout.main.width >= MIN_MAIN_WIDTH
    assert layout.explorer.width == store.explorer_width
    assert layout.activity.width == store.activity_width


def frames_overlap(first: PanelFrame, second: PanelFrame) -> bool:
    return (
        first.left < second.left + second.width
        and second.left < first.left + first.width
        and first.top < second.top + second.height
        and second.top < first.top + first.height
    )


def test_geometry_hidden_panels_give_main_full_width():
    store = WorkspaceStore()
    store.toggle_explorer()
    store.toggle_activity()
    layout = compute_layout(120, 40, store)
    assert layout.explorer is None and layout.activity is None
    assert layout.main.width == 120
    assert layout.main.left == 0


def test_geometry_clamps_oversized_panels():
    store = WorkspaceStore(explorer_width=200, activity_width=200)
    layout = compute_layout(160, 40, store)
    assert layout.explorer is not None
    assert layout.activity is not None
    assert layout.main.width >= MIN_MAIN_WIDTH
    for index, first in enumerate(layout.panels):
        for second in layout.panels[index + 1 :]:
            assert not frames_overlap(first, second)


def test_geometry_narrow_terminal_hides_side_panels():
    store = WorkspaceStore()
    layout = compute_layout(90, 30, store)
    assert layout.explorer is None or layout.explorer.width >= MIN_EXPLORER_WIDTH
    if layout.activity is not None:
        assert layout.activity.width >= MIN_ACTIVITY_WIDTH
    assert layout.main.width >= 1


def test_geometry_minimum_widths_are_respected():
    store = WorkspaceStore(explorer_width=2, activity_width=2)
    layout = compute_layout(160, 40, store)
    assert layout.explorer is not None and layout.explorer.width >= MIN_EXPLORER_WIDTH
    assert layout.activity is not None and layout.activity.width >= MIN_ACTIVITY_WIDTH


def test_geometry_splitter_positions_between_panels():
    store = WorkspaceStore()
    layout = compute_layout(140, 40, store)
    assert layout.explorer_splitter == layout.explorer.left + layout.explorer.width
    assert layout.activity_splitter == layout.activity.left - 1


def test_workspace_supported_threshold():
    assert workspace_supported(100) is True
    assert workspace_supported(99) is False


def test_workspace_mode_env_matrix():
    assert workspace_setting({"EURUS_WORKSPACE": "off"}) == "off"
    assert workspace_setting({"EURUS_WORKSPACE": "ON"}) == "on"
    assert workspace_setting({"EURUS_WORKSPACE": "weird"}) == "auto"
    assert workspace_setting({}) == "auto"
    assert workspace_mode({"EURUS_WORKSPACE": "off"}, columns=200, tty=True) is False
    assert workspace_mode({"EURUS_WORKSPACE": "on"}, columns=40, tty=False) is True
    assert workspace_mode({}, columns=140, tty=True) is True
    assert workspace_mode({}, columns=80, tty=True) is False
    assert workspace_mode({}, columns=140, tty=False) is False


@pytest.mark.parametrize("columns", [100, 110, 128, 140, 180, 220])
@pytest.mark.parametrize("rows", [24, 30, 50])
def test_geometry_never_overlaps_at_common_sizes(columns, rows):
    store = WorkspaceStore()
    for preset in ("focus", "workspace", "coding"):
        store.set_preset(preset)
        layout = compute_layout(columns, rows, store)
        panels = layout.panels
        assert panels
        for index, first in enumerate(panels):
            for second in panels[index + 1 :]:
                assert not frames_overlap(first, second)
        total = sum(frame.width for frame in panels)
        assert total <= columns
