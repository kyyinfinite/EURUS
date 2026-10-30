import asyncio

import pytest

from eurus.bootstrap.integrations.browser import (
    CHROMIUM_KIND_MISSING,
    CHROMIUM_KIND_SYSTEM,
    BrowserSetupReport,
    browser_check_result,
    detect_browser,
    probe_cdp_sync,
    self_test_settings,
)
from eurus.bootstrap.models import BROKEN, MISSING, READY, UNKNOWN


def _environ(tmp_path, **extra):
    env = {"EURUS_HOME": str(tmp_path / "eurus-home")}
    env.update(extra)
    return env


class _FakeConfig:
    def __init__(self, backend="auto", cdp_endpoint="", chromium_executable=""):
        self.browser = self
        self.backend = backend
        self.cdp_endpoint = cdp_endpoint
        self.chromium_executable = chromium_executable
        self.profile_dir = ""


def test_detect_browser_probes_configured_endpoint(tmp_path, monkeypatch):
    calls = []

    async def fake_probe(endpoint, timeout=4.0):
        calls.append(endpoint)
        return True, "cdp reachable: HeadlessChrome"

    monkeypatch.setattr("eurus.bootstrap.integrations.browser.probe_cdp", fake_probe)
    report = detect_browser(
        _environ(tmp_path, EURUS_CDP_ENDPOINT="http://127.0.0.1:9222"),
    )
    assert calls == ["http://127.0.0.1:9222"]
    assert report.cdp_reachable == READY
    assert "reachable" in report.detail


def test_detect_browser_marks_unreachable_endpoint_broken(tmp_path, monkeypatch):
    async def fake_probe(endpoint, timeout=4.0):
        return False, "cdp probe failed: ConnectError"

    monkeypatch.setattr("eurus.bootstrap.integrations.browser.probe_cdp", fake_probe)
    report = detect_browser(
        _environ(tmp_path, EURUS_CDP_ENDPOINT="http://127.0.0.1:9"),
        config=_FakeConfig(),
    )
    assert report.cdp_reachable == BROKEN
    result = browser_check_result(report)
    assert result.status == BROKEN
    assert "cdp probe failed" in result.detail


def test_detect_browser_env_endpoint_wins_over_config(tmp_path, monkeypatch):
    seen = []

    async def fake_probe(endpoint, timeout=4.0):
        seen.append(endpoint)
        return True, "ok"

    monkeypatch.setattr("eurus.bootstrap.integrations.browser.probe_cdp", fake_probe)
    detect_browser(
        _environ(tmp_path, EURUS_CDP_ENDPOINT="http://env:9222"),
        config=_FakeConfig(cdp_endpoint="http://config:9222"),
    )
    assert seen == ["http://env:9222"]


