from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from eurus.agent.events import AgentEvent
from eurus.security.redaction import summarize_arguments

MAX_ITEMS = 400
DETAIL_CLIP = 400

FILE_READ_TOOLS = frozenset({"read_file", "list_files", "search_code", "grep_search"})
FILE_WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "apply_patch", "create_file", "propose_patch"}
)

STATUS_GLYPHS = {
    "running": "●",
    "ok": "✓",
    "error": "✗",
    "pending": "○",
    "allowed": "✓",
    "denied": "✗",
    "info": "·",
    "thinking": "◈",
    "done": "✓",
}


class ActivitySink(Protocol):
    def on_event(self, event: AgentEvent) -> None: ...

    def on_note(self, kind: str, title: str, detail: str = "") -> None: ...


@dataclass(slots=True)
class ActivityItem:
    kind: str
    title: str
    detail: str = ""
    status: str = "info"
    started: float = 0.0
    duration: float = 0.0
    expanded: bool = False
    depth: int = 0
    tool_call_id: str = ""
    path: str = ""
    usage_text: str = ""
    order: int = 0

    def glyph(self) -> str:
        return STATUS_GLYPHS.get(self.status, "·")


@dataclass(slots=True)
class ActivityFeed:
    on_change: Callable[[], None] | None = None
    items: list[ActivityItem] = field(default_factory=list)
    selected: int = 0
    offset: int = 0
    follow: bool = True
    _order: int = 0

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def _append(self, item: ActivityItem) -> None:
        self.items.append(item)
        if len(self.items) > MAX_ITEMS:
            del self.items[:-MAX_ITEMS]
            self.selected = min(self.selected, len(self.items) - 1)

    def on_event(self, event: AgentEvent) -> None:
        handler = {
            "step": self._on_step,
            "tool_call": self._on_tool_call,
            "tool_result": self._on_tool_result,
            "done": self._on_done,
            "lesson": self._on_lesson,
            "skill": self._on_skill,
            "plan": self._on_plan,
            "recovery": self._on_recovery,
        }.get(event.kind)
        if handler is not None:
            handler(event)
            self._notify()

    def on_note(self, kind: str, title: str, detail: str = "") -> None:
        status = {
            "approval": "pending",
            "approval_result": "allowed",
            "denied": "denied",
            "error": "error",
            "info": "info",
        }.get(kind, "info")
        if kind == "denied":
            status = "denied"
        self._append(
            ActivityItem(
                kind=kind,
                title=title,
                detail=detail[:DETAIL_CLIP],
                status=status,
                started=time.monotonic(),
                order=self._next_order(),
            )
        )
        self._notify()

    def _drop_thinking(self) -> None:
        for item in reversed(self.items):
            if item.kind == "thinking" and item.status == "running":
                item.duration = time.monotonic() - item.started
                item.status = "ok"
                return

    def _on_step(self, event: AgentEvent) -> None:
        for item in reversed(self.items):
            if item.kind == "thinking" and item.status == "running":
                return
        self._append(
            ActivityItem(
                kind="thinking",
                title=f"thinking · step {event.step}",
                status="running",
                started=time.monotonic(),
                order=self._next_order(),
            )
        )

    def _on_tool_call(self, event: AgentEvent) -> None:
        call = event.tool_call
        if call is None:
            return
        self._drop_thinking()
        kind = "tool"
        path = ""
        if call.name in FILE_READ_TOOLS:
            kind = "file_read"
        elif call.name in FILE_WRITE_TOOLS:
            kind = "file_edit"
        title = call.name
        if call.name in {"read_file", "write_file", "edit_file", "apply_patch", "create_file"}:
            path = str(call.arguments.get("path", ""))
            if path:
                title = f"{call.name} {path}"
        elif call.name == "propose_patch":
            path = str(call.arguments.get("path", ""))
            title = f"propose patch {path}" if path else "propose patch"
        else:
            summary = summarize_arguments(call.arguments)
            if summary:
                title = f"{call.name} {summary[:120]}"
        self._append(
            ActivityItem(
                kind=kind,
                title=title,
                detail=summarize_arguments(call.arguments)[:DETAIL_CLIP],
                status="running",
                started=time.monotonic(),
                tool_call_id=call.id,
                path=path,
                order=self._next_order(),
            )
        )

    def _on_tool_result(self, event: AgentEvent) -> None:
        result = event.tool_result
        if result is None:
            return
        for item in reversed(self.items):
            if item.tool_call_id and item.tool_call_id == result.call_id:
                item.status = "error" if result.is_error else "ok"
                item.duration = time.monotonic() - item.started
                item.detail = (result.output or "")[:DETAIL_CLIP]
                return
        self._append(
            ActivityItem(
                kind="tool",
                title=result.name,
                detail=(result.output or "")[:DETAIL_CLIP],
                status="error" if result.is_error else "ok",
                started=time.monotonic(),
                duration=0.0,
                tool_call_id=result.call_id,
                order=self._next_order(),
            )
        )

    def _on_done(self, event: AgentEvent) -> None:
        self._drop_thinking()
        usage = event.usage
        usage_text = ""
        if usage is not None and usage.total_tokens:
            usage_text = f"{usage.total_tokens} tokens"
        stop = event.stop_reason or "completed"
        self._append(
            ActivityItem(
                kind="done",
                title=f"turn finished · {stop}",
                detail=event.text[:DETAIL_CLIP],
                status="done",
                started=time.monotonic(),
                usage_text=usage_text,
                order=self._next_order(),
            )
        )

    def _on_lesson(self, event: AgentEvent) -> None:
        if event.text:
            self._append(
                ActivityItem(
                    kind="info",
                    title="lesson saved",
                    detail=event.text[:DETAIL_CLIP],
                    status="info",
                    order=self._next_order(),
                )
            )

    def _on_skill(self, event: AgentEvent) -> None:
        if event.text:
            self._append(
                ActivityItem(
                    kind="info",
                    title="skill drafted",
                    detail=event.text[:DETAIL_CLIP],
                    status="info",
                    order=self._next_order(),
                )
            )

    def _on_plan(self, event: AgentEvent) -> None:
        if event.text:
            self._append(
                ActivityItem(
                    kind="info",
                    title=f"plan updated · step {event.step}",
                    detail=event.text[:DETAIL_CLIP],
                    status="info",
                    order=self._next_order(),
                )
            )

    def _on_recovery(self, event: AgentEvent) -> None:
        if event.text:
            self._append(
                ActivityItem(
                    kind="info",
                    title=f"recovery · step {event.step}",
                    detail=event.text[:DETAIL_CLIP],
                    status="error",
                    order=self._next_order(),
                )
            )

    def note_sub_agent(self, title: str, detail: str = "") -> None:
        self._append(
            ActivityItem(
                kind="sub_agent",
                title=title,
                detail=detail[:DETAIL_CLIP],
                status="running",
                started=time.monotonic(),
                depth=1,
                order=self._next_order(),
            )
        )
        self._notify()

    def running_count(self) -> int:
        return sum(1 for item in self.items if item.status == "running")

    def move(self, delta: int) -> None:
        if not self.items:
            return
        if self.follow:
            self.selected = len(self.items) - 1
        self.selected = min(max(self.selected + delta, 0), len(self.items) - 1)
        self.follow = self.selected >= len(self.items) - 1
        self._notify()

    def toggle_expand(self) -> None:
        if 0 <= self.selected < len(self.items):
            item = self.items[self.selected]
            item.expanded = not item.expanded
            self._notify()

    def visible_rows(self, height: int) -> list[tuple[ActivityItem, int]]:
        rows: list[tuple[ActivityItem, int]] = []
        for index, item in enumerate(self.items):
            rows.append((item, index))
            if item.expanded and item.detail:
                for _ in range(_detail_line_count(item)):
                    rows.append((item, -1))
        if self.follow:
            self.selected = max(len(self.items) - 1, 0)
        start = self.offset
        if self.selected < start:
            start = self.selected
        end = start + max(height, 1)
        if len(rows) > end:
            start = max(len(rows) - max(height, 1), 0) if self.follow else start
        self.offset = max(start, 0)
        return rows[self.offset : self.offset + max(height, 1)]

    def clear(self) -> None:
        self.items.clear()
        self.selected = 0
        self.offset = 0
        self.follow = True
        self._notify()


def _detail_line_count(item: ActivityItem) -> int:
    lines = item.detail.split("\n")
    return min(len(lines), 8)
