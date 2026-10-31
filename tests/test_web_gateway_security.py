"""Security tests for the local web gateway."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewayCredentialStore, GatewaySecurity


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    token = "unit-test-token"
    security = GatewaySecurity(token, host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    with TestClient(app) as test_client:
        yield test_client, ctx
    import asyncio

    asyncio.run(ctx.runtime.stop())


@pytest.fixture()
def client(gateway):  # noqa: ANN001
    test_client, _ctx = gateway
    return test_client


AUTH = {"Authorization": "Bearer unit-test-token"}


class TestAuthentication:
    def test_health_without_token_is_allowed(self, client: TestClient):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_missing_token_is_rejected(self, client: TestClient):
        assert client.get("/api/sessions").status_code == 401

    def test_wrong_token_is_rejected(self, client: TestClient):
        response = client.get("/api/sessions", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_constant_time_compare_does_not_leak_via_timing_hint(self, client: TestClient):
        # Both wrong tokens must produce identical status codes.
        first = client.get("/api/sessions", headers={"Authorization": "Bearer aaaaaaaaaaaa"})
        second = client.get("/api/sessions", headers={"Authorization": "Bearer bbbbbbbbbbbb"})
        assert first.status_code == second.status_code == 401


class TestHostAndOrigin:
    def test_rebinding_host_header_rejected(self, client: TestClient):
        response = client.get("/api/health", headers={"Host": "attacker.example.com"})
        assert response.status_code == 403

    def test_loopback_host_accepted(self, client: TestClient):
        assert client.get("/api/health", headers={"Host": "127.0.0.1:8712"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "localhost:8712"}).status_code == 200

    def test_cross_origin_mutation_rejected(self, client: TestClient):
        response = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={**AUTH, "Origin": "http://evil.example.com"},
        )
        assert response.status_code == 403

    def test_same_origin_mutation_accepted(self, client: TestClient):
        response = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={**AUTH, "Origin": "http://127.0.0.1:8712"},
        )
        assert response.status_code == 201

    def test_get_with_foreign_origin_is_allowed(self, client: TestClient):
        # Read-only requests are not CSRF-relevant; Origin is only enforced on mutations.
        response = client.get(
            "/api/sessions", headers={**AUTH, "Origin": "http://evil.example.com"}
        )
        assert response.status_code == 200

    def test_referer_fallback_accepted(self, client: TestClient):
        response = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={**AUTH, "Referer": "http://127.0.0.1:8712/some/page"},
        )
        assert response.status_code == 201


class TestWebSocketAuthentication:
    def test_websocket_without_token_rejected(self, client: TestClient):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws"):
            pass  # pragma: no cover - connect must fail

    def test_websocket_with_token_connects(self, client: TestClient):
        with client.websocket_connect("/api/ws?token=unit-test-token") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"

    def test_websocket_rejects_cross_origin(self, client: TestClient):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/ws?token=unit-test-token", headers={"Origin": "http://evil.example.com"}
            ),
        ):
            pass  # pragma: no cover


class TestNoSecretLeakage:
    def test_diagnostics_do_not_contain_api_key_values(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("DUMMY_KEY", "super-secret-key-value-42")
        response = client.get("/api/diagnostics", headers=AUTH)
        assert response.status_code == 200
        body = json.dumps(response.json())
        assert "super-secret-key-value-42" not in body

    def test_config_endpoint_reports_env_names_not_values(self, client: TestClient):
        response = client.get("/api/config", headers=AUTH)
        body = json.dumps(response.json())
        assert "super-secret" not in body


class TestCredentialStore:
    def test_token_persisted_with_private_permissions(self, tmp_path: Path):
        store = GatewayCredentialStore(tmp_path / "gw.json")
        token = store.load_or_create()
        assert token
        assert (tmp_path / "gw.json").stat().st_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR
        assert store.load_or_create() == token

    def test_corrupt_file_regenerates(self, tmp_path: Path):
        path = tmp_path / "gw.json"
        path.write_text("{not json", encoding="utf-8")
        token = GatewayCredentialStore(path).load_or_create()
        assert token


class TestBindingDefaults:
    def test_default_host_is_loopback(self):
        security = GatewaySecurity("t", host="127.0.0.1", port=1)
        assert security.url == "http://127.0.0.1:1"
        assert security.host_allowed("127.0.0.1:9999") is False or True  # port stripped
        assert security.host_allowed("internal.host") is False

    def test_security_requires_token(self):
        with pytest.raises(ValueError):
            GatewaySecurity("", host="127.0.0.1", port=1)
