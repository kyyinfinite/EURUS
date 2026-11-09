import asyncio
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.content import ImageBlock, referenced_asset_ids
from eurus.events.store import DomainEventStore
from eurus.events.types import IMAGE_RECEIVED, MEDIA_PROCESSING_STARTED, MEDIA_RECEIVED
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity
from eurus.models import ModelRef
from eurus.providers.model_capabilities import (
    CapabilityRegistry,
    plan_routing,
    required_modalities,
)

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)

AUTH = {"Authorization": "Bearer unit-test-token"}


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    with TestClient(app) as test_client:
        yield test_client, ctx
    asyncio.run(ctx.runtime.stop())


def test_full_multimodal_foundation_flow(gateway):
    client, ctx = gateway
    session_id = client.post("/api/sessions", json={"title": "flow"}, headers=AUTH).json()["id"]

    upload = client.post(
        "/api/media",
        data={"session_id": session_id},
        files={"file": ("photo.png", io.BytesIO(PNG_BYTES), "image/png")},
        headers=AUTH,
    )
    assert upload.status_code in (200, 201)
    media_view = upload.json()
    media_id = media_view["id"]
    assert media_view["status"] == "ready"
    assert media_view["metadata"]["width"] == 1
    assert media_view["metadata"]["height"] == 1

    from eurus.media import MediaStore

    resolved_store = MediaStore(database=ctx.store.path)
    stored = resolved_store.get(media_id)
    assert stored is not None
    assert stored.status == "ready"
    assert stored.owner_id == "eurus"
    assert Path(stored.storage_path).is_file()

    domain_events = DomainEventStore(ctx.store.path)
    events = domain_events.list_events(limit=10)
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
    assert all(event.payload["size"] == len(PNG_BYTES) for event in media_events)
    assert all(event.schema_version == 1 for event in media_events)

    send = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"text": "what is in this image?", "attachments": [media_id]},
        headers=AUTH,
    )
    assert send.status_code == 202
    run_id = send.json()["run_id"]

    messages = []
    for _ in range(60):
        messages = ctx.store.load_messages(session_id)
        if messages:
            break
        time.sleep(0.1)
    user_message = next(m for m in messages if m.role == "user")
    assert user_message.blocks
    block = user_message.blocks[0]
    assert isinstance(block, ImageBlock)
    assert block.asset_id == media_id
    assert block.mime_type == "image/png"
    assert "what is in this image?" in user_message.content
    assert "[attach id=" in user_message.content

    assert referenced_asset_ids(user_message.blocks) == (media_id,)
    referenced = resolved_store.get(referenced_asset_ids(user_message.blocks)[0])
    assert referenced is not None
    assert referenced.session_id == session_id

    modalities = required_modalities(user_message.blocks)
    assert modalities == ("text", "image")

    registry = CapabilityRegistry()
    text_only_chain = [ModelRef.parse("deepseek/deepseek-chat")]
    decision = plan_routing(text_only_chain, modalities, registry=registry)
    assert not decision.ok
    error = decision.error()
    assert error.required == ("text", "image")
    assert error.candidates[0][1] == ("image",)

    vision_chain = [
        ModelRef.parse("deepseek/deepseek-chat"),
        ModelRef.parse("openai/gpt-4o"),
    ]
    vision_decision = plan_routing(vision_chain, modalities, registry=registry)
    assert vision_decision.ok
    assert vision_decision.selected == ModelRef.parse("openai/gpt-4o")

    runs = client.get("/api/runs", headers=AUTH).json()
    run_rows = runs.get("items", []) if isinstance(runs, dict) else runs
    assert any(run.get("run_id") == run_id for run in run_rows)

    body = user_message.to_dict()
    serialized = str(body)
    assert "base64" not in serialized
    assert "\x89PNG" not in serialized
