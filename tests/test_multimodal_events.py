import asyncio
from pathlib import Path

import pytest

from eurus.agent.events import AgentEvent
from eurus.content import AudioBlock, ImageBlock, TextBlock
from eurus.events.multimodal import MultimodalEventPublisher, safe_publish
from eurus.events.store import DomainEventStore
from eurus.events.types import (
    AUDIO_RECEIVED,
    DOCUMENT_RECEIVED,
    IMAGE_RECEIVED,
    INBOUND_EVENT_TYPES,
    MEDIA_ANALYSIS_COMPLETED,
    MEDIA_PROCESSING_EVENT_TYPES,
    MEDIA_PROCESSING_FAILED,
    MEDIA_PROCESSING_STARTED,
    MEDIA_RECEIVED,
    MEDIA_TRANSCRIPTION_COMPLETED,
    MEDIA_TRANSCRIPTION_DELTA,
    MEDIA_TRANSCRIPTION_STARTED,
    MEDIA_VALIDATION_COMPLETED,
    MEDIA_VALIDATION_STARTED,
    MESSAGE_RECEIVED,
    MULTIMODAL_EVENT_TYPES,
    SCHEMA_VERSIONS,
    VIDEO_FRAME_EXTRACTED,
    VIDEO_RECEIVED,
    EventPayloadError,
    media_received_event_type,
    schema_version_for,
    validate_payload,
)
from eurus.storage.migrations import LATEST_SCHEMA_VERSION

ASSET_A = "a" * 32
ASSET_B = "b" * 32
SHA = "c" * 64

MEDIA_VIEW = {
    "media_id": ASSET_A,
    "session_id": "session-1",
    "kind": "image",
    "mime": "image/png",
    "size": 128,
    "sha256": SHA,
    "filename": "photo.png",
}


def test_required_event_vocabulary_is_defined():
    assert MESSAGE_RECEIVED == "MESSAGE_RECEIVED"
    assert MEDIA_RECEIVED == "MEDIA_RECEIVED"
    assert IMAGE_RECEIVED == "IMAGE_RECEIVED"
    assert AUDIO_RECEIVED == "AUDIO_RECEIVED"
    assert VIDEO_RECEIVED == "VIDEO_RECEIVED"
    assert DOCUMENT_RECEIVED == "DOCUMENT_RECEIVED"
    assert len(MULTIMODAL_EVENT_TYPES) == 15
    assert set(INBOUND_EVENT_TYPES) | set(MEDIA_PROCESSING_EVENT_TYPES) == set(
        MULTIMODAL_EVENT_TYPES
    )


def test_every_event_type_has_schema_version():
    for event_type in MULTIMODAL_EVENT_TYPES:
        assert schema_version_for(event_type) >= 1
    assert schema_version_for("UNKNOWN_EVENT_TYPE") == 1
    assert all(version == 1 for version in SCHEMA_VERSIONS.values())


@pytest.mark.parametrize("event_type", MULTIMODAL_EVENT_TYPES)
def test_valid_payloads_pass_for_every_type(event_type):
    payloads = {
        MESSAGE_RECEIVED: {
            "session_id": "session-1",
            "message_id": "run-1",
            "text": "hello",
            "media_ids": [ASSET_A],
            "source": "web",
        },
        MEDIA_RECEIVED: MEDIA_VIEW,
        IMAGE_RECEIVED: MEDIA_VIEW,
        AUDIO_RECEIVED: {**MEDIA_VIEW, "kind": "audio", "mime": "audio/mpeg"},
        VIDEO_RECEIVED: {**MEDIA_VIEW, "kind": "video", "mime": "video/mp4"},
        DOCUMENT_RECEIVED: {
            **MEDIA_VIEW,
            "kind": "document",
            "mime": "application/pdf",
        },
        MEDIA_VALIDATION_STARTED: {"media_id": ASSET_A, "session_id": "session-1"},
        MEDIA_VALIDATION_COMPLETED: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "valid": True,
        },
        MEDIA_PROCESSING_STARTED: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "stage": "metadata",
        },
        MEDIA_TRANSCRIPTION_DELTA: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "text": "partial words",
            "sequence": 2,
        },
        MEDIA_TRANSCRIPTION_COMPLETED: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "text": "full transcript",
            "duration_seconds": 12.5,
        },
        VIDEO_FRAME_EXTRACTED: {
            "media_id": ASSET_A,
            "frame_media_id": ASSET_B,
            "session_id": "session-1",
            "timestamp_seconds": 4.5,
        },
        MEDIA_ANALYSIS_COMPLETED: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "summary": "a red chart",
        },
        MEDIA_PROCESSING_FAILED: {
            "media_id": ASSET_A,
            "session_id": "session-1",
            "stage": "thumbnail",
            "error_code": "decoder_error",
        },
    }
    if event_type == MEDIA_TRANSCRIPTION_STARTED:
        payloads[event_type] = {"media_id": ASSET_A, "language": "en"}
    validate_payload(event_type, payloads[event_type])


