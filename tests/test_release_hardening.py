from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.coding.paths import git_child_env
from eurus.config import load_config
from eurus.gateway.web.app import (
    MAX_WEBSOCKET_CLIENTS,
    SlidingWindowRateLimiter,
    build_context,
    create_app,
)
from eurus.gateway.web.auth import GatewaySecurity
from eurus.gateway.web.service import REDACTED_VALUE, _redact_config_view
from eurus.media import MediaStore


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8713)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    with TestClient(app) as test_client:
        yield test_client, ctx
    asyncio.run(ctx.runtime.stop())


@pytest.fixture()
def client(gateway):
    return gateway[0]


AUTH = {"Authorization": "Bearer unit-test-token"}


class TestRateLimiter:
    def test_allows_under_limit_and_blocks_at_limit(self):
        limiter = SlidingWindowRateLimiter(window_seconds=60.0, limit=3)
        assert limiter.allow("token")
        assert limiter.allow("token")
        assert limiter.allow("token")
        assert not limiter.allow("token")
        assert not limiter.allow("token")

    def test_recovery_after_window(self):
        limiter = SlidingWindowRateLimiter(window_seconds=0.05, limit=1)
        base = 1000.0
        assert limiter.allow("token", now=base)
        assert not limiter.allow("token", now=base + 0.01)
        assert limiter.allow("token", now=base + 10.0)

    def test_api_returns_429_when_limit_breached(self, client: TestClient):
        burst = SlidingWindowRateLimiter(window_seconds=60.0, limit=240)
        overflow = 0
        for _ in range(300):
            response = client.get("/api/sessions", headers=AUTH)
            if response.status_code == 429:
                overflow += 1
                assert response.headers["Retry-After"]
        assert overflow >= 55
        assert burst.allow("unused")


class TestWebSocketCap:
    def test_cap_constant_is_small(self):
        assert 1 <= MAX_WEBSOCKET_CLIENTS <= 64


class TestHtmlDisposition:
    def test_html_media_is_served_as_attachment(self, client: TestClient):
        created = client.post("/api/sessions", json={"title": "media"}, headers=AUTH)
        session_id = created.json()["id"]
        upload = client.post(
            "/api/media",
            headers=AUTH,
            data={"session_id": session_id},
            files={"file": ("page.html", b"<html><body>hi</body></html>", "text/html")},
        )
        assert upload.status_code == 201
        media_id = upload.json()["id"]
        content = client.get(f"/api/media/{media_id}/content", headers=AUTH)
        assert content.status_code == 200
        assert content.headers["content-disposition"].startswith("attachment")

    def test_text_media_is_served_inline(self, client: TestClient):
        created = client.post("/api/sessions", json={"title": "media2"}, headers=AUTH)
        session_id = created.json()["id"]
        upload = client.post(
            "/api/media",
            headers=AUTH,
            data={"session_id": session_id},
            files={"file": ("note.txt", b"plain notes", "text/plain")},
        )
        media_id = upload.json()["id"]
        content = client.get(f"/api/media/{media_id}/content", headers=AUTH)
        assert content.headers["content-disposition"].startswith("inline")


class TestErrorLeakage:
    async def test_internal_errors_are_generic(self, gateway):
        test_client, _ctx = gateway
        handler = test_client.app.exception_handlers[Exception]
        response = await handler(None, OSError("/home/owner/secret/path failed"))
        assert response.status_code == 500
        body = response.body.decode("utf-8")
        assert "/home/owner" not in body
        assert "secret/path" not in body
        assert "see server logs" in body


class TestConfigRedaction:
    def test_all_provider_header_values_are_redacted(self):
        view = {
            "providers": {
                "dummy": {
                    "extra_headers": [
                        ["Authorization", "Bearer sk-live-123456"],
                        ["X-Auth-Key", "value-that-looks-secret"],
                        ["X-Custom", "harmless"],
                    ]
                }
            }
        }
        redacted = _redact_config_view(view)
        headers = redacted["providers"]["dummy"]["extra_headers"]
        assert all(value == REDACTED_VALUE for _name, value in headers)


class TestMediaFileModes:
    def test_saved_media_uses_private_modes(self, tmp_path: Path):
        database = tmp_path / "media.sqlite3"
        root = tmp_path / "media-root"
        store = MediaStore(database=database, root=root)
        saved = store.save(
            session_id="sess-modes",
            filename="note.txt",
            data=b"hello",
            declared_mime="text/plain",
        )
        payload = root / "sess-modes" / f"{saved.id}.txt"
        file_mode = stat.S_IMODE(payload.stat().st_mode)
        dir_mode = stat.S_IMODE(payload.parent.stat().st_mode)
        assert file_mode == 0o600
        assert dir_mode == 0o700


class TestGitChildEnv:
    def test_git_children_do_not_see_secrets_or_overrides(self, monkeypatch):
        monkeypatch.setenv("EURUS_MCP_SERVER_TOKEN", "topsecret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proxy-key")
        monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
        monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")
        env = git_child_env()
        assert "EURUS_MCP_SERVER_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            assert name not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "PATH" in env

    def test_git_children_inherit_path_and_home(self):
        env = git_child_env()
        assert "PATH" in env
        assert "HOME" in env or os.name == "nt"
