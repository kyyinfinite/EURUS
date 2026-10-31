"""End-to-end integration tests: web gateway -> canonical AgentEngine -> SQLite.

These tests run the REAL agent stack (build_agent, ToolExecutionBoundary,
AgentEngine, SessionStore, RunStore, EventStore) with a scripted provider
transport, exercising the same path a browser chat message takes. All
orchestration goes through the HTTP API, exactly like the frontend does.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eurus.config import load_config
from eurus.gateway.web.app import build_context, create_app
from eurus.gateway.web.auth import GatewaySecurity
from eurus.providers import testing as provider_testing

AUTH = {"Authorization": "Bearer unit-test-token"}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={
            "agent": {"model": "groq/test"},
            "permissions": {"workspace": str(workspace)},
        },
    )
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=120)
    app = create_app(ctx)
    with TestClient(app) as test_client:
        yield test_client, ctx
    asyncio.run(ctx.runtime.stop())


def _script_router(ctx, monkeypatch, *, streams) -> None:  # noqa: ANN001
    """Patch the WebRuntime router factory with a scripted transport."""
    _router, _transport = provider_testing.scripted_router(streams=streams)
    monkeypatch.setattr("eurus.gateway.web.runtime.build_router", lambda config: _router)


def _poll(client: TestClient, path: str, predicate, *, timeout: float = 30.0):  # noqa: ANN001
    """Poll a GET endpoint until the predicate on its JSON body is truthy."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200, response.text
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.1)
    raise AssertionError(f"condition not met for {path}: {last!r}")


class HangingStreamTransport(provider_testing.FakeTransport):
    async def stream_sse(self, url, headers, payload):  # noqa: ANN001
        self.calls.append(provider_testing.RecordedCall(url, dict(headers), dict(payload)))
        yield json.dumps({"choices": [{"delta": {"content": "starting"}}]})
        await asyncio.sleep(60)


def _make_router(transport):  # noqa: ANN001
    from eurus.catalog import BUILTIN_PROVIDERS
    from eurus.models import ModelRef
    from eurus.providers.base import RetryPolicy
    from eurus.providers.openai_compat import OpenAICompatProvider
    from eurus.providers.router import Router

    provider = OpenAICompatProvider(
        BUILTIN_PROVIDERS["groq"],
        "k",
        transport,
        RetryPolicy(max_retries=0),
        provider_testing.RecordingSleep(),
    )
    return Router({"groq": provider}, [ModelRef.parse("groq/test")])


class TestChatExecution:
    def test_message_reaches_real_agent_and_persists(self, gateway, monkeypatch):  # noqa: ANN001
        client, ctx = gateway
        _script_router(
            ctx, monkeypatch, streams=[provider_testing.openai_stream("All systems nominal.")]
        )
        session = client.post("/api/sessions", json={"title": "E2E"}, headers=AUTH).json()
        response = client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"text": "Status report?"},
            headers=AUTH,
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        run = _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"completed", "failed"},
        )
        assert run["status"] == "completed", run["stop_reason"]
        assert run["session_id"] == session["id"]
        assert run["owner_id"] == "web"
        assert run["origin"] == "web"

        messages = client.get(f"/api/sessions/{session['id']}/messages", headers=AUTH).json()
        contents = [message["content"] for message in messages]
        assert "Status report?" in contents
        assert "All systems nominal." in contents

        events = client.get(f"/api/runs/{run_id}/events", headers=AUTH).json()
        event_types = {event["event_type"] for event in events}
        assert {"run.started", "run.completed"} <= event_types

    def test_known_run_id_before_execution(self, gateway, monkeypatch):  # noqa: ANN001
        """The pre-allocated run id must be the engine's canonical run id."""
        client, ctx = gateway
        _script_router(ctx, monkeypatch, streams=[provider_testing.openai_stream("ok")])
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "hi"}, headers=AUTH
        ).json()["run_id"]
        run = _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"completed", "failed"},
        )
        assert run["run_id"] == run_id

    def test_second_message_while_run_active_is_rejected(self, gateway, monkeypatch):  # noqa: ANN001
        client, ctx = gateway
        monkeypatch.setattr(
            "eurus.gateway.web.runtime.build_router",
            lambda config: _make_router(HangingStreamTransport()),
        )
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "one"}, headers=AUTH
        ).json()["run_id"]

        assert run_id in ctx.hub.web_runs
        conflict = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "two"}, headers=AUTH
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "run_active"

        _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] == "running",
            timeout=30,
        )
        cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=AUTH)
        assert cancelled.status_code == 200
        _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"cancelled", "failed"},
            timeout=30,
        )

    def test_conversation_survives_refresh(self, gateway, monkeypatch):  # noqa: ANN001
        client, ctx = gateway
        _script_router(
            ctx, monkeypatch, streams=[provider_testing.openai_stream("Persistent answer.")]
        )
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "remember this"}, headers=AUTH
        ).json()["run_id"]
        _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"completed", "failed"},
        )
        # A "refresh" re-fetches messages through the API.
        refreshed = client.get(f"/api/sessions/{session['id']}/messages", headers=AUTH).json()
        assert [m["content"] for m in refreshed] == ["remember this", "Persistent answer."]