def test_kind_specific_events_enforce_matching_kind():
    with pytest.raises(EventPayloadError, match="kind must be 'image'"):
        validate_payload(IMAGE_RECEIVED, {**MEDIA_VIEW, "kind": "audio"})
    with pytest.raises(EventPayloadError, match="kind must be 'audio'"):
        validate_payload(AUDIO_RECEIVED, {**MEDIA_VIEW, "kind": "video"})


def test_invalid_asset_ids_rejected_in_payloads():
    with pytest.raises(EventPayloadError, match="media_id"):
        validate_payload(MEDIA_VALIDATION_STARTED, {"media_id": "not-hex"})
    with pytest.raises(EventPayloadError, match="frame_media_id"):
        validate_payload(VIDEO_FRAME_EXTRACTED, {"media_id": ASSET_A, "frame_media_id": "xyz"})
    with pytest.raises(EventPayloadError, match="hex"):
        validate_payload(MESSAGE_RECEIVED, {"session_id": "s", "media_ids": ["bad-id"]})


def test_binary_values_rejected_in_payloads():
    with pytest.raises(EventPayloadError, match="binary"):
        validate_payload(MESSAGE_RECEIVED, {"session_id": "s", "text": b"raw bytes"})
    with pytest.raises(EventPayloadError, match="binary"):
        validate_payload(
            MESSAGE_RECEIVED,
            {"session_id": "s", "media_ids": [b"bytes-id"]},
        )


def test_unknown_event_type_rejected():
    with pytest.raises(EventPayloadError, match="no multimodal payload contract"):
        validate_payload("TASK_STARTED", {"media_id": ASSET_A})
    with pytest.raises(EventPayloadError, match="uppercase"):
        validate_payload("media_received", {})


def test_processing_stages_are_constrained():
    with pytest.raises(EventPayloadError, match="stage"):
        validate_payload(
            MEDIA_PROCESSING_STARTED,
            {"media_id": ASSET_A, "stage": "madness"},
        )
    with pytest.raises(EventPayloadError, match="stage"):
        validate_payload(
            MEDIA_PROCESSING_FAILED,
            {"media_id": ASSET_A, "stage": "unknown", "error_code": "x"},
        )


def test_media_kind_event_mapping():
    assert media_received_event_type("image") == IMAGE_RECEIVED
    assert media_received_event_type("audio") == AUDIO_RECEIVED
    assert media_received_event_type("video") == VIDEO_RECEIVED
    assert media_received_event_type("document") == DOCUMENT_RECEIVED
    with pytest.raises(EventPayloadError):
        media_received_event_type("hologram")


def test_agent_event_multimodal_factories():
    block = ImageBlock(asset_id=ASSET_A, mime_type="image/png")
    started = AgentEvent.content_block_started(0, block)
    delta = AgentEvent.content_block_delta(0, "caption ")
    completed = AgentEvent.content_block_completed(0, block)
    analyzing = AgentEvent.media_input_analyzing(ASSET_A, "image")
    audio_started = AgentEvent.audio_output_started(ASSET_B)
    audio_delta = AgentEvent.audio_output_delta("hello ")
    artifact = AgentEvent.artifact_lifecycle(ASSET_B, "ready")
    assert started.kind == "content_block_started"
    assert started.block is block and started.block_index == 0
    assert delta.kind == "content_block_delta" and delta.text == "caption "
    assert completed.kind == "content_block_completed"
    assert analyzing.kind == "media_input_analyzing"
    assert analyzing.asset_id == ASSET_A and analyzing.media_kind == "image"
    assert audio_started.kind == "audio_output_started" and audio_started.asset_id == ASSET_B
    assert audio_delta.kind == "audio_output_delta"
    assert artifact.kind == "artifact" and artifact.artifact_status == "ready"
    assert artifact.artifact_id == ASSET_B


