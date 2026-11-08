from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import eurus.gateway.web.app as app_module
from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app, resolve_static_dir
from eurus.gateway.web.auth import GatewaySecurity

AUTH = {"Authorization": "Bearer unit-test-token"}


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    monkeypatch.delenv("EURUS_GATEWAY_STATIC", raising=False)
    (tmp_path / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    yield ctx
    import asyncio

    asyncio.run(ctx.runtime.stop())


def make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=app></div></body></html>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "assets" / "style.css").write_text("body{color:#111}", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (dist / "secret.txt").write_text("top secret", encoding="utf-8")
    return dist


def fake_installation(tmp_path: Path, with_static: bool) -> Path:
    package_root = tmp_path / "site" / "eurus" / "gateway" / "web"
    package_root.mkdir(parents=True)
    (tmp_path / "site" / "eurus" / "__init__.py").write_text("", encoding="utf-8")
    if with_static:
        static = package_root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    return package_root / "app.py"


class TestStaticResolution:
    def test_environment_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        external = make_dist(tmp_path / "elsewhere")
        monkeypatch.setenv("EURUS_GATEWAY_STATIC", str(external))
        dist, source = resolve_static_dir()
        assert source == "environment"
        assert dist == external.resolve()

    def test_environment_relative_path_resolves_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=False)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        external = make_dist(tmp_path / "elsewhere")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EURUS_GATEWAY_STATIC", "elsewhere/dist")
        dist, source = resolve_static_dir()
        assert source == "environment"
        assert dist == external.resolve()

    def test_environment_override_without_index_html_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        empty = tmp_path / "empty-build"
        empty.mkdir()
        monkeypatch.setenv("EURUS_GATEWAY_STATIC", str(empty))
        dist, source = resolve_static_dir()
        assert source == "packaged"
        assert dist is not None and dist.name == "static"

    def test_environment_override_missing_directory_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        monkeypatch.setenv("EURUS_GATEWAY_STATIC", str(tmp_path / "does-not-exist"))
        dist, source = resolve_static_dir()
        assert source == "packaged"

    def test_explicit_parameter_wins_over_packaged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        explicit = make_dist(tmp_path / "explicit-build")
        dist, source = resolve_static_dir(explicit)
        assert source == "explicit"
        assert dist == explicit.resolve()

    def test_packaged_assets_found_in_installed_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        dist, source = resolve_static_dir()
        assert source == "packaged"
        assert dist == (tmp_path / "site" / "eurus" / "gateway" / "web" / "static").resolve()

    def test_repository_layout_used_when_no_packaged_assets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        package_root = tmp_path / "src" / "eurus" / "gateway" / "web"
        package_root.mkdir(parents=True)
        make_dist(tmp_path / "frontend")
        monkeypatch.setattr(app_module, "__file__", str(package_root / "app.py"))
        dist, source = resolve_static_dir()
        assert source == "repository"
        assert dist == (tmp_path / "frontend" / "dist").resolve()

    def test_unavailable_when_nothing_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake_app = fake_installation(tmp_path, with_static=False)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        dist, source = resolve_static_dir()
        assert dist is None
        assert source == "unavailable"

    def test_resolution_independent_of_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        monkeypatch.chdir(tmp_path / "site")
        dist, source = resolve_static_dir()
        assert source == "packaged"
        assert dist is not None and dist.is_dir()

    def test_invalid_explicit_parameter_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_app = fake_installation(tmp_path, with_static=True)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        dist, source = resolve_static_dir(tmp_path / "nope")
        assert source == "packaged"


class TestStaticServing:
    @pytest.fixture()
    def client(self, gateway, tmp_path: Path) -> TestClient:
        dist = make_dist(tmp_path / "ui")
        app = create_app(gateway, static_dir=dist)
        with TestClient(app) as test_client:
            yield test_client

    def test_valid_frontend_route_serves_index(self, client: TestClient):
        response = client.get("/sessions/abc", follow_redirects=False)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "id=app" in response.text

    def test_root_serves_index(self, client: TestClient):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_static_asset_content_types(self, client: TestClient):
        js = client.get("/assets/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        css = client.get("/assets/style.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        svg = client.get("/favicon.svg")
        assert svg.status_code == 200
        assert "svg" in svg.headers["content-type"]

    def test_unknown_asset_returns_404_json(self, client: TestClient):
        response = client.get("/assets/missing.js")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_unknown_frontend_route_falls_back_to_index(self, client: TestClient):
        response = client.get("/totally/unknown/route", follow_redirects=False)
        assert response.status_code == 200
        assert "id=app" in response.text

    def test_encoded_path_traversal_blocked(self, client: TestClient):
        response = client.get("/%2e%2e/secret.txt")
        assert response.status_code in {404, 200}
        assert "top secret" not in response.text
        nested = client.get("/assets/%2e%2e/%2e%2e/secret.txt")
        assert "top secret" not in nested.text

    def test_symlink_escape_blocked(self, client: TestClient, tmp_path: Path):
        dist = tmp_path / "ui" / "dist"
        link = dist / "escape.js"
        target = tmp_path / "outside.js"
        target.write_text("leak", encoding="utf-8")
        link.symlink_to(target)
        response = client.get("/escape.js")
        assert response.status_code == 404

    def test_api_route_still_works(self, client: TestClient):
        response = client.get("/api/health", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_unknown_api_route_returns_404_not_index(self, client: TestClient):
        response = client.get("/api/definitely-not-a-route", headers=AUTH)
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"]["code"] == "not_found"
        assert "html" not in response.headers.get("content-type", "")

    def test_unknown_api_route_without_auth_returns_401(self, client: TestClient):
        response = client.get("/api/definitely-not-a-route")
        assert response.status_code == 401

    def test_health_reports_web_ui_status(self, client: TestClient, tmp_path: Path):
        response = client.get("/api/health", headers=AUTH)
        web_ui = response.json()["web_ui"]
        assert web_ui["available"] is True
        assert web_ui["source"] == "explicit"
        assert web_ui["path"] == str((tmp_path / "ui" / "dist").resolve())


class TestStaticUnavailable:
    @pytest.fixture()
    def unavailable_client(self, gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake_app = fake_installation(tmp_path, with_static=False)
        monkeypatch.setattr(app_module, "__file__", str(fake_app))
        app = create_app(gateway)
        with TestClient(app) as test_client:
            yield test_client

    def test_frontend_missing_returns_actionable_503(self, unavailable_client: TestClient):
        response = unavailable_client.get("/some/spa/route", follow_redirects=False)
        assert response.status_code == 503
        assert "EURUS_GATEWAY_STATIC" in response.text
        assert "npm run build" in response.text

    def test_api_works_without_frontend(self, unavailable_client: TestClient):
        response = unavailable_client.get("/api/health", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["web_ui"]["available"] is False
        assert body["web_ui"]["source"] == "unavailable"

    def test_unknown_api_route_is_404_without_frontend(self, unavailable_client: TestClient):
        response = unavailable_client.get("/api/missing", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_assets_404_without_frontend(self, unavailable_client: TestClient):
        response = unavailable_client.get("/assets/app.js", follow_redirects=False)
        assert response.status_code == 503