class TestApprovalFlow:
    def test_mutating_tool_waits_for_web_approval(self, gateway, monkeypatch, workspace: Path):  # noqa: ANN001
        client, ctx = gateway
        streams = [
            provider_testing.openai_stream(
                "",
                calls=[("call-1", "create_file", {"path": "approved.txt", "content": "hello"})],
            ),
            provider_testing.openai_stream("File created."),
        ]
        _script_router(ctx, monkeypatch, streams=streams)
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "make the file"}, headers=AUTH
        ).json()["run_id"]

        approvals = _poll(
            client,
            "/api/approvals",
            lambda body: len(body) == 1,
        )
        approval = approvals[0]
        assert approval["tool_name"] == "create_file"
        assert approval["run_id"] == run_id
        assert approval["session_id"] == session["id"]
        assert "approved.txt" in approval["arguments_json"]

        resolved = client.post(
            f"/api/approvals/{approval['token']}/resolve",
            json={"session_id": session["id"], "decision": "approved"},
            headers=AUTH,
        )
        assert resolved.status_code == 200, resolved.text

        # Duplicate resolve is rejected.
        duplicate = client.post(
            f"/api/approvals/{approval['token']}/resolve",
            json={"session_id": session["id"], "decision": "approved"},
            headers=AUTH,
        )
        assert duplicate.status_code == 422

        run = _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"completed", "failed"},
            timeout=30,
        )
        assert run["status"] == "completed", run["stop_reason"]
        assert (workspace / "approved.txt").read_text(encoding="utf-8") == "hello"

        # Decisions are persisted durably for cross-interface audit.
        with sqlite3.connect(ctx.broker.persistence_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT status, decision FROM gateway_approval_requests"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"
        assert rows[0]["decision"] == 1

        # The approval center is now empty.
        assert _poll(client, "/api/approvals", lambda body: body == []) == []

    def test_denied_approval_blocks_mutation(self, gateway, monkeypatch, workspace: Path):  # noqa: ANN001
        client, ctx = gateway
        streams = [
            provider_testing.openai_stream(
                "",
                calls=[("call-1", "create_file", {"path": "denied.txt", "content": "no"})],
            ),
            provider_testing.openai_stream("Understood, not creating the file."),
        ]
        _script_router(ctx, monkeypatch, streams=streams)
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "make the file"}, headers=AUTH
        ).json()["run_id"]

        approvals = _poll(client, "/api/approvals", lambda body: len(body) == 1)
        token = approvals[0]["token"]

        # Wrong session binding must not resolve.
        wrong = client.post(
            f"/api/approvals/{token}/resolve",
            json={"session_id": "other-session", "decision": "approved"},
            headers=AUTH,
        )
        assert wrong.status_code == 422

        resolved = client.post(
            f"/api/approvals/{token}/resolve",
            json={"session_id": session["id"], "decision": "denied"},
            headers=AUTH,
        )
        assert resolved.status_code == 200

        _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"completed", "failed"},
            timeout=30,
        )
        assert not (workspace / "denied.txt").exists()


class TestCancellation:
    def test_cancel_request_terminates_run(self, gateway, monkeypatch):  # noqa: ANN001
        client, ctx = gateway
        monkeypatch.setattr(
            "eurus.gateway.web.runtime.build_router",
            lambda config: _make_router(HangingStreamTransport()),
        )
        session = client.post("/api/sessions", json={}, headers=AUTH).json()
        run_id = client.post(
            f"/api/sessions/{session['id']}/messages", json={"text": "hang"}, headers=AUTH
        ).json()["run_id"]

        # Wait until the engine has persisted the run as running.
        _poll(client, f"/api/runs/{run_id}", lambda body: body["status"] == "running")

        cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=AUTH)
        assert cancelled.status_code == 200
        run = _poll(
            client,
            f"/api/runs/{run_id}",
            lambda body: body["status"] in {"cancelled", "failed"},
            timeout=30,
        )
        assert run["status"] == "cancelled"


class TestWebSocketEvents:
    def test_agent_events_stream_to_websocket(self, gateway, monkeypatch):  # noqa: ANN001
        client, ctx = gateway
        _script_router(ctx, monkeypatch, streams=[provider_testing.openai_stream("streamed hello")])
        session = client.post("/api/sessions", json={}, headers=AUTH).json()

        with client.websocket_connect("/api/ws?token=unit-test-token") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            ws.send_json({"type": "subscribe", "sessions": [session["id"]]})
            ack = ws.receive_json()
            assert ack["type"] == "subscribed"

            client.post(
                f"/api/sessions/{session['id']}/messages",
                json={"text": "say hi"},
                headers=AUTH,
            )

            kinds: list[str] = []
            statuses: list[str] = []
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "agent_event":
                    kinds.append(event["event"]["kind"])
                elif event["type"] == "run_status":
                    statuses.append(event["status"])
                    if event["status"] in {"completed", "failed", "cancelled"}:
                        break
            assert "text" in kinds
            assert "done" in kinds
            assert statuses and statuses[-1] in {"completed", "failed"}