def test_agent_event_artifact_status_is_validated():
    with pytest.raises(ValueError, match="artifact status"):
        AgentEvent.artifact_lifecycle(ASSET_A, "vapourized")


def test_agent_event_text_events_remain_unchanged():
    event = AgentEvent.text_delta("hi")
    assert event.kind == "text" and event.text == "hi"
    assert event.block is None and event.block_index == 0
    assert AgentEvent.text_reset().kind == "text_reset"


@pytest.fixture()
def store(tmp_path: Path) -> DomainEventStore:
    return DomainEventStore(tmp_path / "events.db")


def test_domain_event_schema_version_roundtrip(store: DomainEventStore):
    event = store.publish(MEDIA_RECEIVED, payload=validate_payload(MEDIA_RECEIVED, MEDIA_VIEW))
    assert event is not None
    assert event.schema_version == 1
    stored = store.get_event(event.event_id)
    assert stored is not None
    assert stored.schema_version == 1
    assert stored.event_type == MEDIA_RECEIVED
    assert stored.subject_id == ""


def test_domain_event_explicit_schema_version(store: DomainEventStore):
    event = store.publish("CUSTOM_EVENT", payload={}, schema_version=3)
    assert event is not None and event.schema_version == 3
    with pytest.raises(ValueError):
        store.publish("CUSTOM_EVENT", payload={}, schema_version=0)
    with pytest.raises(ValueError):
        store.publish("CUSTOM_EVENT", payload={}, schema_version=True)


def test_domain_event_migration_adds_column(tmp_path: Path):
    database = tmp_path / "migrated.db"
    store = DomainEventStore(database)
    store.publish(MEDIA_RECEIVED, payload=validate_payload(MEDIA_RECEIVED, MEDIA_VIEW))
    import sqlite3

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(eurus_domain_events)")}
    assert "schema_version" in columns
    version_row = connection.execute("SELECT MAX(version) FROM eurus_schema_migrations").fetchone()
    assert version_row[0] == LATEST_SCHEMA_VERSION


def test_domain_event_legacy_rows_default_to_version_one(tmp_path: Path):
    import sqlite3

    from eurus.storage.migrations import apply_migrations

    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        connection.execute(
            """INSERT INTO eurus_domain_events(
                event_id, event_type, subject_type, subject_id, payload_json,
                correlation_id, causation_id, dedup_key, occurred_at, created_at, owner_id)
               VALUES ('legacy1', 'OLD_EVENT', '', '', '{}', '', '', NULL,
                       '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '')"""
        )
    store = DomainEventStore(database)
    legacy = store.get_event("legacy1")
    assert legacy is not None
    assert legacy.schema_version == 1
    assert legacy.event_type == "OLD_EVENT"


def test_publisher_media_received_emits_generic_and_specific(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store, owner_id="eurus")
    published = publisher.media_received(MEDIA_VIEW, source="web")
    types = [event.event_type for event in published]
    assert types == [MEDIA_RECEIVED, IMAGE_RECEIVED]
    assert all(event.owner_id == "eurus" for event in published)
    assert all(event.subject_type == "media" for event in published)
    assert all(event.subject_id == ASSET_A for event in published)


def test_publisher_media_received_is_idempotent_by_dedup(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store)
    first = publisher.media_received(MEDIA_VIEW, source="web")
    second = publisher.media_received(MEDIA_VIEW, source="web")
    assert len(first) == 2
    assert second == []
    events = store.list_events(limit=10)
    assert len(events) == 2


def test_publisher_message_received(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store, owner_id="eurus")
    event = publisher.message_received(
        "session-1",
        message_id="run-9",
        text="analyze this",
        media_ids=[ASSET_A, ASSET_B],
        source="web",
        correlation_id="run-9",
    )
    assert event is not None
    assert event.event_type == MESSAGE_RECEIVED
    assert event.correlation_id == "run-9"
    assert event.dedup_key == "message-received:web:run-9"
    duplicate = publisher.message_received(
        "session-1",
        message_id="run-9",
        media_ids=[],
        source="web",
    )
    assert duplicate is None


