from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

FocusTarget = str
MAX_PANEL_WIDTH = 120
LAYOUT_PRESETS: tuple[str, ...] = ("focus", "workspace", "coding")
FOCUS_ORDER: tuple[FocusTarget, ...] = ("composer", "explorer", "workspace", "activity")

PRESET_PANELS: dict[str, dict[str, bool]] = {
    "focus": {"explorer": False, "activity": False},
    "workspace": {"explorer": True, "activity": True},
    "coding": {"explorer": True, "activity": True},
}


@dataclass(slots=True)
class WorkspaceStore:
    focus: FocusTarget = "composer"
    preset: str = "workspace"
    explorer_visible: bool = True
    activity_visible: bool = True
    explorer_width: int = 26
    activity_width: int = 32
    version: int = 0
    _listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def notify(self) -> None:
        self.version += 1
        for callback in tuple(self._listeners):
            callback()

    def set_focus(self, target: FocusTarget) -> None:
        if target not in FOCUS_ORDER or target == self.focus:
            return
        self.focus = target
        self.notify()

    def cycle_focus(self, delta: int = 1) -> FocusTarget:
        visible = [item for item in FOCUS_ORDER if self._target_visible(item)]
        if not visible:
            return self.focus
        current = visible.index(self.focus) if self.focus in visible else 0
        self.focus = visible[(current + delta) % len(visible)]
        self.notify()
        return self.focus

    def _target_visible(self, target: FocusTarget) -> bool:
        if target == "explorer":
            return self.explorer_visible
        if target == "activity":
            return self.activity_visible
        return True

    def set_preset(self, preset: str) -> None:
        if preset not in LAYOUT_PRESETS or preset == self.preset:
            return
        self.preset = preset
        panels = PRESET_PANELS[preset]
        self.explorer_visible = panels["explorer"]
        self.activity_visible = panels["activity"]
        self.notify()

    def cycle_preset(self) -> str:
        index = LAYOUT_PRESETS.index(self.preset) if self.preset in LAYOUT_PRESETS else 0
        self.set_preset(LAYOUT_PRESETS[(index + 1) % len(LAYOUT_PRESETS)])
        return self.preset

    def toggle_explorer(self) -> bool:
        self.explorer_visible = not self.explorer_visible
        if not self.explorer_visible and self.focus == "explorer":
            self.focus = "workspace"
        self.notify()
        return self.explorer_visible

    def toggle_activity(self) -> bool:
        self.activity_visible = not self.activity_visible
        if not self.activity_visible and self.focus == "activity":
            self.focus = "workspace"
        self.notify()
        return self.activity_visible

    def resize_explorer(self, delta: int) -> None:
        self.explorer_width = min(max(self.explorer_width + delta, 1), MAX_PANEL_WIDTH)
        self.notify()

    def resize_activity(self, delta: int) -> None:
        self.activity_width = min(max(self.activity_width + delta, 1), MAX_PANEL_WIDTH)
        self.notify()
