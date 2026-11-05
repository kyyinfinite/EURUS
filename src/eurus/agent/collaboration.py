from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.multiagent.artifacts import ArtifactRecord, ArtifactStore
from eurus.multiagent.communication import MessageStore
from eurus.multiagent.contracts import ArtifactRef
from eurus.storage.database import Database, DatabasePath

COLLAB_MESSAGE_TYPES = frozenset(
    {
        "task_request",
        "progress_update",
        "result_delivery",
        "failure_report",
        "clarification_request",
        "cancellation_signal",
        "artifact_published",
        "review_request",
        "review_result",
        "revision_request",
        "blocker_report",
        "decision_record",
        "task_dependency_ready",
    }
)

MAX_REFS_PER_MESSAGE = 16


@dataclass(frozen=True, slots=True)
class CollaborationMessage:
    message_id: str
    assignment_id: str
    sender: str
    message_type: str
    body: dict[str, Any]
    artifact_refs: tuple[ArtifactRef, ...]
    correlation_id: str


class CollaborationError(ValueError):
    pass


class CollaborationHub:
    def __init__(
        self,
        database: Database | DatabasePath,
        storage_root: Path | str,
        *,
        max_artifact_bytes: int | None = None,
    ) -> None:
        self.database = database if isinstance(database, Database) else Database(database)
        if max_artifact_bytes is None:
            self.artifacts = ArtifactStore(self.database, storage_root)
        else:
            self.artifacts = ArtifactStore(
                self.database, storage_root, max_bytes=max_artifact_bytes
            )
        self.messages = MessageStore(self.database)

    def publish_artifact(
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
        announce_to: tuple[str, ...] | None = None,
    ) -> ArtifactRecord:
        record = self.artifacts.publish(
            content,
            owner_id=owner_id,
            mission_id=mission_id,
            task_id=task_id,
            assignment_id=assignment_id,
            artifact_type=artifact_type,
            media_type=media_type,
            metadata=metadata,
        )
        for recipient in announce_to or ():
            self._grant(record, recipient, owner_id=owner_id)
            self.messages.record(
                recipient,
                assignment_id or "orchestrator",
                "artifact_published",
                _body(
                    {
                        "artifact_id": record.artifact_id,
                        "artifact_type": record.artifact_type,
                        "version": record.version,
                        "content_hash": record.content_hash,
                        "media_type": record.media_type,
                        "mission_id": mission_id,
                    }
                ),
            )
        return record

    def share_artifact(
        self,
        artifact_id: str,
        recipient_assignment_id: str,
        *,
        owner_id: str,
    ) -> bool:
        record = self.artifacts.get_metadata(artifact_id, owner_id=owner_id)
        self._grant(record, recipient_assignment_id, owner_id=owner_id)
        return True

    def read_artifact(
        self,
        artifact_id: str,
        *,
        owner_id: str,
        assignment_id: str | None = None,
    ) -> bytes:
        return self.artifacts.read(artifact_id, owner_id=owner_id, assignment_id=assignment_id)

    def send(
        self,
        *,
        assignment_id: str,
        sender: str,
        message_type: str,
        body: dict[str, Any] | str,
        owner_id: str,
        mission_id: str,
        artifact_refs: tuple[ArtifactRef, ...] | list[ArtifactRef] = (),
        correlation_id: str = "",
    ) -> CollaborationMessage:
        if message_type not in COLLAB_MESSAGE_TYPES:
            raise CollaborationError(f"unknown collaboration message type: {message_type!r}")
        body_json = _body(body) if isinstance(body, dict) else str(body)
        refs = tuple(artifact_refs)
        if len(refs) > MAX_REFS_PER_MESSAGE:
            raise CollaborationError(
                f"a message may reference at most {MAX_REFS_PER_MESSAGE} artifacts"
            )
        self._validate_refs(refs, owner_id=owner_id, mission_id=mission_id)
        envelope = {
            "mission_id": mission_id,
            "correlation_id": correlation_id,
            "artifact_refs": [ref.to_dict() for ref in refs],
            "body": body_json[:6000],
        }
        message = self.messages.record(assignment_id, sender, message_type, json.dumps(envelope))
        return CollaborationMessage(
            message_id=message.message_id,
            assignment_id=assignment_id,
            sender=sender,
            message_type=message_type,
            body=_safe_dict(body_json),
            artifact_refs=refs,
            correlation_id=correlation_id,
        )

    def request_review(
        self,
        *,
        assignment_id: str,
        sender: str,
        owner_id: str,
        mission_id: str,
        artifact_id: str,
        scope: str = "",
    ) -> CollaborationMessage:
        record = self.artifacts.get_metadata(artifact_id, owner_id=owner_id)
        return self.send(
            assignment_id=assignment_id,
            sender=sender,
            message_type="review_request",
            body={"artifact_id": artifact_id, "scope": scope},
            owner_id=owner_id,
            mission_id=mission_id,
            artifact_refs=(record.to_ref(),),
            correlation_id=mission_id,
        )

    def report_blocker(
        self,
        *,
        assignment_id: str,
        sender: str,
        owner_id: str,
        mission_id: str,
        blocker: str,
    ) -> CollaborationMessage:
        return self.send(
            assignment_id=assignment_id,
            sender=sender,
            message_type="blocker_report",
            body={"blocker": blocker},
            owner_id=owner_id,
            mission_id=mission_id,
            correlation_id=mission_id,
        )

    def record_decision(
        self,
        *,
        assignment_id: str,
        sender: str,
        owner_id: str,
        mission_id: str,
        decision: str,
        rationale: str = "",
        source_artifact_ids: tuple[str, ...] | list[str] = (),
    ) -> CollaborationMessage:
        refs: list[ArtifactRef] = []
        for artifact_id in source_artifact_ids:
            record = self.artifacts.get_metadata(str(artifact_id), owner_id=owner_id)
            refs.append(record.to_ref())
        return self.send(
            assignment_id=assignment_id,
            sender=sender,
            message_type="decision_record",
            body={"decision": decision, "rationale": rationale},
            owner_id=owner_id,
            mission_id=mission_id,
            artifact_refs=tuple(refs),
            correlation_id=mission_id,
        )

    def notify_dependency_ready(
        self,
        *,
        assignment_id: str,
        sender: str,
        owner_id: str,
        mission_id: str,
        task_key: str,
    ) -> CollaborationMessage:
        return self.send(
            assignment_id=assignment_id,
            sender=sender,
            message_type="task_dependency_ready",
            body={"task_key": task_key},
            owner_id=owner_id,
            mission_id=mission_id,
            correlation_id=mission_id,
        )

    def conversation(self, assignment_id: str, *, limit: int = 100) -> list[CollaborationMessage]:
        rendered: list[CollaborationMessage] = []
        for message in self.messages.list_for_assignment(assignment_id, limit=limit):
            payload = _safe_envelope(message.body)
            refs = tuple(
                ArtifactRef(
                    artifact_id=str(item.get("artifact_id", "")),
                    artifact_type=str(item.get("artifact_type", "report")),
                    version=int(item.get("version", 1) or 1),
                    content_hash=str(item.get("content_hash", "")),
                    media_type=str(item.get("media_type", "text/plain")),
                    owner_id=str(item.get("owner_id", "")),
                )
                for item in payload.get("artifact_refs", [])
                if isinstance(item, dict) and str(item.get("artifact_id", "")).strip()
            )
            rendered.append(
                CollaborationMessage(
                    message_id=message.message_id,
                    assignment_id=assignment_id,
                    sender=message.sender,
                    message_type=message.message_type,
                    body=_safe_dict(str(payload.get("body", ""))),
                    artifact_refs=refs,
                    correlation_id=str(payload.get("correlation_id", "")),
                )
            )
        return rendered

    def _grant(self, record: ArtifactRecord, recipient: str, *, owner_id: str) -> None:
        if not recipient.strip():
            raise CollaborationError("recipient assignment id must not be empty")
        self.artifacts.grant_access(record.artifact_id, recipient, owner_id=owner_id)

    def _validate_refs(
        self, refs: tuple[ArtifactRef, ...], *, owner_id: str, mission_id: str
    ) -> None:
        for ref in refs:
            record = self.artifacts.get_metadata(ref.artifact_id, owner_id=owner_id)
            if record.mission_id != mission_id:
                raise CollaborationError(
                    f"artifact {ref.artifact_id} belongs to mission "
                    f"{record.mission_id!r}, not {mission_id!r}"
                )


def _body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _safe_dict(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"text": raw}
    return parsed if isinstance(parsed, dict) else {"text": raw}


def _safe_envelope(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"body": raw}
    return parsed if isinstance(parsed, dict) else {"body": raw}
