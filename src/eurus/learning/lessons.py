from __future__ import annotations

import re
from pathlib import Path

from eurus.memory.manager import MemoryManager

CATEGORY = "lesson"
MAX_QUERY_TERMS = 12
GLOBAL_SCOPE = "global"
STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "your",
        "have",
        "will",
        "can",
        "not",
        "are",
        "was",
        "you",
        "any",
        "all",
        "yang",
        "dan",
        "untuk",
        "dengan",
        "dari",
        "pada",
        "ini",
        "itu",
        "atau",
        "ada",
        "tidak",
        "akan",
        "bisa",
        "saya",
        "kamu",
        "buat",
    ]
)


def _terms(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for word in re.findall(r"\w+", text.lower()):
        if len(word) >= 3 and word not in STOPWORDS and word.isalnum():
            seen.setdefault(word)
    return list(seen)


def _similarity(left: str, right: str) -> float:
    a, b = set(re.findall(r"\w+", left.lower())), set(re.findall(r"\w+", right.lower()))
    return len(a & b) / len(a | b) if a and b else 0.0


def lesson_scope(owner_id: str | None) -> str:
    if not owner_id or not owner_id.strip():
        return GLOBAL_SCOPE
    return f"owner:{owner_id.strip()}"


class LessonStore:
    def __init__(self, database: Path, *, limit: int = 5, max_lessons: int = 500) -> None:
        self.database = database
        self.limit = limit
        self.max_lessons = max_lessons
        self._memory: MemoryManager | None = None

    def _manager(self) -> MemoryManager:
        if self._memory is None:
            self._memory = MemoryManager(self.database)
        return self._memory

    def relevant(self, task: str, *, owner_id: str | None = None) -> list[str]:
        terms = _terms(task)[:MAX_QUERY_TERMS]
        if self.limit < 1 or not terms or not self.database.exists():
            return []
        rows = self._manager().store.search_memories_any(
            terms, self.limit, category=CATEGORY, scope=lesson_scope(owner_id)
        )
        return [row["content"] for row in rows]

    def all(self, *, owner_id: str | None = None) -> list[str]:
        if not self.database.exists():
            return []
        rows = self._manager().store.list_memories(
            category=CATEGORY, limit=self.max_lessons, scope=lesson_scope(owner_id)
        )
        return [row["content"] for row in rows]

    def is_duplicate(self, lesson: str, *, owner_id: str | None = None) -> bool:
        return any(_similarity(lesson, known) >= 0.7 for known in self.all(owner_id=owner_id))

    def add(self, lesson: str, *, owner_id: str | None = None, shared: bool = False) -> int | None:
        if shared and not owner_id:
            raise ValueError("shared lessons require an owning owner identity")
        if self.is_duplicate(lesson, owner_id=owner_id):
            return None
        if len(self.all(owner_id=owner_id)) >= self.max_lessons:
            return None
        scope = GLOBAL_SCOPE if shared or not owner_id else lesson_scope(owner_id)
        return self._manager().store.add_memory(
            lesson,
            tags="auto",
            category=CATEGORY,
            memory_type="lesson",
            source="reflector",
            confidence=0.75,
            scope=scope,
        )

    def remove(self, memory_id: int, *, owner_id: str | None = None) -> bool:
        if not self.database.exists():
            return False
        if owner_id is not None and not self._visible(memory_id, owner_id):
            return False
        return self._manager().forget(memory_id)

    def _visible(self, memory_id: int, owner_id: str) -> bool:
        row = self._manager().store.get_memory(memory_id)
        if row is None:
            return False
        scope = str(row.get("scope", ""))
        return scope in (GLOBAL_SCOPE, lesson_scope(owner_id))
