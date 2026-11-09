from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from eurus import paths
from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity
from eurus.multiagent.artifacts import ArtifactStore


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


@pytest.fixture()
def client(gateway: tuple[TestClient, Any]) -> TestClient:
    return gateway[0]


AUTH = {"Authorization": "Bearer unit-test-token"}


def _publish_artifact(tmp_path: Path | None, **overrides) -> dict:
    from eurus.owner import OWNER_ID

    store = ArtifactStore(paths.database_path(), paths.home_dir() / "data" / "artifacts")
    record = store.publish(
        overrides.pop("content", b"artifact body"),
        owner_id=OWNER_ID,
        mission_id=overrides.pop("mission_id", "m-1"),
        artifact_type=overrides.pop("artifact_type", "report"),
        name=overrides.pop("name", "report.txt"),
        source_tool=overrides.pop("source_tool", "office_specialist"),
        run_id=overrides.pop("run_id", "run-1"),
        **overrides,
    )
    return record.to_dict()


def test_artifact_listing_and_content_round_trip(client: TestClient, tmp_path: Path) -> None:
    record = _publish_artifact(tmp_path, run_id="run-77", name="report.md")
    listing = client.get("/api/artifacts?run_id=run-77", headers=AUTH).json()
    assert any(item["artifact_id"] == record["artifact_id"] for item in listing["items"])
    assert listing["next_cursor"] is None
    assert listing["total"] == len(listing["items"])
    detail = client.get(f"/api/artifacts/{record['artifact_id']}", headers=AUTH).json()
    assert detail["name"] == "report.md"
    assert detail["source_tool"] == "office_specialist"
    content = client.get(f"/api/artifacts/{record['artifact_id']}/content", headers=AUTH)
    assert content.status_code == 200
    assert content.content == b"artifact body"
    assert content.headers["content-type"].startswith("text/plain")


def test_artifact_content_uses_declared_media_type(client: TestClient, tmp_path: Path) -> None:
    record = _publish_artifact(
        tmp_path, name="report.pdf", content=b"%pdf-body", artifact_type="document"
    )
    response = client.get(f"/api/artifacts/{record['artifact_id']}/content", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_unknown_artifact_returns_404(client: TestClient) -> None:
    assert client.get("/api/artifacts/nope", headers=AUTH).status_code == 404
    assert client.get("/api/artifacts/nope/content", headers=AUTH).status_code == 404


def test_tampered_artifact_content_rejected(client: TestClient, tmp_path: Path) -> None:
    record = _publish_artifact(tmp_path)
    root = paths.home_dir() / "data" / "artifacts"
    for owner_dir in root.iterdir():
        for item in owner_dir.glob("*.bin"):
            item.write_bytes(b"tampered")
    response = client.get(f"/api/artifacts/{record['artifact_id']}/content", headers=AUTH)
    assert response.status_code == 404


def test_expired_artifact_hidden_from_listing(client: TestClient, tmp_path: Path) -> None:
    _publish_artifact(
        tmp_path,
        name="gone.txt",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=20),
    )
    import time

    time.sleep(0.1)
    listing = client.get("/api/artifacts", headers=AUTH).json()
    assert "gone.txt" not in {item["name"] for item in listing["items"]}


def test_artifact_pages_walk_the_registry_through_cursors(client: TestClient) -> None:
    published = [_publish_artifact(None, name=f"page-{index}.txt") for index in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    requests = 0
    while True:
        response = client.get(
            "/api/artifacts",
            params={"limit": 2, **({"cursor": cursor} if cursor is not None else {})},
            headers=AUTH,
        )
        page = response.json()
        requests += 1
        seen.extend(item["artifact_id"] for item in page["items"])
        assert page["total"] == len(published)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert requests <= 5
    assert requests == 3
    assert sorted(seen) == sorted(record["artifact_id"] for record in published)


def test_artifact_page_cursor_is_url_encoded(client: TestClient) -> None:
    _publish_artifact(None, name="first.txt")
    _publish_artifact(None, name="second.txt")
    page = client.get("/api/artifacts?limit=1", headers=AUTH).json()
    assert page["next_cursor"] is not None
    follow = client.get(
        "/api/artifacts?limit=1",
        params={"cursor": page["next_cursor"]},
        headers=AUTH,
    )
    assert follow.status_code == 200
    assert follow.json()["next_cursor"] is None


def test_artifact_page_rejects_malformed_cursor(client: TestClient) -> None:
    _publish_artifact(None, name="first.txt")
    assert client.get("/api/artifacts?cursor=not-a-cursor", headers=AUTH).status_code == 422
    assert client.get("/api/artifacts?cursor=a|", headers=AUTH).status_code == 422


def test_artifact_routes_require_auth(client: TestClient) -> None:
    assert client.get("/api/artifacts").status_code in {401, 403}
