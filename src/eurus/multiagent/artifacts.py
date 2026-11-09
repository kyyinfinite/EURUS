from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus.multiagent.contracts import ArtifactRef
from eurus.storage.database import Database, DatabasePath

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
STANDALONE_MISSION_ID = "standalone"
_MAX_METADATA_KEYS = 32
_MAX_PAGE_LIMIT = 500
_DEFAULT_PAGE_LIMIT = 50


class ArtifactError(ValueError):
    pass


class ArtifactPermissionError(PermissionError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    owner_id: str
    mission_id: str
    task_id: str | None
    assignment_id: str | None
    artifact_type: str
    media_type: str
    storage_key: str
    content_hash: str
    size_bytes: int
    version: int
    metadata: dict[str, Any]
    allowed_recipients: tuple[str, ...]
    created_at: datetime
    name: str = ""
    source_tool: str = ""
    run_id: str = ""
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or _now())

    def to_ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            artifact_type=self.artifact_type,
            version=self.version,
            content_hash=self.content_hash,
            media_type=self.media_type,
            owner_id=self.owner_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "owner_id": self.owner_id,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "artifact_type": self.artifact_type,
            "media_type": self.media_type,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "metadata": dict(self.metadata),
            "allowed_recipients": list(self.allowed_recipients),
            "created_at": _time(self.created_at),
            "name": self.name,
            "source_tool": self.source_tool,
            "run_id": self.run_id,
            "expires_at": _time(self.expires_at) if self.expires_at is not None else None,
        }


