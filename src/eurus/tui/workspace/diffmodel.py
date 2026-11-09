from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

DIFF_GUARD_CELLS = 2_000_000
DEFAULT_CONTEXT = 3
MIN_CONTEXT = 0
MAX_CONTEXT = 12


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: str
    old_number: int
    new_number: int
    text: str


@dataclass(slots=True)
class DiffHunk:
    index: int
    old_start: int
    old_lines: list[str]
    new_start: int
    new_lines: list[str]
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    decision: str = "pending"

    @property
    def added(self) -> int:
        return len(self.new_lines)

    @property
    def removed(self) -> int:
        return len(self.old_lines)

    def unified(self) -> list[DiffLine]:
        out: list[DiffLine] = []
        old_number = self.old_start - len(self.context_before)
        new_number = self.new_start - len(self.context_before)
        for line in self.context_before:
            out.append(DiffLine("context", old_number, new_number, line))
            old_number += 1
            new_number += 1
        for line in self.old_lines:
            out.append(DiffLine("removed", old_number, new_number, line))
            old_number += 1
        for line in self.new_lines:
            out.append(DiffLine("added", old_number, new_number, line))
            new_number += 1
        for line in self.context_after:
            out.append(DiffLine("context", old_number, new_number, line))
            old_number += 1
            new_number += 1
        return out

    def side_by_side(self) -> list[tuple[str, DiffLine, DiffLine]]:
        rows: list[tuple[str, DiffLine, DiffLine]] = []
        unified = self.unified()
        pending_old: list[DiffLine] = []
        pending_new: list[DiffLine] = []
        for line in unified:
            if line.kind == "removed":
                pending_old.append(line)
            elif line.kind == "added":
                pending_new.append(line)
            else:
                while pending_old or pending_new:
                    left = pending_old.pop(0) if pending_old else DiffLine("blank", 0, 0, "")
                    right = pending_new.pop(0) if pending_new else DiffLine("blank", 0, 0, "")
                    rows.append(("changed", left, right))
                rows.append(("context", line, line))
        while pending_old or pending_new:
            left = pending_old.pop(0) if pending_old else DiffLine("blank", 0, 0, "")
            right = pending_new.pop(0) if pending_new else DiffLine("blank", 0, 0, "")
            rows.append(("changed", left, right))
        return rows


@dataclass(slots=True)
class DiffStats:
    added: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.added + self.removed

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{self.added}")
        if self.removed:
            parts.append(f"-{self.removed}")
        return " ".join(parts) if parts else "no changes"


@dataclass(slots=True)
class DiffModel:
    old_text: str
    new_text: str
    hunks: list[DiffHunk] = field(default_factory=list)
    stats: DiffStats = field(default_factory=DiffStats)
    identical: bool = True

    @classmethod
    def build(cls, old_text: str, new_text: str) -> DiffModel:
        model = cls(old_text=old_text, new_text=new_text)
        old_lines = old_text.split("\n")
        new_lines = new_text.split("\n")
        matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        if matcher.quick_ratio() == 1.0 and old_lines == new_lines:
            model.identical = True
            return model
        model.identical = False
        changed_blocks = [
            (tag, i1, i2, j1, j2) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
        ]
        for stats_index, (_, i1, i2, j1, j2) in enumerate(changed_blocks):
            hunk = DiffHunk(
                index=stats_index,
                old_start=i1 + 1,
                old_lines=old_lines[i1:i2],
                new_start=j1 + 1,
                new_lines=new_lines[j1:j2],
            )
            model.hunks.append(hunk)
        model.stats.added = sum(len(h.new_lines) for h in model.hunks)
        model.stats.removed = sum(len(h.old_lines) for h in model.hunks)
        model.stats.unchanged = max(len(old_lines) - model.stats.removed, 0)
        model.apply_context(DEFAULT_CONTEXT)
        return model

    def apply_context(self, context: int) -> None:
        context = min(max(context, MIN_CONTEXT), MAX_CONTEXT)
        old_lines = self.old_text.split("\n")
        for hunk in self.hunks:
            before = context
            after = context
            for other in self.hunks:
                if other is hunk:
                    continue
                gap_after = other.old_start - (hunk.old_start + len(hunk.old_lines))
                if gap_after >= 0:
                    after = min(after, gap_after)
                gap_before = hunk.old_start - (other.old_start + len(other.old_lines))
                if gap_before >= 0:
                    before = min(before, gap_before)
            hunk.context_before = old_lines[
                max(hunk.old_start - 1 - before, 0) : hunk.old_start - 1
            ]
            old_end = hunk.old_start - 1 + len(hunk.old_lines)
            hunk.context_after = old_lines[old_end : old_end + after]

    def hunk_at(self, position: int) -> DiffHunk | None:
        if 0 <= position < len(self.hunks):
            return self.hunks[position]
        return None

    def decide(self, hunk_index: int, decision: str) -> None:
        hunk = self.hunk_at(hunk_index)
        if hunk is None or decision not in {"accepted", "rejected", "pending"}:
            return
        hunk.decision = decision

    def accept_hunk(self, hunk_index: int) -> None:
        self.decide(hunk_index, "accepted")

    def reject_hunk(self, hunk_index: int) -> None:
        self.decide(hunk_index, "rejected")

    def accept_all(self) -> None:
        for hunk in self.hunks:
            hunk.decision = "accepted"

    def reject_all(self) -> None:
        for hunk in self.hunks:
            hunk.decision = "rejected"

    def reset_decisions(self) -> None:
        for hunk in self.hunks:
            hunk.decision = "pending"

    def pending_count(self) -> int:
        return sum(1 for hunk in self.hunks if hunk.decision == "pending")

    def reviewed_text(self) -> str:
        old_lines = self.old_text.split("\n")
        matcher = SequenceMatcher(None, old_lines, self.new_text.split("\n"), autojunk=False)
        result: list[str] = []
        cursor = 0
        blocks = list(matcher.get_opcodes())
        hunk_index = 0
        for tag, i1, i2, j1, j2 in blocks:
            if tag == "equal":
                result.extend(old_lines[i1:i2])
                cursor = i2
                continue
            hunk = self.hunk_at(hunk_index)
            hunk_index += 1
            if hunk is None:
                continue
            if i1 > cursor:
                result.extend(old_lines[cursor:i1])
            if hunk.decision != "rejected":
                result.extend(self.new_text.split("\n")[j1:j2])
            else:
                result.extend(old_lines[i1:i2])
            cursor = i2
        if cursor < len(old_lines):
            result.extend(old_lines[cursor:])
        return "\n".join(result)