def test_publisher_media_event_processing_lifecycle(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store)
    started = publisher.media_event(
        MEDIA_PROCESSING_STARTED,
        ASSET_A,
        {"stage": "metadata", "session_id": "session-1"},
        correlation_id="run-1",
    )
    failed = publisher.media_event(
        MEDIA_PROCESSING_FAILED,
        ASSET_A,
        {
            "stage": "thumbnail",
            "error_code": "decoder_error",
            "detail": "cannot decode",
            "session_id": "session-1",
        },
        correlation_id="run-1",
        causation_id=started.event_id if started else "",
    )
    assert started is not None and failed is not None
    assert failed.causation_id == started.event_id
    assert failed.payload["error_code"] == "decoder_error"


def test_publisher_rejects_invalid_payloads(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store)
    with pytest.raises(EventPayloadError):
        publisher.media_received({**MEDIA_VIEW, "kind": "hologram"})
    with pytest.raises(EventPayloadError):
        publisher.media_event(MEDIA_PROCESSING_FAILED, "nothex", {"stage": "metadata"})


def test_safe_publish_swallows_and_logs_errors(caplog):
    def failing() -> None:
        raise EventPayloadError("bad payload")

    with caplog.at_level("WARNING"):
        safe_publish(failing, context="test")
    assert any("multimodal event rejected" in record.message for record in caplog.records)


def test_event_ordering_and_correlation(store: DomainEventStore):
    publisher = MultimodalEventPublisher(store)
    publisher.message_received(
        "session-1", message_id="run-1", media_ids=[], source="web", correlation_id="run-1"
    )
    publisher.media_received(MEDIA_VIEW, source="web", correlation_id="run-1")
    publisher.media_event(
        MEDIA_VALIDATION_STARTED, ASSET_A, {"session_id": "session-1"}, correlation_id="run-1"
    )
    events = store.list_events(limit=10)
    assert sorted(event.event_type for event in events) == sorted(
        [
            MESSAGE_RECEIVED,
            MEDIA_RECEIVED,
            IMAGE_RECEIVED,
            MEDIA_VALIDATION_STARTED,
        ]
    )
    correlated = [event for event in events if event.correlation_id == "run-1"]
    assert len(correlated) == 4


def test_gateway_upload_publishes_media_events(tmp_path: Path, monkeypatch):
    import io

    from fastapi.testclient import TestClient

    from eurus.config import load_config
    from eurus.gateway.web.app import build_context, create_app
    from eurus.gateway.web.auth import GatewaySecurity

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc8\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer unit-test-token"}
        session_id = client.post("/api/sessions", json={"title": "s"}, headers=auth).json()["id"]
        response = client.post(
            "/api/media",
            data={"session_id": session_id},
            files={"file": ("photo.png", io.BytesIO(png), "image/png")},
            headers=auth,
        )
        assert response.status_code in (200, 201)
        media_id = response.json()["id"]
        store = DomainEventStore(ctx.store.path)
        events = store.list_events(limit=10)
        types = [event.event_type for event in events]
        assert MEDIA_RECEIVED in types
        assert IMAGE_RECEIVED in types
        assert MEDIA_PROCESSING_STARTED in types
        media_events = [
            event
            for event in events
            if event.subject_id == media_id and event.event_type in (MEDIA_RECEIVED, IMAGE_RECEIVED)
        ]
        assert len(media_events) == 2
        for event in media_events:
            assert event.payload["mime"] == "image/png"
            assert event.payload["kind"] == "image"
            assert event.payload["size"] == len(png)
        started = [
            event
            for event in events
            if event.subject_id == media_id and event.event_type == MEDIA_PROCESSING_STARTED
        ]
        assert len(started) == 1
        assert started[0].payload["stage"] == "metadata"
        assert all("bytes" not in str(event.payload) for event in events)
    asyncio.run(ctx.runtime.stop())