class ArtifactStore:
    def __init__(
        self,
        database: Database | DatabasePath,
        storage_root: Path | str,
        *,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        self.storage_root = Path(storage_root).expanduser().resolve()
        self.max_bytes = max_bytes
        self.storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def publish(
        self,
        content: bytes | str,
        *,
        owner_id: str,
        mission_id: str,
        task_id: str | None = None,
        assignment_id: str | None = None,
        artifact_type: str,
        media_type: str = "text/plain",
        metadata: dict[str, Any] | None = None,
        name: str = "",
        source_tool: str = "",
        run_id: str = "",
        expires_at: datetime | None = None,
    ) -> ArtifactRecord:
        if not owner_id.strip() or not mission_id.strip():
            raise ArtifactError("owner_id and mission_id are required")
        if not artifact_type.strip():
            raise ArtifactError("artifact_type is required")
        if metadata is not None and len(metadata) > _MAX_METADATA_KEYS:
            raise ArtifactError("too many metadata keys")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise ArtifactError("expires_at must include a timezone")
            if expires_at <= _now():
                raise ArtifactError("expires_at must be in the future")
        data = content.encode("utf-8") if isinstance(content, str) else content
        if not data:
            raise ArtifactError("artifact content must not be empty")
        if len(data) > self.max_bytes:
            raise ArtifactError(
                f"artifact exceeds the {self.max_bytes} byte limit ({len(data)} bytes)"
            )
        content_hash = hashlib.sha256(data).hexdigest()
        artifact_id = uuid.uuid4().hex
        version = self._next_version(owner_id, mission_id, task_id, artifact_type)
        relative_key = Path(owner_id) / f"{artifact_id}.bin"
        absolute_path = (self.storage_root / relative_key).resolve()
        if self.storage_root not in absolute_path.parents:
            raise ArtifactError("storage key escaped the artifact storage root")
        absolute_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute_path.write_bytes(data)
        try:
            absolute_path.chmod(0o600)
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO eurus_agent_artifacts(
                        artifact_id, owner_id, mission_id, task_id, assignment_id,
                        artifact_type, media_type, storage_key, content_hash,
                        size_bytes, version, metadata_json, allowed_recipients_json,
                        created_at, name, source_tool, run_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        owner_id,
                        mission_id,
                        task_id,
                        assignment_id,
                        artifact_type,
                        media_type,
                        str(relative_key),
                        content_hash,
                        len(data),
                        version,
                        json.dumps(metadata or {}),
                        json.dumps([]),
                        _time(_now()),
                        name.strip(),
                        source_tool.strip(),
                        run_id.strip(),
                        _time(expires_at) if expires_at is not None else None,
                    ),
                )
        except BaseException:
            absolute_path.unlink(missing_ok=True)
            raise
        return self._require(artifact_id, owner_id)

    def grant_access(
        self, artifact_id: str, recipient_assignment_id: str, *, owner_id: str
    ) -> bool:
        record = self._require(artifact_id, owner_id)
        recipients = set(record.allowed_recipients) | {recipient_assignment_id}
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE eurus_agent_artifacts SET allowed_recipients_json=? "
                "WHERE artifact_id=? AND owner_id=?",
                (json.dumps(sorted(recipients)), artifact_id, owner_id),
            )
        return True

    def get_metadata(self, artifact_id: str, *, owner_id: str) -> ArtifactRecord:
        return self._require(artifact_id, owner_id)

    def read(self, artifact_id: str, *, owner_id: str, assignment_id: str | None = None) -> bytes:
        record = self._require(artifact_id, owner_id)
        if record.is_expired():
            raise ArtifactError(f"artifact {artifact_id} has expired")
        self._authorize(record, assignment_id)
        path = self._path_for(record)
        if not path.is_file():
            raise ArtifactError(f"artifact {artifact_id} content is missing")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record.content_hash:
            raise ArtifactError(f"artifact {artifact_id} failed integrity verification")
        return data

    def read_text(
        self, artifact_id: str, *, owner_id: str, assignment_id: str | None = None
    ) -> str:
        return self.read(artifact_id, owner_id=owner_id, assignment_id=assignment_id).decode(
            "utf-8"
        )

    def verify_integrity(self, artifact_id: str, *, owner_id: str) -> bool:
        record = self._require(artifact_id, owner_id)
        path = self._path_for(record)
        if not path.is_file():
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == record.content_hash

    def list_for_task(
        self, mission_id: str, task_id: str, *, owner_id: str
    ) -> list[ArtifactRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                "WHERE owner_id=? AND mission_id=? AND task_id=? ORDER BY created_at",
                (owner_id, mission_id, task_id),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_mission(self, mission_id: str, *, owner_id: str) -> list[ArtifactRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                "WHERE owner_id=? AND mission_id=? ORDER BY created_at",
                (owner_id, mission_id),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_run(self, owner_id: str, run_id: str) -> list[ArtifactRecord]:
        if not run_id.strip():
            raise ArtifactError("run_id is required")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                "WHERE owner_id=? AND run_id=? ORDER BY created_at, artifact_id",
                (owner_id, run_id.strip()),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_recent(
        self, owner_id: str, *, limit: int = 50, include_expired: bool = False
    ) -> list[ArtifactRecord]:
        bounded = max(1, min(int(limit), 500))
        clause = "" if include_expired else "AND (expires_at IS NULL OR expires_at > ?) "
        params: tuple[Any, ...] = (owner_id,)
        if not include_expired:
            params += (_time(_now()),)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                f"WHERE owner_id=? {clause}"
                "ORDER BY created_at DESC, artifact_id DESC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_page(
        self,
        owner_id: str,
        *,
        limit: int = _DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        include_expired: bool = False,
    ) -> tuple[list[ArtifactRecord], str | None]:
        if not owner_id.strip():
            raise ArtifactError("owner_id is required")
        bounded = max(1, min(int(limit), _MAX_PAGE_LIMIT))
        expiry_clause = "" if include_expired else "AND (expires_at IS NULL OR expires_at > ?) "
        params: list[Any] = [owner_id]
        if not include_expired:
            params.append(_time(_now()))
        keyset_clause = ""
        if cursor is not None:
            anchor_created, anchor_id = _decode_cursor(cursor)
            keyset_clause = "AND (created_at < ? OR (created_at = ? AND artifact_id < ?)) "
            params.extend([_time(anchor_created), _time(anchor_created), anchor_id])
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                f"WHERE owner_id=? {expiry_clause}{keyset_clause}"
                "ORDER BY created_at DESC, artifact_id DESC LIMIT ?",
                (*params, bounded + 1),
            ).fetchall()
        records = [self._record(row) for row in rows[:bounded]]
        if len(rows) > bounded:
            last = records[-1]
            return records, _encode_cursor(last.created_at, last.artifact_id)
        return records, None

    def count_active(self, owner_id: str, *, include_expired: bool = False) -> int:
        if not owner_id.strip():
            raise ArtifactError("owner_id is required")
        clause = "" if include_expired else "AND (expires_at IS NULL OR expires_at > ?)"
        params: tuple[Any, ...] = (owner_id,)
        if not include_expired:
            params += (_time(_now()),)
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM eurus_agent_artifacts WHERE owner_id=? {clause}",
                params,
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def purge_expired(self, *, now: datetime | None = None) -> int:
        cutoff = _time(now or _now())
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM eurus_agent_artifacts "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (cutoff,),
            ).fetchall()
            doomed = [self._record(row) for row in rows]
            for record in doomed:
                connection.execute(
                    "DELETE FROM eurus_agent_artifacts WHERE artifact_id=? AND owner_id=?",
                    (record.artifact_id, record.owner_id),
                )
        for record in doomed:
            try:
                self._path_for(record).unlink(missing_ok=True)
            except (ArtifactError, OSError):
                continue
        return len(doomed)

    def _authorize(self, record: ArtifactRecord, assignment_id: str | None) -> None:
        if assignment_id is None:
            return
        if record.assignment_id == assignment_id:
            return
        if assignment_id in record.allowed_recipients:
            return
        raise ArtifactPermissionError(
            f"assignment {assignment_id!r} is not authorized to read artifact {record.artifact_id}"
        )

    def _path_for(self, record: ArtifactRecord) -> Path:
        candidate = (self.storage_root / record.storage_key).resolve()
        if self.storage_root not in candidate.parents:
            raise ArtifactError("storage key escaped the artifact storage root")
        return candidate

    def _require(self, artifact_id: str, owner_id: str) -> ArtifactRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_agent_artifacts WHERE artifact_id=? AND owner_id=?",
                (artifact_id, owner_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"artifact {artifact_id!r} not found for this owner")
        return self._record(row)

    def _next_version(
        self, owner_id: str, mission_id: str, task_id: str | None, artifact_type: str
    ) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM eurus_agent_artifacts "
                "WHERE owner_id=? AND mission_id=? AND task_id IS ? AND artifact_type=?",
                (owner_id, mission_id, task_id, artifact_type),
            ).fetchone()
        return int(row["total"]) + 1

    def _record(self, row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            owner_id=row["owner_id"],
            mission_id=row["mission_id"],
            task_id=row["task_id"],
            assignment_id=row["assignment_id"],
            artifact_type=row["artifact_type"],
            media_type=row["media_type"],
            storage_key=row["storage_key"],
            content_hash=row["content_hash"],
            size_bytes=int(row["size_bytes"]),
            version=int(row["version"]),
            metadata=_json_dict(row["metadata_json"]),
            allowed_recipients=tuple(_json_list(row["allowed_recipients_json"])),
            created_at=_parse(row["created_at"]) or _now(),
            name=str(row["name"] or ""),
            source_tool=str(row["source_tool"] or ""),
            run_id=str(row["run_id"] or ""),
            expires_at=_parse(row["expires_at"]),
        )


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_cursor(created_at: datetime, artifact_id: str) -> str:
    return f"{_time(created_at)}|{artifact_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    parts = cursor.split("|")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ArtifactError("malformed pagination cursor")
    parsed = _parse(parts[0].strip())
    if parsed is None:
        raise ArtifactError("malformed pagination cursor")
    return parsed, parts[1].strip()


def _json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
