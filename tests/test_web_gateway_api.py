"""API surface tests for the web gateway service layer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity
from eurus.gateway.web.hub import is_active
from eurus.runtime.models import Run, RunStatus


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
def client(gateway):  # noqa: ANN001
    return gateway[0]


AUTH = {"Authorization": "Bearer unit-test-token"}


class TestSessions:
    def test_create_list_and_delete(self, client: TestClient):
        created = client.post("/api/sessions", json={"title": "Planning"}, headers=AUTH)
        assert created.status_code == 201
        session = created.json()
        assert session["title"] == "Planning"
        assert session["message_count"] == 0

        listed = client.get("/api/sessions", headers=AUTH).json()
        assert [item["id"] for item in listed] == [session["id"]]

        deleted = client.delete(f"/api/sessions/{session['id']}", headers=AUTH)
        assert deleted.json()["deleted"] is True
        assert client.get("/api/sessions", headers=AUTH).json() == []

    def test_messages_of_unknown_session_404(self, client: TestClient):
        response = client.get("/api/sessions/nope/messages", headers=AUTH)
        assert response.status_code == 404

    def test_empty_message_rejected(self, client: TestClient):
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        response = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "  "}, headers=AUTH
        )
        assert response.status_code == 422


class TestRuns:
    def test_unknown_run_404(self, client: TestClient):
        assert client.get("/api/runs/missing", headers=AUTH).status_code == 404
        assert client.get("/api/runs/missing/events", headers=AUTH).status_code == 404
        assert client.post("/api/runs/missing/cancel", headers=AUTH).status_code == 404

    def test_run_listing_shape(self, client: TestClient):
        assert client.get("/api/runs", headers=AUTH).json() == []


class TestConfig:
    def test_read_returns_sections_and_revision(self, client: TestClient):
        data = client.get("/api/config", headers=AUTH).json()
        assert data["revision"] >= 1
        assert "agent" in data["editable_sections"]
        assert data["config"]["agent"]["model"] == "dummy/dummy"

    def test_patch_updates_user_toml(self, client: TestClient, tmp_path: Path):
        response = client.patch(
            "/api/config",
            json={"section": "network", "values": {"timeout_seconds": 60.0}},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] is True
        assert body["live_reload"] is True
        config_text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "timeout_seconds = 60.0" in config_text

    def test_patch_invalid_value_rejected(self, client: TestClient):
        response = client.patch(
            "/api/config",
            json={"section": "agent", "values": {"temperature": "way-off"}},
            headers=AUTH,
        )
        assert response.status_code == 422
        assert "rejected" in response.json()["error"]["detail"]

    def test_patch_unknown_section_rejected(self, client: TestClient):
        response = client.patch(
            "/api/config",
            json={"section": "pricing", "values": {"models": {}}},
            headers=AUTH,
        )
        assert response.status_code == 422

    def test_patch_provider_add_and_remove(self, client: TestClient, tmp_path: Path):
        added = client.patch(
            "/api/config",
            json={
                "section": "providers",
                "values": {"custom": {"protocol": "openai", "base_url": "https://x/v1"}},
            },
            headers=AUTH,
        )
        assert added.status_code == 200
        assert "custom" in (tmp_path / "config.toml").read_text(encoding="utf-8")
        removed = client.patch(
            "/api/config",
            json={"section": "providers", "values": {"custom": None}},
            headers=AUTH,
        )
        assert removed.status_code == 200
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "[providers.custom]" not in text


class TestProvidersToolsMemory:
    def test_providers_listing(self, client: TestClient):
        providers = client.get("/api/providers", headers=AUTH).json()
        names = {item["name"] for item in providers}
        assert "dummy" in names
        dummy = next(item for item in providers if item["name"] == "dummy")
        assert dummy["status"] in {"ready", "missing key"}

    def test_tools_listing_contains_registry(self, client: TestClient):
        tools = client.get("/api/tools", headers=AUTH).json()
        names = {tool["name"] for tool in tools}
        assert {"calculate", "read_file", "create_file"} <= names
        create_file = next(tool for tool in tools if tool["name"] == "create_file")
        assert create_file["mutates"] is True
        assert create_file["requires_approval"] is True

    def test_memory_add_list_delete(self, client: TestClient):
        added = client.post(
            "/api/memory",
            json={"content": "Prefers concise answers."},
            headers=AUTH,
        )
        assert added.status_code == 201
        memory = added.json()
        assert memory["trust"] == "user_approved"
        listed = client.get("/api/memory", headers=AUTH).json()
        assert any(item["id"] == memory["id"] for item in listed)
        deleted = client.delete(f"/api/memory/{memory['id']}", headers=AUTH)
        assert deleted.json()["deleted"] is True

    def test_memory_search(self, client: TestClient):
        client.post(
            "/api/memory", json={"content": "Zebra logistics handled by Dana."}, headers=AUTH
        )
        results = client.get("/api/memory?query=zebra+logistics", headers=AUTH).json()
        assert any("Zebra" in item["content"] for item in results)


class TestLogsAndDashboards:
    def test_logs_endpoint(self, client: TestClient):
        assert client.get("/api/logs", headers=AUTH).status_code == 200

    def test_dashboard_shape(self, client: TestClient):
        data = client.get("/api/dashboard", headers=AUTH).json()
        assert "runtime" in data and "recent_sessions" in data and "diagnostics" in data

    def test_runtime_shape(self, client: TestClient):
        data = client.get("/api/runtime", headers=AUTH).json()
        assert data["active_runs"] == []
        assert data["runtime_health"]["started"] is True

    def test_static_spa_fallback(self, client: TestClient):
        response = client.get("/some/spa/route")
        assert response.status_code == 200


class TestActiveRunHeuristics:
    def test_is_active_window(self):
        run = Run(owner_id="web", objective="x", status=RunStatus.RUNNING)
        assert is_active(run) is True

    def test_stale_running_run_is_not_active(self):

        run = Run(owner_id="web", objective="x", status=RunStatus.RUNNING)
        future = run.updated_at.timestamp() + 3 * 3600
        assert is_active(run, now=future) is False

    def test_terminal_run_never_active(self):
        run = Run(owner_id="web", objective="x", status=RunStatus.COMPLETED)
        assert is_active(run) is False
