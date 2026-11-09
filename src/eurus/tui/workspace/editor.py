from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from eurus.coding.paths import atomic_write_at, read_text_at
from eurus.errors import ToolError
from eurus.tui.workspace.editor_buffer import EditorBuffer
from eurus.tui.workspace.preview import LoadedFile, load_preview

CONFLICT_NONE = ""
CONFLICT_EXTERNAL = "external"
CONFLICT_MISSING = "missing"


@dataclass(slots=True)
class EditorSession:
    root: Path
    relative: str
    buffer: EditorBuffer = field(default_factory=EditorBuffer)
    original_text: str = ""
    original_hash: str = ""
    disk_hash: str = ""
    loaded_ok: bool = False
    readonly: bool = False
    readonly_reason: str = ""
    conflict: str = CONFLICT_NONE
    message: str = ""
    status: str = "open"
    scroll_offset: int = 0
    col_offset: int = 0
    pending_discard: bool = False

    @classmethod
    def open(cls, root: Path, relative: str) -> EditorSession:
        loaded: LoadedFile = load_preview(root, relative)
        session = cls(root=root, relative=relative)
        if loaded.error:
            session.readonly = True
            session.readonly_reason = loaded.error
            session.message = loaded.error
            session.buffer = EditorBuffer(lines=[""])
            return session
        if loaded.binary:
            session.readonly = True
            session.readonly_reason = "binary file"
            session.message = "binary file opened read-only"
            session.buffer = EditorBuffer(lines=[""])
            return session
        if loaded.too_large:
            session.readonly = True
            session.readonly_reason = "file larger than 1 MB"
            session.message = "file too large to edit; open the preview"
            session.buffer = EditorBuffer(lines=[""])
            return session
        session.buffer = EditorBuffer.from_text(loaded.content)
        session.original_text = loaded.content
        session.original_hash = hash_text(loaded.content)
        session.disk_hash = session.original_hash
        session.loaded_ok = True
        return session

    @property
    def title(self) -> str:
        return self.relative.rsplit("/", 1)[-1]

    def is_dirty(self) -> bool:
        return self.buffer.to_text() != self.original_text

    def check_external_change(self) -> bool:
        if self.readonly:
            return False
        try:
            current = read_text_at(self.root, self.relative)
        except ToolError as exc:
            self.conflict = CONFLICT_MISSING
            self.message = str(exc)
            return True
        digest = hash_text(current)
        changed = digest != self.disk_hash
        if changed:
            self.conflict = CONFLICT_EXTERNAL
            self.message = "file changed on disk while editing"
        return changed

    def save(self) -> bool:
        if self.readonly:
            self.message = "file is read-only"
            return False
        if self.conflict == CONFLICT_EXTERNAL:
            self.message = "resolve the conflict first: overwrite or reload"
            return False
        if self.conflict == CONFLICT_MISSING:
            self.message = "file disappeared; use overwrite to recreate"
            return False
        content = self.buffer.to_text()
        try:
            atomic_write_at(self.root, self.relative, content)
        except ToolError as exc:
            self.message = str(exc)
            return False
        self.original_text = content
        self.original_hash = hash_text(content)
        self.disk_hash = self.original_hash
        self.conflict = CONFLICT_NONE
        self.message = f"saved {self.relative}"
        self.status = "saved"
        return True

    def overwrite(self) -> bool:
        if self.readonly:
            self.message = "file is read-only"
            return False
        content = self.buffer.to_text()
        try:
            atomic_write_at(self.root, self.relative, content)
        except ToolError as exc:
            self.message = str(exc)
            return False
        self.original_text = content
        self.original_hash = hash_text(content)
        self.disk_hash = self.original_hash
        self.conflict = CONFLICT_NONE
        self.message = f"overwrote {self.relative}"
        return True

    def reload_from_disk(self) -> bool:
        try:
            current = read_text_at(self.root, self.relative)
        except ToolError as exc:
            self.message = str(exc)
            return False
        self.buffer = EditorBuffer.from_text(current)
        self.original_text = current
        self.original_hash = hash_text(current)
        self.disk_hash = self.original_hash
        self.conflict = CONFLICT_NONE
        self.message = f"reloaded {self.relative}"
        return True

    def discard(self) -> bool:
        if self.readonly:
            self.message = "nothing to discard"
            return False
        self.buffer = EditorBuffer.from_text(self.original_text)
        self.conflict = CONFLICT_NONE
        self.message = f"discarded changes to {self.relative}"
        return True


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
