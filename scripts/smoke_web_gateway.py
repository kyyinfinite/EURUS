"""Manual smoke test for the web gateway (not part of the pytest suite)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from eurus.config import load_config  # noqa: E402
from eurus.gateway.web.app import build_context, create_app  # noqa: E402
from eurus.gateway.web.auth import GatewaySecurity  # noqa: E402


def main() -> None:
    tmp = Path("/tmp/eurus-gateway-smoke")
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "config.toml").write_text(
        """
[providers.dummy]
protocol = "openai"
base_url = "http://127.0.0.1:9/v1"
api_key_env = "DUMMY_KEY"

[agent]
model = "dummy/dummy-model"
""",
        encoding="utf-8",
    )
    import os

    os.environ["EURUS_HOME"] = str(tmp)
    os.environ["DUMMY_KEY"] = "smoke-key-123456"

    config = load_config(cwd=tmp)
    token = "test-token-abc123"
    security = GatewaySecurity(token, host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)

    # Health must refuse bad tokens / hosts before any runtime work.
    app = create_app(ctx)
    with TestClient(app) as client:
        ok = client.get("/api/health", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
        print("health:", json.dumps(ok.json()["status"]))

        bad = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        print("bad token rejected:", bad.status_code)

        rebinding = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {token}", "Host": "evil.example.com"},
        )
        assert rebinding.status_code == 403, rebinding.text
        print("rebinding host rejected:", rebinding.status_code)

        csrf = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "http://evil.example.com",
            },
        )
        assert csrf.status_code == 403, csrf.text
        print("cross-origin post rejected:", csrf.status_code)

        created = client.post(
            "/api/sessions",
            json={"title": "Smoke session"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        print("session created:", session_id)

        sessions = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
        assert sessions.status_code == 200 and len(sessions.json()) == 1
        print("sessions listed:", sessions.json()[0]["title"])

        msgs = client.get(
            f"/api/sessions/{session_id}/messages", headers={"Authorization": f"Bearer {token}"}
        )
        assert msgs.status_code == 200 and msgs.json() == []
        print("messages empty ok")

        cfg = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
        assert cfg.status_code == 200
        print("config sections:", sorted(cfg.json()["config"].keys()))

        patched = client.patch(
            "/api/config",
            json={"section": "agent", "values": {"temperature": 0.4}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert patched.status_code == 200, patched.text
        print("config patched:", patched.json())

        invalid = client.patch(
            "/api/config",
            json={"section": "agent", "values": {"temperature": "not-a-number"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert invalid.status_code == 422, invalid.text
        print("invalid config rejected:", invalid.json()["error"]["code"])

        providers = client.get("/api/providers", headers={"Authorization": f"Bearer {token}"})
        assert providers.status_code == 200
        print("providers:", providers.json())

        tools = client.get("/api/tools", headers={"Authorization": f"Bearer {token}"})
        assert tools.status_code == 200 and len(tools.json()) > 5
        print("tools count:", len(tools.json()))

        mem = client.post(
            "/api/memory",
            json={"content": "The launch code is stored in the vault."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert mem.status_code == 201, mem.text
        print("memory added id:", mem.json()["id"])

        logs = client.get("/api/logs", headers={"Authorization": f"Bearer {token}"})
        assert logs.status_code == 200
        print("logs captured:", len(logs.json()))

        diag = client.get("/api/diagnostics", headers={"Authorization": f"Bearer {token}"})
        assert diag.status_code == 200
        assert "smoke-key" not in json.dumps(diag.json())
        print("diagnostics ok, no secrets leaked")

        # Missing session -> 404
        miss = client.post(
            "/api/sessions/does-not-exist/messages",
            json={"text": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert miss.status_code == 404, miss.text
        print("unknown session 404 ok")

    asyncio.run(ctx.runtime.stop())
    print("SMOKE OK")


if __name__ == "__main__":
    main()
