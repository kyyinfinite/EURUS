from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus.memory.embeddings import EmbeddingProvider
from eurus.memory.lexical import cosine_similarity, lexical_vector
from eurus.security.redaction import (
    EMAIL_PATTERN,
    SENSITIVE_ASSIGNMENT_PATTERN,
    contains_secret,
)
from eurus.sessions import DEFAULT_MEMORY_OWNER, SessionStore

MEMORY_CLASSES: dict[str, str] = {
    "fact": "semantic",
    "profile": "semantic",
    "preference": "semantic",
    "project_fact": "semantic",
    "agent_note": "semantic",
    "episode": "episodic",
    "lesson": "procedural",
    "procedural": "procedural",
}
MEMORY_CLASS_NAMES = frozenset({"working", "episodic", "semantic", "procedural"})

_SENSITIVE_TERMS = re.compile(
    r"(?i)\b(?:diagnosed with|my medical|my health condition|my religion|my sexual|"
    r"my political affiliation|social security number|credit card number|"
    r"didiagnosis dengan|kondisi medis saya|kondisi medisku|penyakit saya|penyakitku|"
    r"diagnosis saya|agama saya|agamaku|seksualitas saya|seksualitasku|"
    r"orientasi seksual saya|afiliasi politik saya|pandangan politik saya|"
    r"nomor kartu kredit|kartu kredit saya|kartu kreditku|nomor ktp|ktp saya|"
    r"nomor induk kependudukan|nik saya)\b"
)
_CANDIDATE_PATTERNS = (
    (re.compile(r"(?i)\bremember that\s+(.+?)[.!?]*$"), "fact"),
    (re.compile(r"(?i)\b(?:i prefer|my preference is|please always)\s+(.+?)[.!?]*$"), "preference"),
    (
        re.compile(
            r"(?i)\b(?:for this project,?|we decided that|project decision:)\s+(.+?)[.!?]*$"
        ),
        "project_fact",
    ),
    (re.compile(r"(?i)\bmy name is\s+(.+?)[.!?]*$"), "profile"),
    (
        re.compile(
            r"(?i)\b(?:validated workflow|workflow to reuse|the reliable steps are)"
            r"\s*:?\s*(.+?)[.!?]*$"
        ),
        "procedural",
    ),
    (
        re.compile(
            r"(?i)\b(?:for future reference|the outcome of this task is|completed task summary)"
            r"\s*:?\s*(.+?)[.!?]*$"
        ),
        "episode",
    ),
    (re.compile(r"(?i)\b(?:ingat bahwa|catat bahwa)\s+(.+?)[.!?]*$"), "fact"),
    (
        re.compile(
            r"(?i)\b(?:aku lebih suka|saya lebih suka|aku lebih memilih|"
            r"saya lebih memilih|tolong selalu)\s+(.+?)[.!?]*$"
        ),
        "preference",
    ),
    (
        re.compile(
            r"(?i)\b(?:untuk proyek ini,?|keputusan proyek\s*:|kami memutuskan bahwa)"
            r"\s+(.+?)[.!?]*$"
        ),
        "project_fact",
    ),
    (re.compile(r"(?i)\b(?:namaku|nama saya)\s+(.+?)[.!?]*$"), "profile"),
    (
        re.compile(
            r"(?i)\b(?:alur kerja yang terbukti|prosedur yang bisa dipakai ulang|"
            r"langkah yang terbukti)\s*:?\s*(.+?)[.!?]*$"
        ),
        "procedural",
    ),
    (
        re.compile(
            r"(?i)\b(?:untuk referensi ke depan|hasil tugas ini adalah|"
            r"ringkasan tugas selesai)\s*:?\s*(.+?)[.!?]*$"
        ),
        "episode",
    ),
)


