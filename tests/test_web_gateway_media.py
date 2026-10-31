"""Media pipeline tests: upload validation, ownership, message composition."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc8\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


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

MARKER_RE = re.compile(r"\[attach id=([a-f0-9]+) kind=(\w+) name=(\S+) mime=(\S+) size=(\d+)\]")


def _make_session(client: TestClient, title: str = "Media session") -> str:
    return client.post("/api/sessions", json={"title": title}, headers=AUTH).json()["id"]


def _upload(client: TestClient, session_id: str, filename: str, data: bytes, mime: str):
    return client.post(
        "/api/media",
        data={"session_id": session_id},
        files={"file": (filename, io.BytesIO(data), mime)},
        headers=AUTH,
    )


class TestUpload:
    def test_upload_png_returns_media_view(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(client, session_id, "photo.png", PNG_BYTES, "image/png")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "image"
        assert body["mime"] == "image/png"
        assert body["size"] == len(PNG_BYTES)
        assert body["session_id"] == session_id
        assert body["url"].endswith(f"/api/media/{body['id']}/content")

    def test_upload_rejects_unknown_session(self, client: TestClient):
        response = _upload(client, "missing", "x.png", PNG_BYTES, "image/png")
        assert response.status_code == 404

    def test_upload_rejects_disallowed_type(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(
            client, session_id, "evil.exe", b"MZ\x90\x00binary", "application/octet-stream"
        )
        assert response.status_code in (400, 422)
        assert "unsupported" in response.json()["error"]["detail"].lower()

    def test_upload_rejects_oversize(self, client: TestClient, monkeypatch):
        session_id = _make_session(client)
        from eurus import media as media_module

        monkeypatch.setattr(media_module, "MAX_MEDIA_BYTES", 16)
        response = _upload(client, session_id, "big.png", PNG_BYTES + b"\x00" * 64, "image/png")
        assert response.status_code in (400, 413, 422)

    def test_upload_sniffs_mismatched_content_type(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(client, session_id, "notes.txt", b"plain text body", "image/png")
        assert response.status_code == 201
        assert response.json()["mime"] == "text/plain"
        assert response.json()["kind"] == "document"

    def test_upload_sanitizes_filename(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(client, session_id, "../../etc/passwd.png", PNG_BYTES, "image/png")
        assert response.status_code == 201
        assert "/" not in response.json()["filename"]
        assert ".." not in response.json()["filename"]


class TestContentAccess:
    def test_content_served_with_mime(self, client: TestClient):
        session_id = _make_session(client)
        media = _upload(client, session_id, "photo.png", PNG_BYTES, "image/png").json()
        response = client.get(f"/api/media/{media['id']}/content", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.content == PNG_BYTES

    def test_content_requires_token(self, client: TestClient):
        session_id = _make_session(client)
        media = _upload(client, session_id, "photo.png", PNG_BYTES, "image/png").json()
        assert client.get(f"/api/media/{media['id']}/content").status_code == 401

    def test_unknown_media_404(self, client: TestClient):
        assert client.get("/api/media/deadbeef", headers=AUTH).status_code == 404
        assert client.get("/api/media/deadbeef/content", headers=AUTH).status_code == 404

    def test_delete_media(self, client: TestClient):
        session_id = _make_session(client)
        media = _upload(client, session_id, "photo.png", PNG_BYTES, "image/png").json()
        assert client.delete(f"/api/media/{media['id']}", headers=AUTH).status_code == 200
        assert client.get(f"/api/media/{media['id']}", headers=AUTH).status_code == 404


class TestMessageComposition:
    def test_send_message_embeds_attachment_marker(self, client: TestClient, gateway):
        session_id = _make_session(client)
        media = _upload(client, session_id, "photo.png", PNG_BYTES, "image/png").json()
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "what is in this image?", "attachments": [media["id"]]},
            headers=AUTH,
        )
        # The dummy provider endpoint is unreachable; the run fails but the
        # user message (with the composed annotation) is persisted first.
        assert response.status_code == 202
        _ctx = gateway[1]
        import time

        for _ in range(50):
            messages = client.get(f"/api/sessions/{session_id}/messages", headers=AUTH).json()
            if messages:
                break
            time.sleep(0.1)
        user_message = next((m for m in messages if m["role"] == "user"), None)
        assert user_message is not None
        match = MARKER_RE.search(user_message["content"])
        assert match is not None, user_message["content"]
        assert match.group(1) == media["id"]
        assert match.group(2) == "image"
        assert "what is in this image?" in user_message["content"]

    def test_send_message_inlines_text_document(self, client: TestClient):
        session_id = _make_session(client)
        media = _upload(
            client, session_id, "notes.txt", b"hello from a text file", "text/plain"
        ).json()
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "read this", "attachments": [media["id"]]},
            headers=AUTH,
        )
        import time

        for _ in range(50):
            messages = client.get(f"/api/sessions/{session_id}/messages", headers=AUTH).json()
            if messages:
                break
            time.sleep(0.1)
        user_message = next((m for m in messages if m["role"] == "user"), None)
        assert user_message is not None
        assert "hello from a text file" in user_message["content"]
        assert "--- begin notes.txt ---" in user_message["content"]

    def test_attachment_from_other_session_rejected(self, client: TestClient):
        first = _make_session(client, "first")
        second = _make_session(client, "second")
        media = _upload(client, first, "photo.png", PNG_BYTES, "image/png").json()
        response = client.post(
            f"/api/sessions/{second}/messages",
            json={"text": "steal attachment", "attachments": [media["id"]]},
            headers=AUTH,
        )
        assert response.status_code in (400, 403, 404, 422)

    def test_unknown_attachment_rejected(self, client: TestClient):
        session_id = _make_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "hello", "attachments": ["ffffffffffffffffffffffffffffffff"]},
            headers=AUTH,
        )
        assert response.status_code == 404


class TestCodeAndDataUploads:
    """The multimodal file system accepts code + XML sources as documents."""

    def test_upload_python_source(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(client, session_id, "script.py", b"print('hi')\n", "text/x-python")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "document"
        assert body["mime"] == "text/x-python"

    def test_upload_python_source_with_browsers_default_mime(self, client: TestClient):
        """Some browsers send application/octet-stream (or nothing) for .py."""
        session_id = _make_session(client)
        response = _upload(client, session_id, "script.py", b"x = 1\n", "application/octet-stream")
        assert response.status_code == 201, response.text
        assert response.json()["mime"] == "text/x-python"

    def test_upload_xml_document(self, client: TestClient):
        session_id = _make_session(client)
        response = _upload(client, session_id, "data.xml", b"<a><b>1</b></a>", "text/xml")
        assert response.status_code == 201
        assert response.json()["mime"] == "text/xml"

    def test_upload_typescript_and_web_sources(self, client: TestClient):
        session_id = _make_session(client)
        for name, mime in (
            ("app.ts", "text/typescript"),
            ("page.html", "text/html"),
            ("style.css", "text/css"),
            ("query.sql", "application/sql"),
        ):
            response = _upload(client, session_id, name, b"body {}", mime)
            assert response.status_code == 201, (name, response.text)
            assert response.json()["kind"] == "document"

    def test_code_source_is_inlined_for_text_only_providers(self, client: TestClient):
        session_id = _make_session(client)
        media = _upload(
            client, session_id, "tool.py", b"def run():\n    return 42\n", "text/x-python"
        ).json()
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "run this", "attachments": [media["id"]]},
            headers=AUTH,
        )
        import time

        for _ in range(50):
            messages = client.get(f"/api/sessions/{session_id}/messages", headers=AUTH).json()
            if messages:
                break
            time.sleep(0.1)
        user_message = next((m for m in messages if m["role"] == "user"), None)
        assert user_message is not None
        assert "def run():" in user_message["content"]
        assert "--- begin tool.py ---" in user_message["content"]


class TestMediaListing:
    def test_list_media_returns_recent_rows(self, client: TestClient):
        session_id = _make_session(client)
        first = _upload(client, session_id, "a.png", PNG_BYTES, "image/png").json()
        second = _upload(client, session_id, "b.txt", b"text", "text/plain").json()
        response = client.get("/api/media", headers=AUTH)
        assert response.status_code == 200
        rows = response.json()
        ids = [row["id"] for row in rows]
        assert second["id"] in ids and first["id"] in ids
        # newest first
        assert ids.index(second["id"]) < ids.index(first["id"])

    def test_list_media_filters_by_session(self, client: TestClient):
        first = _make_session(client, "first")
        second = _make_session(client, "second")
        _upload(client, first, "a.png", PNG_BYTES, "image/png")
        mine = _upload(client, second, "b.png", PNG_BYTES, "image/png").json()
        rows = client.get(f"/api/media?session_id={second}", headers=AUTH).json()
        assert [row["id"] for row in rows] == [mine["id"]]

    def test_list_media_rejects_unknown_session(self, client: TestClient):
        assert client.get("/api/media?session_id=missing", headers=AUTH).status_code == 404

    def test_list_media_requires_token(self, client: TestClient):
        assert client.get("/api/media").status_code == 401