def test_engine_translates_multimodal_agent_events(tmp_path: Path):
    from eurus.agent import Agent
    from eurus.providers.testing import openai_stream, scripted_router
    from eurus.runtime.engine import AgentEngine
    from eurus.storage.events import EventStore
    from eurus.tools import ToolContext, default_registry

    router, _ = scripted_router(streams=[openai_stream("done")])
    agent = Agent(
        router,
        default_registry(),
        ToolContext(workspace=Path("."), max_output=1000, timeout=5.0),
        system_prompt="sys",
        max_steps=10,
        stream=True,
    )
    engine = AgentEngine(agent, event_store=EventStore(tmp_path / "events.db"))
    block = ImageBlock(asset_id=ASSET_A, mime_type="image/png")
    extra_events = [
        AgentEvent.content_block_started(0, TextBlock("look")),
        AgentEvent.content_block_delta(0, "look"),
        AgentEvent.content_block_completed(0, TextBlock("look")),
        AgentEvent.media_input_analyzing(ASSET_A, "image"),
        AgentEvent.audio_output_started(ASSET_B),
        AgentEvent.audio_output_delta("spoken "),
        AgentEvent.content_block_started(1, block),
        AgentEvent.content_block_completed(1, block),
        AgentEvent.artifact_lifecycle(ASSET_B, "created"),
        AgentEvent.artifact_lifecycle(ASSET_B, "ready"),
    ]

    async def go():
        async def recorder(event):
            pass

        original_run = agent.run

        async def run_with_extra(text, *, blocks=()):
            for extra in extra_events:
                yield extra
            async for event in original_run(text, blocks=blocks):
                yield event

        agent.run = run_with_extra
        return await engine.run("objective", on_event=recorder)

    result = asyncio.run(go())
    types = [event.event_type for event in result.runtime_events]
    assert "content_block.started" in types
    assert "content_block.delta" in types
    assert "content_block.completed" in types
    assert "media.input.analyzing" in types
    assert "audio_output.started" in types
    assert "audio_output.delta" in types
    assert "artifact.created" in types
    assert "artifact.ready" in types
    block_event = next(e for e in result.runtime_events if e.event_type == "media.input.analyzing")
    assert block_event.payload == {"asset_id": ASSET_A, "media_kind": "image"}
    artifact_event = next(e for e in result.runtime_events if e.event_type == "artifact.ready")
    assert artifact_event.payload == {"artifact_id": ASSET_B}


def test_hub_view_serializes_multimodal_fields():
    from eurus.gateway.web.hub import _agent_event_view

    block = AudioBlock(asset_id=ASSET_A, mime_type="audio/mpeg")
    event = AgentEvent.content_block_started(2, block)
    view = _agent_event_view(event)
    assert view["kind"] == "content_block_started"
    assert view["block_index"] == 2
    assert view["block"] == {"type": "audio", "asset_id": ASSET_A, "mime_type": "audio/mpeg"}
    analyzing = AgentEvent.media_input_analyzing(ASSET_B, "video")
    view = _agent_event_view(analyzing)
    assert view["asset_id"] == ASSET_B
    assert view["media_kind"] == "video"
    artifact = AgentEvent.artifact_lifecycle(ASSET_B, "failed")
    view = _agent_event_view(artifact)
    assert view["artifact_id"] == ASSET_B
    assert view["artifact_status"] == "failed"
    legacy_view = _agent_event_view(AgentEvent.text_delta("hi"))
    assert legacy_view == {"kind": "text", "step": 0, "text": "hi"}


def test_event_bridge_relays_multimodal_events():
    from eurus.runtime.event_bridge import AgentEventBridge

    bridge = AgentEventBridge()
    subscriber_id, queue = bridge.subscribe("session-1")
    block = ImageBlock(asset_id=ASSET_A, mime_type="image/png")
    events = [
        AgentEvent.content_block_started(0, block),
        AgentEvent.content_block_delta(0, "hi"),
        AgentEvent.content_block_completed(0, block),
    ]
    for event in events:
        asyncio.run(bridge.publish("session-1", event, task_id="task-1"))
    relayed = [asyncio.run(queue.get()).event for _ in events]
    assert [event.kind for event in relayed] == [
        "content_block_started",
        "content_block_delta",
        "content_block_completed",
    ]
    assert relayed[0].block == block
    bridge.close()