class MemoryProvider(ABC):
    @abstractmethod
    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        scope: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def remember(
        self,
        content: str,
        *,
        category: str = "general",
        tags: str = "",
        scope: str = "global",
        provenance: dict[str, Any] | None = None,
        expires_at: str = "",
        trust: str = "derived",
        owner_id: str = DEFAULT_MEMORY_OWNER,
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    def forget(self, memory_id: int) -> bool:
        raise NotImplementedError


class SQLiteMemoryProvider(MemoryProvider):
    def __init__(self, database: Path) -> None:
        self.store = SessionStore(database)

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        scope: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.search_memories(
            query,
            limit=min(max(limit, 1), 100),
            scope=scope,
            owner_id=owner_id,
        )

    def remember(
        self,
        content: str,
        *,
        category: str = "general",
        tags: str = "",
        scope: str = "global",
        provenance: dict[str, Any] | None = None,
        expires_at: str = "",
        trust: str = "derived",
        owner_id: str = DEFAULT_MEMORY_OWNER,
    ) -> int:
        return self.store.add_memory(
            content,
            tags,
            category=category,
            scope=scope,
            provenance=provenance,
            expires_at=expires_at,
            trust=trust,
            owner_id=owner_id,
        )

    def forget(self, memory_id: int) -> bool:
        return self.store.delete_memory(memory_id)


class HybridMemoryProvider(MemoryProvider):
    def __init__(
        self,
        database: Path,
        *,
        candidate_pool: int = 400,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.store = SessionStore(database)
        self.candidate_pool = candidate_pool
        self.embedding = embedding

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        scope: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        bounded = min(max(limit, 1), 100)
        keyword_rows = self.store.search_memories(
            query, bounded * 2, scope=scope, owner_id=owner_id
        )
        pool = self.store.list_memories(limit=self.candidate_pool, scope=scope, owner_id=owner_id)
        known_ids = {int(row["id"]) for row in keyword_rows}
        scored: list[tuple[float, dict[str, Any]]] = [(1.0, dict(row)) for row in keyword_rows]
        query_vector = lexical_vector(query)
        semantic_scores = self._semantic_scores(query, pool)

        for row in pool:
            if int(row["id"]) in known_ids:
                continue
            content = str(row.get("content", ""))
            if not content:
                continue
            score = cosine_similarity(query_vector, lexical_vector(content))
            semantic = semantic_scores.get(int(row["id"]))
            if semantic is not None:
                score = max(score, semantic)
            if score > 0.05:
                scored.append((score, dict(row)))
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[dict[str, Any]] = []
        for score, row in scored[:bounded]:
            row["retrieval_score"] = round(float(score), 4)
            results.append(row)
        return results

    def _semantic_scores(self, query: str, pool: list[dict[str, Any]]) -> dict[int, float]:
        if self.embedding is None or not self.embedding.available or not pool:
            return {}
        try:
            vectors = self.embedding.embed([query, *[str(row.get("content", "")) for row in pool]])
        except Exception:
            logging.getLogger(__name__).debug("embedding provider failed", exc_info=True)
            return {}
        if len(vectors) != len(pool) + 1:
            return {}
        query_vector = vectors[0]
        scores: dict[int, float] = {}
        for row, vector in zip(pool, vectors[1:], strict=True):
            similarity = self.embedding.similarity(query_vector, vector)
            if similarity > 0.05:
                scores[int(row["id"])] = float(similarity)
        return scores

    def remember(
        self,
        content: str,
        *,
        category: str = "general",
        tags: str = "",
        scope: str = "global",
        provenance: dict[str, Any] | None = None,
        expires_at: str = "",
        trust: str = "derived",
        owner_id: str = DEFAULT_MEMORY_OWNER,
    ) -> int:
        return self.store.add_memory(
            content,
            tags,
            category=category,
            scope=scope,
            provenance=provenance,
            expires_at=expires_at,
            trust=trust,
            owner_id=owner_id,
        )

    def forget(self, memory_id: int) -> bool:
        return self.store.delete_memory(memory_id)


def build_memory_provider(
    name: str,
    database: Path,
    *,
    embedding: EmbeddingProvider | None = None,
) -> MemoryProvider:
    normalized = name.strip().lower()
    if normalized == "sqlite":
        return SQLiteMemoryProvider(database)
    if normalized == "hybrid":
        return HybridMemoryProvider(database, embedding=embedding)
    raise ValueError(f"unsupported memory provider: {name}")


class MemoryManager:
    def __init__(
        self,
        database: Path,
        *,
        max_memories: int = 2000,
        retention_days: int = 0,
        max_content_chars: int = 500,
        owner_id: str = DEFAULT_MEMORY_OWNER,
        provider: MemoryProvider | None = None,
    ) -> None:
        if not 10 <= max_memories <= 100_000:
            raise ValueError("max_memories must be between 10 and 100000")
        if not 0 <= retention_days <= 36500:
            raise ValueError("retention_days must be between 0 and 36500")
        if not 100 <= max_content_chars <= 5000:
            raise ValueError("max_content_chars must be between 100 and 5000")
        owner = owner_id.strip() or DEFAULT_MEMORY_OWNER
        if len(owner) > 200:
            raise ValueError("owner_id exceeds 200 characters")
        self.store = SessionStore(database)
        self.provider: MemoryProvider = provider or SQLiteMemoryProvider(database)
        self.max_memories = max_memories
        self.retention_days = retention_days
        self.max_content_chars = max_content_chars
        self.owner_id = owner

    def remember(
        self,
        content: str,
        *,
        category: str = "general",
        tags: str = "",
        scope: str = "global",
        trust: str = "user_approved",
    ) -> int:
        self._validate_content(content)
        if not scope.strip() or len(scope) > 500:
            raise ValueError("memory scope must be non-empty and at most 500 characters")
        if trust not in {"user_approved", "derived"}:
            raise ValueError(
                "direct writes may only be user_approved or derived; use "
                "remember_untrusted for quarantined external content"
            )
        expires_at = (
            (datetime.now(UTC) + timedelta(days=self.retention_days)).isoformat()
            if self.retention_days
            else ""
        )
        memory_id = self.provider.remember(
            content,
            category=category,
            tags=tags,
            scope=scope,
            provenance={
                "source": "explicit_memory_write",
                "trust": trust,
                "recorded_at": datetime.now(UTC).isoformat(),
                "owner_id": self.owner_id,
            },
            expires_at=expires_at,
            trust=trust,
            owner_id=self.owner_id,
        )
        self.enforce_budget(scope=scope)
        return memory_id

    def remember_untrusted(
        self,
        content: str,
        *,
        provenance: dict[str, Any] | None = None,
        scope: str = "global",
        category: str = "general",
    ) -> int:
        self._validate_content(content)
        memory_id = self.provider.remember(
            content,
            category=category,
            tags="quarantined",
            scope=scope,
            provenance={
                **(provenance or {}),
                "source": "untrusted_external_content",
                "recorded_at": datetime.now(UTC).isoformat(),
                "owner_id": self.owner_id,
            },
            expires_at="",
            trust="untrusted",
            owner_id=self.owner_id,
        )
        self.store.record_event(
            "memory_quarantined",
            metadata={"memory_id": memory_id, "scope": scope, "owner_id": self.owner_id},
        )
        return memory_id

    def promote(self, memory_id: int, *, authority: str) -> bool:
        if authority != "user":
            raise PermissionError("only an explicit user action can promote quarantined memory")
        row = self.store.get_memory(memory_id)
        if row is None:
            return False
        if str(row.get("trust", "derived")) != "untrusted":
            return False
        promoted = self.store.set_memory_trust(memory_id, "user_approved")
        if promoted:
            self.store.record_event(
                "memory_promoted",
                metadata={"memory_id": memory_id, "authority": authority},
            )
        return promoted

    def quarantined(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list_memories(
            limit=1000, scope=scope, include_untrusted=True, owner_id=self.owner_id
        )
        return [row for row in rows if str(row.get("trust", "")) == "untrusted"]

    def recall(
        self, query: str, *, limit: int = 5, scope: str | None = None
    ) -> list[dict[str, Any]]:
        return self.provider.recall(query, limit=limit, scope=scope, owner_id=self.owner_id)

    def forget(self, memory_id: int) -> bool:
        return self.provider.forget(memory_id)

    def update(
        self,
        memory_id: int,
        content: str,
        *,
        category: str | None = None,
        tags: str | None = None,
    ) -> bool:
        self._validate_content(content)
        return self.store.update_memory(memory_id, content, category=category, tags=tags)

    @staticmethod
    def _validate_content(content: str) -> None:
        if not content or len(content) > 5000:
            raise ValueError("memory content must be between 1 and 5000 characters")
        if (
            _SENSITIVE_TERMS.search(content)
            or EMAIL_PATTERN.search(content)
            or SENSITIVE_ASSIGNMENT_PATTERN.search(content)
            or contains_secret(content)
        ):
            raise ValueError("memory content rejected by privacy policy")

    def extract_candidates(self, user_text: str) -> list[dict[str, Any]]:
        text = " ".join(user_text.split())
        if not text or len(text) > 8_000:
            return []
        if (
            _SENSITIVE_TERMS.search(text)
            or EMAIL_PATTERN.search(text)
            or SENSITIVE_ASSIGNMENT_PATTERN.search(text)
            or contains_secret(text)
        ):
            return []
        candidates: list[dict[str, Any]] = []
        for pattern, memory_type in _CANDIDATE_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            content = match.group(1).strip(" \t\r\n.,;:!?\"'")
            if len(content) < 4 or len(content) > self.max_content_chars:
                continue
            if (
                _SENSITIVE_TERMS.search(content)
                or EMAIL_PATTERN.search(content)
                or SENSITIVE_ASSIGNMENT_PATTERN.search(content)
                or contains_secret(content)
            ):
                continue
            candidates.append(
                {
                    "content": content,
                    "memory_type": memory_type,
                    "category": memory_type,
                    "confidence": 0.9 if memory_type != "fact" else 0.85,
                    "source": "auto:explicit_user_statement",
                }
            )
        return candidates

    def learn_from_interaction(
        self,
        user_text: str,
        *,
        scope: str,
        session_id: str | None = None,
        max_candidates: int = 3,
        outcome: str = "completed",
        owner_id: str | None = None,
    ) -> dict[str, int]:
        if not 1 <= max_candidates <= 10:
            raise ValueError("max_candidates must be between 1 and 10")
        active_owner = (owner_id or self.owner_id).strip() or DEFAULT_MEMORY_OWNER

        def record(event_type: str, metadata: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                self.store.record_event(event_type, session_id=session_id, metadata=metadata)

        considered = self.extract_candidates(user_text)[:max_candidates]
        accepted = rejected = 0
        for candidate in considered:
            content = str(candidate["content"])
            try:
                existing = self.store.search_memories(
                    content, limit=5, scope=scope, owner_id=active_owner
                )
                normalized = " ".join(content.casefold().split()).rstrip(".!?")
                if any(
                    (
                        " ".join(str(row.get("content", "")).casefold().split()).rstrip(".!?")
                        == normalized
                    )
                    for row in existing
                ):
                    rejected += 1
                    record(
                        "memory_candidate_rejected",
                        {"reason": "duplicate", "memory_type": candidate["memory_type"]},
                    )
                    continue
                self.store.add_memory(
                    content,
                    category=str(candidate["category"]),
                    memory_type=str(candidate["memory_type"]),
                    confidence=float(candidate["confidence"]),
                    source=str(candidate["source"]),
                    scope=scope,
                    provenance={
                        "session_id": session_id,
                        "outcome": outcome,
                        "extraction": "deterministic_explicit_statement",
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "owner_id": active_owner,
                    },
                    expires_at=(
                        (datetime.now(UTC) + timedelta(days=self.retention_days)).isoformat()
                        if self.retention_days
                        else ""
                    ),
                    trust="derived",
                    owner_id=active_owner,
                )
                self.enforce_budget(scope=scope)
                accepted += 1
                record(
                    "memory_candidate_accepted",
                    {
                        "reason": "eligible_explicit_statement",
                        "memory_type": candidate["memory_type"],
                    },
                )
            except Exception:
                rejected += 1
                record(
                    "memory_candidate_rejected",
                    {
                        "reason": "storage_or_validation_error",
                        "memory_type": candidate["memory_type"],
                    },
                )
        if not considered:
            record("memory_extraction_completed", {"considered": 0, "accepted": 0, "rejected": 0})
        else:
            record(
                "memory_extraction_completed",
                {"considered": len(considered), "accepted": accepted, "rejected": rejected},
            )
        return {"considered": len(considered), "accepted": accepted, "rejected": rejected}

    def correct(
        self, memory_id: int, content: str, *, scope: str, category: str | None = None
    ) -> bool:
        rows = self._scoped_rows(scope)
        if not any(int(row["id"]) == memory_id for row in rows):
            return False
        self._validate_content(content)
        return self.store.update_memory(memory_id, content, category=category)

    def forget_scoped(self, memory_id: int, *, scope: str) -> bool:
        rows = self._scoped_rows(scope)
        if not any(int(row["id"]) == memory_id for row in rows):
            return False
        return self.forget(memory_id)

    def _scoped_rows(self, scope: str) -> list[dict[str, Any]]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT id, content, tags, category, created_at, updated_at, memory_type, "
                "confidence, source, use_count, last_used_at, scope, provenance, expires_at, "
                "trust, owner_id FROM memories WHERE scope = ? AND trust != 'untrusted' "
                "AND owner_id IN (?, ?) ORDER BY id DESC LIMIT 100000",
                (scope, DEFAULT_MEMORY_OWNER, self.owner_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def enforce_budget(self, *, scope: str | None = None) -> dict[str, int]:
        expired = self.purge_expired()
        rows = (
            self.store.list_memories(limit=100_000) if scope is None else self._scoped_rows(scope)
        )
        overflow = max(0, len(rows) - self.max_memories)
        deleted = 0
        if overflow:
            oldest = sorted(
                rows, key=lambda row: (float(row.get("confidence", 0.7)), row.get("updated_at", ""))
            )[:overflow]
            for row in oldest:
                if self.store.delete_memory(int(row["id"])):
                    deleted += 1
        return {
            "expired": expired,
            "budget_deleted": deleted,
            "remaining": max(0, len(rows) - deleted - expired),
        }

    def purge_expired(self) -> int:
        with self.store._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE expires_at != '' AND expires_at <= ?",
                (datetime.now(UTC).isoformat(),),
            )
            return int(cursor.rowcount)

    def consolidate(
        self, *, scope: str | None = None, max_merges: int = 50, dry_run: bool = False
    ) -> dict[str, Any]:
        if not 1 <= max_merges <= 1000:
            raise ValueError("max_merges must be between 1 and 1000")
        rows = self._consolidation_rows(scope)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            memory_type = str(row.get("memory_type", "fact"))
            memory_class = MEMORY_CLASSES.get(memory_type, "semantic")
            normalized = " ".join(str(row.get("content", "")).casefold().split()).rstrip(".!?")
            if not normalized:
                continue
            groups.setdefault((memory_class, normalized), []).append(row)
        merges: list[dict[str, Any]] = []
        for (memory_class, _), group in sorted(groups.items()):
            if len(group) < 2 or len(merges) >= max_merges:
                continue
            ordered = sorted(
                group,
                key=lambda item: (
                    -float(item.get("confidence", 0.7)),
                    -int(item.get("use_count", 0)),
                    str(item.get("updated_at", "")),
                ),
            )
            survivor, duplicates = ordered[0], ordered[1:]
            merged_ids = [int(item["id"]) for item in duplicates]
            if dry_run:
                merges.append(
                    {
                        "survivor_id": int(survivor["id"]),
                        "merged_ids": merged_ids,
                        "class": memory_class,
                    }
                )
                continue
            survivor_id = int(survivor["id"])
            provenance = self._merged_provenance(survivor, duplicates)
            with self.store._connection() as connection:
                connection.execute(
                    "UPDATE memories SET provenance=?, confidence=?, "
                    "use_count=use_count+?, updated_at=? WHERE id=?",
                    (
                        json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
                        min(1.0, float(survivor.get("confidence", 0.7)) + 0.05),
                        sum(int(item.get("use_count", 0)) for item in duplicates),
                        datetime.now(UTC).isoformat(),
                        survivor_id,
                    ),
                )
                placeholders = ",".join("?" for _ in merged_ids)
                connection.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", merged_ids)
            self.store.record_event(
                "memory_consolidated",
                metadata={
                    "survivor_id": survivor_id,
                    "merged_ids": merged_ids,
                    "memory_class": memory_class,
                    "provenance_preserved": True,
                    "owner_id": self.owner_id,
                },
            )
            merges.append(
                {"survivor_id": survivor_id, "merged_ids": merged_ids, "class": memory_class}
            )
        return {
            "merged": len(merges),
            "deleted": sum(len(entry["merged_ids"]) for entry in merges),
            "groups": merges,
            "dry_run": dry_run,
        }

    def _consolidation_rows(self, scope: str | None) -> list[dict[str, Any]]:
        if scope is None:
            return self.store.list_memories(
                limit=100_000, include_untrusted=False, owner_id=self.owner_id
            )
        return self._scoped_rows(scope)

    @staticmethod
    def _merged_provenance(
        survivor: dict[str, Any], duplicates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        def parse(row: dict[str, Any]) -> dict[str, Any]:
            raw = row.get("provenance")
            if isinstance(raw, dict):
                return raw
            try:
                parsed = json.loads(str(raw or "{}"))
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        base = parse(survivor)
        consolidated_from = list(base.get("consolidated_from", []))
        sources = {str(base.get("source", ""))}
        for row in duplicates:
            duplicate_provenance = parse(row)
            sources.add(str(duplicate_provenance.get("source", "")))
            consolidated_from.append(
                {
                    "memory_id": int(row["id"]),
                    "source": duplicate_provenance.get("source", ""),
                    "owner_id": row.get("owner_id", ""),
                    "recorded_at": duplicate_provenance.get("recorded_at", ""),
                    "confidence": float(row.get("confidence", 0.7)),
                }
            )
        base["consolidated_from"] = consolidated_from
        base["consolidated_sources"] = sorted(source for source in sources if source)
        base["consolidated_at"] = datetime.now(UTC).isoformat()
        return base

    def statistics(self, *, scope: str | None = None) -> dict[str, int]:
        rows = self.store.list_memories(limit=100_000, scope=scope, owner_id=self.owner_id)
        by_type: dict[str, int] = {}
        for row in rows:
            key = str(row.get("memory_type", "fact"))
            by_type[key] = by_type.get(key, 0) + 1
        return {"total": len(rows), **{f"type_{key}": value for key, value in by_type.items()}}

    def memory_class_statistics(self, *, scope: str | None = None) -> dict[str, int]:
        rows = self.store.list_memories(limit=100_000, scope=scope, owner_id=self.owner_id)
        by_class: dict[str, int] = {name: 0 for name in sorted(MEMORY_CLASS_NAMES)}
        for row in rows:
            memory_type = str(row.get("memory_type", "fact"))
            memory_class = MEMORY_CLASSES.get(memory_type, "semantic")
            by_class[memory_class] = by_class.get(memory_class, 0) + 1
        return {
            "total": len(rows),
            **{f"{name}_memories": count for name, count in by_class.items()},
        }

    def reconcile_candidate(self, content: str, *, scope: str, category: str) -> int | None:
        normalized = " ".join(content.casefold().split()).rstrip(".!?")
        rows = self.store.list_memories(
            category=category, limit=10_000, scope=scope, owner_id=self.owner_id
        )
        for row in rows:
            current = " ".join(str(row.get("content", "")).casefold().split()).rstrip(".!?")
            if current == normalized:
                return int(row["id"])
        return None

    def curated_context(self, *, scope: str = "global", max_chars: int = 1800) -> list[str]:
        if not 100 <= max_chars <= 6000:
            raise ValueError("max_chars must be between 100 and 6000")
        rows = self.store.list_memories(limit=200, owner_id=self.owner_id)
        selected = [
            row
            for row in rows
            if row.get("memory_type") in {"profile", "preference", "project_fact"}
            and row.get("scope", "global") in {"global", scope}
            and float(row.get("confidence", 0.7)) >= 0.6
        ]
        result: list[str] = []
        used = 0
        for row in selected:
            line = f"[{row['memory_type']}] {str(row['content']).strip()}"
            if not line or used + len(line) > max_chars:
                continue
            result.append(line)
            used += len(line)
        return result


class PromptMemory:
    def __init__(
        self,
        database: Path,
        lessons: Any,
        *,
        max_chars: int = 1800,
        scope: str = "global",
        owner_id: str = DEFAULT_MEMORY_OWNER,
    ) -> None:
        self.database = database
        self.lessons = lessons
        self.max_chars = max_chars
        self.scope = scope
        self.owner_id = owner_id
        self._manager: MemoryManager | None = None

    def _get_manager(self) -> MemoryManager:
        if self._manager is None:
            self._manager = MemoryManager(self.database, owner_id=self.owner_id)
        return self._manager

    def _memory_lines(self, task: str) -> list[str]:
        terms = list(
            dict.fromkeys(
                word
                for word in re.findall(r"\w+", task.lower())
                if len(word) >= 3 and word.isalnum()
            )
        )[:12]
        if not self.database.exists() or not terms:
            return []
        manager = self._get_manager()
        rows = manager.store.relevant_memories(terms, 8, scope=self.scope, owner_id=self.owner_id)
        lines = [
            f"[memory] [{row.get('memory_type', 'fact')}] {row['content']}"
            for row in rows
            if row.get("category") != "lesson" and float(row.get("confidence", 0.7)) >= 0.55
        ]
        lines.extend(
            f"[memory] {line}"
            for line in manager.curated_context(scope=self.scope, max_chars=self.max_chars)
        )
        return lines

    async def relevant(self, task: str) -> list[str]:
        items = list(self.lessons.relevant(task))
        try:
            items.extend(await asyncio.to_thread(self._memory_lines, task))
        except Exception:
            logging.getLogger(__name__).debug("memory recall failed", exc_info=True)
        result: list[str] = []
        used = 0
        for item in items:
            clean = str(item).strip()
            if not clean or used + len(clean) > self.max_chars:
                continue
            result.append(clean)
            used += len(clean)
        return list(dict.fromkeys(result))