def test_detect_browser_without_endpoint_skips_probe(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("probe must not run without an endpoint")

    monkeypatch.setattr("eurus.bootstrap.integrations.browser.probe_cdp", boom)
    report = detect_browser(_environ(tmp_path))
    assert report.cdp_endpoint == ""
    assert report.cdp_reachable == UNKNOWN


def test_detect_browser_uses_config_chromium_executable(tmp_path):
    executable = tmp_path / "chromium"
    executable.write_text("", encoding="utf-8")
    report = detect_browser(
        _environ(tmp_path), config=_FakeConfig(chromium_executable=str(executable))
    )
    assert report.chromium_path == str(executable)
    assert report.chromium_kind == CHROMIUM_KIND_SYSTEM


def test_probe_cdp_sync_maps_results():
    assert probe_cdp_sync.__name__ == "probe_cdp_sync"


def test_browser_check_result_matrix():
    operational = BrowserSetupReport(
        chromium_kind=CHROMIUM_KIND_SYSTEM,
        chromium_path="/usr/bin/chromium",
        launchable=READY,
    )
    assert browser_check_result(operational).status == READY

    unverified = BrowserSetupReport(
        chromium_kind=CHROMIUM_KIND_SYSTEM,
        chromium_path="/usr/bin/chromium",
        launchable=UNKNOWN,
    )
    result = browser_check_result(unverified)
    assert result.status == UNKNOWN
    assert "not verified" in result.detail

    broken_launch = BrowserSetupReport(
        chromium_kind=CHROMIUM_KIND_SYSTEM,
        chromium_path="/usr/bin/chromium",
        launchable=BROKEN,
    )
    assert browser_check_result(broken_launch).status == BROKEN

    missing = BrowserSetupReport(chromium_kind=CHROMIUM_KIND_MISSING)
    assert browser_check_result(missing).status == MISSING

    cdp_ready = BrowserSetupReport(
        cdp_endpoint="http://127.0.0.1:9222", cdp_reachable=READY, detail="cdp reachable"
    )
    assert browser_check_result(cdp_ready).status == READY

    cdp_broken = BrowserSetupReport(
        cdp_endpoint="http://127.0.0.1:9222", cdp_reachable=BROKEN, detail="cdp probe failed"
    )
    result = browser_check_result(cdp_broken)
    assert result.status == BROKEN
    assert result.hint


def test_self_test_settings_follow_config_and_env():
    settings = self_test_settings(
        {"EURUS_CDP_ENDPOINT": "http://env:9222", "EURUS_CHROMIUM": "/env/chrome"},
        _FakeConfig(
            backend="cdp", cdp_endpoint="http://config:9222", chromium_executable="/cfg/chrome"
        ),
    )
    assert settings.backend == "cdp"
    assert settings.cdp_endpoint == "http://env:9222"
    assert settings.chromium_executable == "/env/chrome"
    assert settings.enabled is True
    assert settings.headless is True


def test_self_test_settings_without_config_defaults_auto():
    settings = self_test_settings({})
    assert settings.backend == "auto"
    assert settings.cdp_endpoint == ""
    assert settings.chromium_executable == ""


def test_self_test_uses_configured_backend(monkeypatch):
    import eurus.browser.manager as manager_module

    captured = {}

    class FakeManager:
        def __init__(self, settings):
            captured["settings"] = settings
            self.backend = FakeBackend()

        async def close_all(self):
            return None

    class FakeBackend:
        name = "cdp"

        async def start(self):
            captured["started"] = True

    monkeypatch.setattr(manager_module, "BrowserManager", FakeManager)
    from eurus.bootstrap.integrations import browser as browser_module

    ok, rows = asyncio.run(
        browser_module.launch_self_test(
            False, {}, _FakeConfig(backend="cdp", cdp_endpoint="http://127.0.0.1:9222")
        )
    )
    assert ok is True
    assert captured["settings"].backend == "cdp"
    assert captured["settings"].cdp_endpoint == "http://127.0.0.1:9222"
    assert captured["started"] is True


def test_self_test_reports_backend_start_failure(monkeypatch):
    import eurus.browser.manager as manager_module

    class FailingBackend:
        name = "cdp"

        async def start(self):
            raise RuntimeError("connection refused")

    class FailingManager:
        def __init__(self, settings):
            self.backend = FailingBackend()

        async def close_all(self):
            return None

    monkeypatch.setattr(manager_module, "BrowserManager", FailingManager)
    from eurus.bootstrap.integrations import browser as browser_module

    ok, rows = asyncio.run(browser_module.launch_self_test(False, {}, _FakeConfig(backend="cdp")))
    assert ok is False
    assert any("failed" in detail for _, detail in rows)


def test_detect_browser_missing_everything_keeps_report_shape(tmp_path):
    report = detect_browser(_environ(tmp_path, EURUS_CHROMIUM=str(tmp_path / "nope")))
    assert report.chromium_kind == CHROMIUM_KIND_MISSING
    assert report.chromium_path == ""
    payload = report.to_dict()
    assert payload["cdp_endpoint"] == ""


@pytest.mark.parametrize(
    "kind,launchable,expected",
    [
        (CHROMIUM_KIND_MISSING, UNKNOWN, MISSING),
        (CHROMIUM_KIND_SYSTEM, READY, READY),
    ],
)
def test_browser_check_result_parametrized(kind, launchable, expected):
    report = BrowserSetupReport(chromium_kind=kind, chromium_path="/x", launchable=launchable)
    assert browser_check_result(report).status == expected


def test_dotenv_cdp_endpoint_flows_into_check(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from click.testing import CliRunner

    from eurus.cli.base import main

    class CdpHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"Browser": "TestChrome/1.0"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), CdpHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".env").write_text(f"EURUS_CDP_ENDPOINT=http://127.0.0.1:{port}\n")
        runner = CliRunner(env={"EURUS_HOME": str(home), "HOME": str(tmp_path)})
        result = runner.invoke(main, ["bootstrap", "--check", "--json"])
        data = json.loads(result.output)
        browser = next(item for item in data["checks"] if item["name"] == "browser")
        assert browser["status"] == "READY", browser
        assert f"http://127.0.0.1:{port}" in browser["detail"]
    finally:
        server.shutdown()
