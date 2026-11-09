from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eurus.coding.paths import atomic_write_at, read_text_at
from eurus.errors import ToolError
from eurus.tui.workspace.diffmodel import DiffModel
from eurus.tui.workspace.editor import hash_text
from eurus.tui.workspace.editor_buffer import EditorBuffer

VIEW_UNIFIED = "unified"
VIEW_SIDE_BY_SIDE = "side"


@dataclass(slots=True)
class DiffSession:
    root: Path | None
    relative: str
    model: DiffModel
    view: str = VIEW_UNIFIED
    current_hunk: int = 0
    scroll_offset: int = 0
    column_offset: int = 0
    collapsed: bool = False
    message: str = ""
    source_kind: str = "buffer"
    buffer_hash: str = ""
    editor_key: str = ""
    applied: bool = False

    @classmethod
    def for_buffer(
        cls,
        editor_key: str,
        relative: str,
        old_text: str,
        new_text: str,
    ) -> DiffSession:
        return cls(
            root=None,
            relative=relative,
            model=DiffModel.build(old_text, new_text),
            source_kind="buffer",
            buffer_hash=hash_text(old_text),
            editor_key=editor_key,
        )

    @classmethod
    def for_file(cls, root: Path, relative: str, old_text: str, new_text: str) -> DiffSession:
        return cls(
            root=root,
            relative=relative,
            model=DiffModel.build(old_text, new_text),
            source_kind="file",
            buffer_hash=hash_text(old_text),
        )

    @property
    def title(self) -> str:
        return f"diff {self.relative.rsplit('/', 1)[-1]}"

    def toggle_view(self) -> None:
        self.view = VIEW_SIDE_BY_SIDE if self.view == VIEW_UNIFIED else VIEW_UNIFIED

    def toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        self.model.apply_context(0 if self.collapsed else 3)

    def next_hunk(self) -> int:
        if self.model.hunks:
            self.current_hunk = (self.current_hunk + 1) % len(self.model.hunks)
        return self.current_hunk

    def previous_hunk(self) -> int:
        if self.model.hunks:
            self.current_hunk = (self.current_hunk - 1) % len(self.model.hunks)
        return self.current_hunk

    def accept_current(self) -> None:
        self.model.accept_hunk(self.current_hunk)
        self.message = f"hunk {self.current_hunk + 1} accepted"

    def reject_current(self) -> None:
        self.model.reject_hunk(self.current_hunk)
        self.message = f"hunk {self.current_hunk + 1} rejected"

    def accept_every(self) -> None:
        self.model.accept_all()
        self.message = "all hunks accepted"

    def reject_every(self) -> None:
        self.model.reject_all()
        self.message = "all hunks rejected"

    def review_summary(self) -> str:
        model = self.model
        pending = model.pending_count()
        accepted = sum(1 for hunk in model.hunks if hunk.decision == "accepted")
        rejected = sum(1 for hunk in model.hunks if hunk.decision == "rejected")
        return (
            f"{model.stats.summary()} · {accepted} accepted · "
            f"{rejected} rejected · {pending} pending"
        )

    def apply_review_to_buffer(self, buffer: EditorBuffer) -> bool:
        if self.source_kind != "buffer":
            self.message = "this review targets a file, not a buffer"
            return False
        reviewed = self.model.reviewed_text()
        buffer.lines = reviewed.split("\n")
        buffer.cursor = (0, 0)
        buffer.undo_stack.clear()
        buffer.redo_stack.clear()
        buffer.modified += 1
        self.applied = True
        self.message = "review applied to the editor buffer"
        return True

    def apply_review_to_file(self) -> bool:
        if self.root is None:
            self.message = "this review has no file target"
            return False
        try:
            current = read_text_at(self.root, self.relative)
        except ToolError as exc:
            self.message = str(exc)
            return False
        if hash_text(current) != self.buffer_hash:
            self.message = "file changed on disk; reopen the diff before applying"
            return False
        reviewed = self.model.reviewed_text()
        try:
            atomic_write_at(self.root, self.relative, reviewed)
        except ToolError as exc:
            self.message = str(exc)
            return False
        self.applied = True
        self.buffer_hash = hash_text(reviewed)
        self.message = f"applied review to {self.relative}"
        return True
