from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity

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


@pytest.fixture()
def client(gateway):
    return gateway[0]


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


class TestAutonomyApi:
    def test_status_reports_running(self, client: TestClient):
        data = client.get("/api/autonomy", headers=AUTH).json()
        assert data["mode"] in {"owner", "client"}
        assert data["config"]["enabled"] is True
        assert "subsystems" in data

    def test_toggle_subsystem_without_restart(self, client: TestClient):
        response = client.post(
            "/api/autonomy", json={"subsystem": "goals", "enabled": False}, headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["subsystems"]["goals"] is False
        restored = client.post(
            "/api/autonomy", json={"subsystem": "goals", "enabled": True}, headers=AUTH
        )
        assert restored.json()["subsystems"]["goals"] is True

    def test_toggle_unknown_subsystem_422(self, client: TestClient):
        response = client.post(
            "/api/autonomy", json={"subsystem": "nope", "enabled": False}, headers=AUTH
        )
        assert response.status_code == 422

    def test_requires_token(self, client: TestClient):
        assert client.get("/api/autonomy").status_code == 401


class TestScheduleApi:
    def test_create_list_pause_resume_remove(self, client: TestClient):
        created = client.post(
            "/api/schedules",
            json={
                "objective": "daily digest",
                "kind": "interval",
                "interval_seconds": 3600,
                "name": "digest",
                "timezone": "Asia/Jakarta",
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        schedule = created.json()
        assert schedule["deliver"] == "web"
        assert schedule["enabled"] is True

        listed = client.get("/api/schedules", headers=AUTH).json()
        assert [item["schedule_id"] for item in listed] == [schedule["schedule_id"]]

        paused = client.post(f"/api/schedules/{schedule['schedule_id']}/pause", headers=AUTH)
        assert paused.json()["paused"] is True
        listed = client.get("/api/schedules", headers=AUTH).json()
        assert listed[0]["enabled"] is False

        resumed = client.post(f"/api/schedules/{schedule['schedule_id']}/resume", headers=AUTH)
        assert resumed.json()["resumed"] is True

        removed = client.delete(f"/api/schedules/{schedule['schedule_id']}", headers=AUTH)
        assert removed.json()["removed"] is True
        assert client.get("/api/schedules", headers=AUTH).json() == []

    def test_invalid_create_422_or_invalid(self, client: TestClient):
        missing_kind = client.post("/api/schedules", json={"objective": "x"}, headers=AUTH)
        assert missing_kind.status_code == 422
        bad_interval = client.post(
            "/api/schedules",
            json={"objective": "x", "kind": "interval", "interval_seconds": 3},
            headers=AUTH,
        )
        assert bad_interval.status_code == 422
        bad_time = client.post(
            "/api/schedules",
            json={
                "objective": "x",
                "kind": "once",
                "run_at": "2000-01-01T00:00:00+00:00",
            },
            headers=AUTH,
        )
        assert bad_time.status_code == 422

    def test_run_now_queues_job(self, client: TestClient):
        created = client.post(
            "/api/schedules",
            json={
                "objective": "check now",
                "kind": "interval",
                "interval_seconds": 3600,
            },
            headers=AUTH,
        ).json()
        queued = client.post(f"/api/schedules/{created['schedule_id']}/run", headers=AUTH)
        assert queued.status_code == 202
        assert queued.json()["queued"] is True
        jobs = client.get("/api/jobs", headers=AUTH).json()
        assert jobs and jobs[0]["objective"] == "check now"
        assert jobs[0]["correlation_id"] == created["schedule_id"]

    def test_run_now_unknown_schedule(self, client: TestClient):
        response = client.post("/api/schedules/deadbeef/run", headers=AUTH)
        assert response.status_code == 422

    def test_dry_run_returns_prompt_preview(self, client: TestClient):
        created = client.post(
            "/api/schedules",
            json={
                "objective": "morning brief",
                "kind": "interval",
                "interval_seconds": 3600,
                "pre_script": "echo brief-ready",
                "silent": True,
            },
            headers=AUTH,
        ).json()
        dry = client.post(f"/api/schedules/{created['schedule_id']}/dry-run", headers=AUTH)
        assert dry.status_code == 200
        data = dry.json()
        assert "brief-ready" in data["prelude"]
        assert data["objective"] == "morning brief"
        assert data["silent"] is True

    def test_dry_run_unknown_schedule(self, client: TestClient):
        assert client.post("/api/schedules/deadbeef/dry-run", headers=AUTH).status_code == 404


class TestJobsDeliveriesApi:
    def test_jobs_list_shape(self, client: TestClient):
        client.post(
            "/api/schedules",
            json={
                "objective": "status probe",
                "kind": "once",
                "run_at": _future(),
            },
            headers=AUTH,
        )
        jobs = client.get("/api/jobs", headers=AUTH).json()
        assert jobs == []

    def test_deliveries_list_empty(self, client: TestClient):
        assert client.get("/api/deliveries", headers=AUTH).json() == []
        filtered = client.get("/api/deliveries?status=delivered&channel=web", headers=AUTH).json()
        assert filtered == []
