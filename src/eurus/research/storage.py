from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.research.errors import StorageError
from eurus.research.settings import ResearchSettings

JOB_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
JOB_ID_RE = re.compile(r"^rj-\d{8}-[a-z0-9]{10}$")
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS research_jobs (
        job_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        objective TEXT NOT NULL,
        status TEXT NOT NULL,
        round_count INTEGER NOT NULL DEFAULT 0,
        queries_used INTEGER NOT NULL DEFAULT 0,
        sources_found INTEGER NOT NULL DEFAULT 0,
        fetches INTEGER NOT NULL DEFAULT 0,
        qa_status TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_job_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS research_job_events_job ON research_job_events(job_id)",
    """
    CREATE TABLE IF NOT EXISTS research_sources (
        source_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        domain TEXT NOT NULL,
        title TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        provider TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS research_sources_job ON research_sources(job_id)",
    """
    CREATE TABLE IF NOT EXISTS research_search_cache (
        cache_key TEXT PRIMARY KEY,
        query TEXT NOT NULL,
        providers TEXT NOT NULL,
        limit_used INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_page_cache (
        cache_key TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_job_id() -> str:
    stamp = time.strftime("%Y%m%d", time.gmtime())
    random_part = "".join(secrets.choice(JOB_ID_ALPHABET) for _ in range(10))
    return f"rj-{stamp}-{random_part}"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise StorageError(f"cannot write {path}: {exc}") from exc


class ResearchStore:
    def __init__(
        self,
        *,
        settings: ResearchSettings,
        environ: dict[str, str] | None = None,
        root: Path | None = None,
    ) -> None:
        self.settings = settings
        self._environ = environ
        if root is not None:
            self.root = root
        else:
            self.root = paths.home_manager(environ).root / "research"
        self.indexes_dir = self.root / "indexes"
        self.jobs_dir = self.root / "jobs"
        self.sources_dir = self.root / "sources"
        self.cache_dir = self.root / "cache"
        self.search_cache_dir = self.cache_dir / "search"
        self.page_cache_dir = self.cache_dir / "pages"
        self.exports_dir = self.root / "exports"
        self.database_path = self.indexes_dir / "research.sqlite3"
        for directory in (
            self.root,
            self.indexes_dir,
            self.jobs_dir,
            self.sources_dir,
            self.search_cache_dir,
            self.page_cache_dir,
            self.exports_dir,
        ):
            paths.ensure_private_dir(directory)
        self._connection: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(sqlite3.Error):
                self._connection.close()
            self._connection = None

    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            try:
                conn = sqlite3.connect(self.database_path, timeout=10.0, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.commit()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot open research storage: {exc}") from exc
            self._connection = conn
        return self._connection

    def _execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            conn = self.connection()
            cursor = conn.execute(sql, parameters)
            conn.commit()
            return cursor
        except sqlite3.Error as exc:
            raise StorageError(f"research storage query failed: {exc}") from exc

    def job_directory(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise StorageError(f"invalid research job id: {job_id!r}")
        return self.jobs_dir / job_id

    def create_job(self, mode: str, objective: str) -> str:
        job_id = new_job_id()
        now = utc_now_iso()
        directory = self.job_directory(job_id)
        paths.ensure_private_dir(directory)
        record = {
            "job_id": job_id,
            "mode": mode,
            "objective": objective,
            "status": "pending",
            "round_count": 0,
            "queries_used": 0,
            "sources_found": 0,
            "fetches": 0,
            "qa_status": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        _atomic_write(directory / "job.json", json.dumps(record, ensure_ascii=False, indent=2))
        self._execute(
            "INSERT INTO research_jobs (job_id, mode, objective, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, mode, objective, "pending", now, now),
        )
        return job_id

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_directory(job_id) / "job.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        current = self.load_job(job_id)
        if current is None:
            raise StorageError(f"research job not found: {job_id}")
        allowed = {
            "status",
            "round_count",
            "queries_used",
            "sources_found",
            "fetches",
            "qa_status",
            "error",
            "finished_at",
            "objective",
            "mode",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        current.update(updates)
        current["updated_at"] = utc_now_iso()
        status = str(updates.get("status") or current.get("status") or "running")
        if status in {"completed", "failed", "cancelled", "incomplete"} and not current.get(
            "finished_at"
        ):
            current["finished_at"] = utc_now_iso()
        _atomic_write(
            self.job_directory(job_id) / "job.json",
            json.dumps(current, ensure_ascii=False, indent=2),
        )
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [current["updated_at"], job_id]
        self._execute(
            f"UPDATE research_jobs SET {assignments}, updated_at = ? WHERE job_id = ?",
            tuple(values),
        )

    def set_status(self, job_id: str, status: str, *, error: str = "") -> None:
        self.update_job(job_id, status=status, error=error)

    def record_event(
        self, job_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        self._execute(
            "INSERT INTO research_job_events (job_id, event_type, payload_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (job_id, event_type, json.dumps(payload or {}, ensure_ascii=False), utc_now_iso()),
        )

    def save_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        _atomic_write(
            self.job_directory(job_id) / "plan.json",
            json.dumps(plan, ensure_ascii=False, indent=2),
        )

    def save_round(self, job_id: str, round_record: dict[str, Any]) -> None:
        rounds_dir = self.job_directory(job_id) / "rounds"
        paths.ensure_private_dir(rounds_dir)
        number = int(round_record.get("round_number", 1))
        _atomic_write(
            rounds_dir / f"round_{number:02d}.json",
            json.dumps(round_record, ensure_ascii=False, indent=2),
        )

    def save_sources(self, job_id: str, sources: list[dict[str, Any]]) -> None:
        _atomic_write(
            self.job_directory(job_id) / "sources.json",
            json.dumps(sources, ensure_ascii=False, indent=2),
        )
        now = utc_now_iso()
        for item in sources:
            self._execute(
                "INSERT OR REPLACE INTO research_sources"
                " (source_id, job_id, url, canonical_url, domain, title, content_hash,"
                " provider, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.get("source_id", ""),
                    job_id,
                    item.get("url", ""),
                    item.get("canonical_url", ""),
                    item.get("domain", ""),
                    item.get("title", ""),
                    item.get("content_hash", ""),
                    item.get("provider", ""),
                    now,
                ),
            )
            source_path = self.sources_dir / f"{item.get('source_id', 'unknown')}.json"
            _atomic_write(
                source_path,
                json.dumps({"job_id": job_id, **item}, ensure_ascii=False, indent=2),
            )

    def save_evidence(self, job_id: str, evidence: list[dict[str, Any]]) -> None:
        _atomic_write(
            self.job_directory(job_id) / "evidence.json",
            json.dumps(evidence, ensure_ascii=False, indent=2),
        )

    def save_result(self, job_id: str, result: dict[str, Any]) -> None:
        _atomic_write(
            self.job_directory(job_id) / "result.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        _atomic_write(
            self.exports_dir / f"{job_id}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )

    def load_state(self, job_id: str) -> dict[str, Any] | None:
        job = self.load_job(job_id)
        if job is None:
            return None
        directory = self.job_directory(job_id)

        def _read(name: str) -> dict[str, Any] | list[Any] | None:
            try:
                raw = (directory / name).read_text(encoding="utf-8")
            except OSError:
                return None
            try:
                payload = json.loads(raw)
            except ValueError:
                return None
            if isinstance(payload, (dict, list)):
                return payload
            return None

        return {
            "job": job,
            "plan": _read("plan.json"),
            "sources": _read("sources.json"),
            "evidence": _read("evidence.json"),
            "result": _read("result.json"),
        }

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT job_id, mode, objective, status, round_count, queries_used,"
            " sources_found, fetches, qa_status, created_at, updated_at"
            " FROM research_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def cached_search(
        self, query: str, limit: int, providers: tuple[str, ...]
    ) -> list[dict[str, Any]] | None:
        import hashlib

        key = hashlib.sha256(
            f"{query.casefold()}|{limit}|{','.join(providers)}".encode()
        ).hexdigest()
        now = time.time()
        row = self._execute(
            "SELECT payload_json, expires_at FROM research_search_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) < now:
            self._execute("DELETE FROM research_search_cache WHERE cache_key = ?", (key,))
            return None
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            return None
        return payload if isinstance(payload, list) else None

    def store_cached_search(
        self, query: str, limit: int, providers: tuple[str, ...], hits: list[dict[str, Any]]
    ) -> None:
        import hashlib

        key = hashlib.sha256(
            f"{query.casefold()}|{limit}|{','.join(providers)}".encode()
        ).hexdigest()
        now = time.time()
        self._execute(
            "INSERT OR REPLACE INTO research_search_cache"
            " (cache_key, query, providers, limit_used, payload_json, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                query,
                ",".join(providers),
                limit,
                json.dumps(hits, ensure_ascii=False),
                utc_now_iso(),
                now + self.settings.cache_ttl_seconds,
            ),
        )
        self._evict_search_cache()

    def _evict_search_cache(self) -> None:
        count = self._execute("SELECT COUNT(*) AS n FROM research_search_cache").fetchone()
        total = int(count["n"]) if count is not None else 0
        if total <= self.settings.cache_max_entries:
            return
        surplus = total - self.settings.cache_max_entries
        self._execute(
            "DELETE FROM research_search_cache WHERE cache_key IN ("
            " SELECT cache_key FROM research_search_cache"
            " ORDER BY expires_at ASC LIMIT ?)",
            (surplus,),
        )

    def cached_page(self, url: str) -> dict[str, Any] | None:
        import hashlib

        key = hashlib.sha256(url.casefold().encode("utf-8")).hexdigest()
        now = time.time()
        row = self._execute(
            "SELECT path, expires_at, content_hash FROM research_page_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) < now:
            self._execute("DELETE FROM research_page_cache WHERE cache_key = ?", (key,))
            Path(row["path"]).unlink(missing_ok=True)
            return None
        path = Path(row["path"])
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        payload["from_cache"] = True
        return payload

    def store_cached_page(self, url: str, page: dict[str, Any]) -> None:
        import hashlib

        key = hashlib.sha256(url.casefold().encode("utf-8")).hexdigest()
        now = time.time()
        path = self.page_cache_dir / f"{key}.json"
        payload = {name: value for name, value in page.items() if name != "from_cache"}
        _atomic_write(path, json.dumps(payload, ensure_ascii=False))
        self._execute(
            "INSERT OR REPLACE INTO research_page_cache"
            " (cache_key, url, content_hash, path, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                url,
                str(payload.get("content_hash", "")),
                str(path),
                utc_now_iso(),
                now + self.settings.cache_ttl_seconds,
            ),
        )
        self._evict_page_cache()

    def _evict_page_cache(self) -> None:
        count = self._execute("SELECT COUNT(*) AS n FROM research_page_cache").fetchone()
        total = int(count["n"]) if count is not None else 0
        if total <= self.settings.cache_max_entries:
            return
        surplus = total - self.settings.cache_max_entries
        rows = self._execute(
            "SELECT cache_key, path FROM research_page_cache ORDER BY expires_at ASC LIMIT ?",
            (surplus,),
        ).fetchall()
        for row in rows:
            self._execute(
                "DELETE FROM research_page_cache WHERE cache_key = ?", (row["cache_key"],)
            )
            Path(row["path"]).unlink(missing_ok=True)

    def invalidate_cache(self) -> int:
        removed = 0
        for row in self._execute("SELECT cache_key, path FROM research_page_cache").fetchall():
            Path(row["path"]).unlink(missing_ok=True)
            removed += 1
        self._execute("DELETE FROM research_page_cache")
        self._execute("DELETE FROM research_search_cache")
        return removed

    def prune_jobs(self) -> int:
        cutoff = time.time() - self.settings.job_retention_days * 86_400
        removed = 0
        rows = self._execute("SELECT job_id, created_at FROM research_jobs", ()).fetchall()
        for row in rows:
            try:
                created = datetime.fromisoformat(str(row["created_at"])).timestamp()
            except ValueError:
                continue
            if created >= cutoff:
                continue
            job_id = str(row["job_id"])
            self._execute("DELETE FROM research_jobs WHERE job_id = ?", (job_id,))
            self._execute("DELETE FROM research_sources WHERE job_id = ?", (job_id,))
            self._execute("DELETE FROM research_job_events WHERE job_id = ?", (job_id,))
            import shutil

            shutil.rmtree(self.job_directory(job_id), ignore_errors=True)
            (self.exports_dir / f"{job_id}.json").unlink(missing_ok=True)
            removed += 1
        return removed
