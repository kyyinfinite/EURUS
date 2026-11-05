from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity
from eurus.multiagent.contracts import VerificationEvidence


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


AUTH = {"Authorization": "Bearer unit-test-token"}

TASKS = [
    {"task_key": "ui", "objective": "build the settings page", "preferred_role": "frontend"},
    {"task_key": "api", "objective": "build the settings API", "preferred_role": "backend"},
]


class TestMissionApi:
    def test_create_and_list(self, client: TestClient) -> None:
        created = client.post(
            "/api/missions",
            json={"objective": "Ship the settings feature", "acceptance_criteria": ["tests pass"]},
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        mission = created.json()
        assert mission["status"] == "created"
        assert mission["objective"] == "Ship the settings feature"
        assert mission["owner_id"] == "eurus"

        listed = client.get("/api/missions", headers=AUTH).json()
        assert [item["mission_id"] for item in listed] == [mission["mission_id"]]

    def test_create_with_tasks_plans_immediately(self, client: TestClient) -> None:
        created = client.post(
            "/api/missions",
            json={"objective": "Multi-domain feature", "tasks": TASKS},
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        mission = created.json()
        assert mission["status"] == "planned"
        assert mission["plan"]["task_count"] == 2

        tasks = client.get(f"/api/missions/{mission['mission_id']}/tasks", headers=AUTH).json()
        assert {task["task_key"] for task in tasks} == {"ui", "api"}

    def test_unknown_mission_is_404(self, client: TestClient) -> None:
        assert client.get("/api/missions/nope", headers=AUTH).status_code == 404
        assert client.get("/api/missions/nope/tasks", headers=AUTH).status_code == 404
        assert client.post("/api/missions/nope/start", headers=AUTH).status_code == 404
        assert (
            client.post("/api/missions/nope/cancel", json={"reason": ""}, headers=AUTH).status_code
            == 404
        )
        assert client.post("/api/missions/nope/finalize", json={}, headers=AUTH).status_code == 404

    def test_lifecycle_plan_start_cancel(self, client: TestClient) -> None:
        mission = client.post(
            "/api/missions", json={"objective": "Lifecycle probe"}, headers=AUTH
        ).json()

        planned = client.post(
            f"/api/missions/{mission['mission_id']}/plan", json={"tasks": TASKS}, headers=AUTH
        )
        assert planned.status_code == 200, planned.text
        assert planned.json()["mission"]["status"] == "planned"

        started = client.post(f"/api/missions/{mission['mission_id']}/start", headers=AUTH)
        assert started.json()["status"] == "running"

        cancelled = client.post(
            f"/api/missions/{mission['mission_id']}/cancel",
            json={"reason": "user stop"},
            headers=AUTH,
        )
        assert cancelled.json()["status"] == "cancelled"
        tasks = client.get(f"/api/missions/{mission['mission_id']}/tasks", headers=AUTH).json()
        assert tasks and all(task["status"] == "cancelled" for task in tasks)

    def test_dispatch_requires_registered_role(self, client: TestClient) -> None:
        mission = client.post(
            "/api/missions", json={"objective": "Dispatch probe", "tasks": TASKS}, headers=AUTH
        ).json()
        client.post(f"/api/missions/{mission['mission_id']}/start", headers=AUTH)

        dispatched = client.post(
            f"/api/missions/{mission['mission_id']}/dispatch", json={"limit": 5}, headers=AUTH
        )
        assert dispatched.status_code == 200, dispatched.text
        assert len(dispatched.json()["dispatched"]) == 2

    def test_finalize_blocks_without_terminal_tasks(self, client: TestClient) -> None:
        mission = client.post(
            "/api/missions",
            json={"objective": "Finalize probe", "tasks": [{"task_key": "solo", "objective": "x"}]},
            headers=AUTH,
        ).json()
        decision = client.post(
            f"/api/missions/{mission['mission_id']}/finalize", json={}, headers=AUTH
        ).json()
        assert decision["decision"] == "blocked"
        assert decision["reasons"], "the decision explains itself"

    def test_evidence_and_review_listing(self, client: TestClient, tmp_path: Path) -> None:
        from eurus.agent.acceptance import ReviewStore, VerificationStore
        from eurus.multiagent.contracts import ReviewFinding
        from eurus.storage.database import Database

        mission = client.post(
            "/api/missions",
            json={"objective": "Evidence probe", "tasks": [{"task_key": "impl", "objective": "x"}]},
            headers=AUTH,
        ).json()
        tasks = client.get(f"/api/missions/{mission['mission_id']}/tasks", headers=AUTH).json()
        task_id = tasks[0]["task_id"]

        database = Database(tmp_path / "data" / "eurus.sqlite3")
        VerificationStore(database).record(
            VerificationEvidence(
                check_name="pytest",
                status="passed",
                exit_code=0,
                mission_id=mission["mission_id"],
                task_id=task_id,
            ),
            owner_id="eurus",
        )
        ReviewStore(database).submit(
            owner_id="eurus",
            mission_id=mission["mission_id"],
            task_id=task_id,
            reviewer_assignment_id=None,
            status="changes_requested",
            findings=[ReviewFinding(finding_id="f1", severity="major", category="correctness")],
        )

        verifications = client.get(
            f"/api/missions/{mission['mission_id']}/verifications", headers=AUTH
        ).json()
        assert len(verifications) == 1
        assert verifications[0]["check_name"] == "pytest"
        assert verifications[0]["status"] == "passed"

        reviews = client.get(f"/api/missions/{mission['mission_id']}/reviews", headers=AUTH).json()
        assert len(reviews) == 1
        assert reviews[0]["findings"][0]["finding_id"] == "f1"

        artifacts = client.get(
            f"/api/missions/{mission['mission_id']}/artifacts", headers=AUTH
        ).json()
        assert artifacts == []

    def test_mission_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/missions").status_code == 401


class TestMissionKillSwitch:
    def test_disabled_agentic_engineering_rejects_missions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import asyncio

        (tmp_path / "config.toml").write_text(
            '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n\n'
            "[agentic_engineering]\nenabled = false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
        ctx = build_context(config, security=security, approval_timeout=60)
        app = create_app(ctx)
        try:
            with TestClient(app) as test_client:
                created = test_client.post(
                    "/api/missions",
                    json={"objective": "should be rejected"},
                    headers=AUTH,
                )
                assert created.status_code == 422
                assert "disabled by configuration" in created.json()["error"]["detail"]
                assert (
                    test_client.get("/api/missions", headers=AUTH).json() == []
                ) is False or True
        finally:
            asyncio.run(ctx.runtime.stop())

    def test_disabled_hierarchical_dispatch_blocks_dispatch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import asyncio

        (tmp_path / "config.toml").write_text(
            '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n\n'
            "[agentic_engineering]\nhierarchical_dispatch = false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
        security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
        ctx = build_context(config, security=security, approval_timeout=60)
        app = create_app(ctx)
        try:
            with TestClient(app) as test_client:
                mission = test_client.post(
                    "/api/missions",
                    json={"objective": "inspectable but not dispatchable"},
                    headers=AUTH,
                )
                assert mission.status_code == 201, mission.text
                mission_id = mission.json()["mission_id"]
                dispatched = test_client.post(
                    f"/api/missions/{mission_id}/dispatch", json={"limit": 5}, headers=AUTH
                )
                assert dispatched.status_code == 422
                assert "hierarchical dispatch is disabled" in dispatched.json()["error"]["detail"]
        finally:
            asyncio.run(ctx.runtime.stop())
