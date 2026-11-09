from __future__ import annotations

from collections.abc import Callable, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from eurus.tui.commands import Command
from eurus.tui.workspace.fileindex import FileIndex


class WorkspaceCompleter(Completer):
    def __init__(
        self,
        commands: Iterable[Command],
        index_provider: Callable[[], FileIndex | None],
    ) -> None:
        self.commands = tuple(commands)
        self.index_provider = index_provider

    def get_completions(
        self, document: Document, complete_event: CompleteEvent | None
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        yield from self._slash_completions(text)
        yield from self._file_completions(text)

    def _slash_completions(self, text: str) -> Iterable[Completion]:
        if not text.startswith("/") or any(char.isspace() for char in text):
            return
        key = text.lower()
        for command in self.commands:
            if command.name.startswith(key):
                yield Completion(
                    command.name,
                    start_position=-len(text),
                    display_meta=command.summary,
                )

    def _file_completions(self, text: str) -> Iterable[Completion]:
        at = text.rfind("@")
        if at < 0:
            return
        fragment = text[at + 1 :]
        if "@" in fragment or any(char.isspace() for char in fragment):
            return
        index: FileIndex | None = self.index_provider()
        if index is None:
            return
        for relative in index.matches(fragment, limit=12):
            is_dir = False
            yield Completion(
                relative,
                start_position=-len(fragment),
                display_meta="dir" if is_dir else "file",
            )
