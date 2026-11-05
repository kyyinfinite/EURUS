from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GATEWAY_PORT = 8799
MOCK_PORT = 9777
SECRET = "live-smoke-key-7f3a91d0c2"


def sse(chunks: list[dict]) -> bytes:
    body = b""
    for chunk in chunks:
        body += b"data: " + json.dumps(chunk).encode() + b"\n\n"
    body += b"data: [DONE]\n\n"
    return body


def tool_call_chunk(index: int, call_id: str, name: str, arguments: str) -> dict:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                }
            }
        ]
    }


class MockProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "dummy-model"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        if not self.path.endswith("chat/completions"):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        MockProviderHandler.step += 1
        if MockProviderHandler.step == 1:
            chunks = [
                tool_call_chunk(0, "call-1", "calculate", '{"expression": "21*2"}'),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6}},
            ]
        else:
            chunks = [
                {"choices": [{"delta": {"content": "The answer is 42."}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
            ]
        body = sse(chunks)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


MockProviderHandler.step = 0


def start_mock() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, MOCK_PORT


def http(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, str, dict[str, str]]:
    merged = dict(headers or {})
    if token:
        merged["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        merged.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, method=method, data=data, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (
                response.status,
                response.read().decode(),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.read().decode(),
            {key.lower(): value for key, value in error.headers.items()},
        )


def main() -> int:
    import eurus

    location = Path(eurus.__file__).resolve()
    assert "site-packages" in str(location), f"not using installed package: {location}"
    print(f"[1] installed distribution: {location}")

    assert Path("/home/z/my-project/eurus/eurus/src") not in location.parents
    print("[1] source tree not on import path: ok")

    mock, _ = start_mock()
    home = Path(tempfile.mkdtemp(prefix="eurus-e2e-"))
    (home / "config.toml").write_text(
        "[providers.dummy]\n"
        'protocol = "openai"\n'
        f'base_url = "http://127.0.0.1:{MOCK_PORT}/v1"\n'
        'api_key_env = "EURUS_E2E_KEY"\n'
        "\n[agent]\n"
        'model = "dummy/dummy-model"\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["EURUS_HOME"] = str(home)
    env["EURUS_E2E_KEY"] = SECRET
    venv_bin = Path(sys.prefix) / "bin"
    env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)
    cli = venv_bin / "eurus"
    assert cli.is_file(), f"installed CLI not found at {cli}"

    gateway_log = home / "gateway.log"
    with gateway_log.open("w") as log:
        proc = subprocess.Popen(
            [str(cli), "gateway", "web", "--port", str(GATEWAY_PORT)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd="/tmp",
        )
        try:
            base = f"http://127.0.0.1:{GATEWAY_PORT}"
            token = ""
            for _ in range(120):
                if proc.poll() is not None:
                    print(gateway_log.read_text())
                    raise AssertionError("gateway exited early")
                text = gateway_log.read_text()
                match = re.search(r"token=(\S+)", text)
                if "Web Gateway is ready" in text and match:
                    token = match.group(1)
                    break
                time.sleep(0.5)
            assert token, "gateway never became ready"
            ui_line = [line for line in gateway_log.read_text().splitlines() if "Web UI:" in line]
            assert ui_line and "packaged" in ui_line[0], ui_line
            print(f"[2] gateway ready, {ui_line[0].strip()}")

            status, body, headers = http("GET", f"{base}/api/health", token)
            assert status == 200, (status, body)
            health = json.loads(body)
            assert health["web_ui"]["available"] is True
            assert health["web_ui"]["source"] == "packaged"
            print(f"[3] /api/health ok version={health['version']} web_ui=packaged")

            index_status, index_body, index_headers = http("GET", f"{base}/")
            assert index_status == 200, (index_status, index_body[:300])
            assert "text/html" in index_headers.get("content-type", ""), index_headers
            assert "EURUS" in index_body, index_body[:120]
            print("[4] / serves built index.html")

            asset_match = re.search(r'assets/[^"]+\.js', index_body)
            assert asset_match, "no js asset referenced by index"
            asset = asset_match.group(0)
            a_status, _, a_headers = http("GET", f"{base}/{asset}")
            assert a_status == 200 and "javascript" in a_headers.get("content-type", "")
            print(f"[5] static asset {asset} 200 {a_headers.get('content-type')}")

            css_match = re.search(r'assets/[^"]+\.css', index_body)
            if css_match:
                c_status, _, c_headers = http("GET", f"{base}/{css_match.group(0)}")
                assert c_status == 200 and "css" in c_headers.get("content-type", "")
                print("[6] css asset content-type ok")

            s_status, s_body, s_headers = http("GET", f"{base}/sessions/abc")
            assert s_status == 200 and "text/html" in s_headers.get("content-type", "")
            print("[7] SPA route fallback ok")

            u_status, u_body, _ = http("GET", f"{base}/api/definitely-not-a-route", token)
            assert u_status == 404 and "html" not in json.dumps(json.loads(u_body))
            print("[8] unknown API route -> 404 json (not index.html)")

            ua_status, _, _ = http("GET", f"{base}/assets/missing-xyz.js")
            assert ua_status == 404, ua_status
            print("[9] missing asset -> 404")

            na_status, _, _ = http("GET", f"{base}/api/sessions")
            assert na_status == 401, na_status
            print("[10] unauthenticated protected API -> 401")

            exempt_status, exempt_body, _ = http("GET", f"{base}/api/health")
            assert exempt_status == 200, exempt_status
            print("[10] unauthenticated health probe stays exempt")

            rebinding = http(
                "GET", f"{base}/api/health", token, headers={"Host": "evil.example.com"}
            )
            assert rebinding[0] == 403, rebinding[0]
            print("[11] host rebinding -> 403")

            traversal = http("GET", f"{base}/%2e%2e/%2e%2e/config.toml")
            assert "api_key" not in traversal[1], traversal[1][:200]
            print("[12] path traversal blocked")

            created_status, created_body, _ = http(
                "POST", f"{base}/api/sessions", token, {"title": "Release E2E"}
            )
            assert created_status == 201, (created_status, created_body)
            session_id = json.loads(created_body)["id"]
            print(f"[13] session created {session_id[:8]}")

            sent_status, sent_body, _ = http(
                "POST",
                f"{base}/api/sessions/{session_id}/messages",
                token,
                {"text": "Use the calculate tool to compute 21*2 and tell me the answer."},
            )
            assert sent_status == 202, (sent_status, sent_body)
            run_id = json.loads(sent_body)["run_id"]
            print(f"[14] task submitted run={run_id[:8]}")

            final_status = ""
            for _ in range(240):
                _, run_body, _ = http("GET", f"{base}/api/runs/{run_id}", token)
                run = json.loads(run_body)
                final_status = run["status"]
                if final_status in {"completed", "failed"}:
                    break
                time.sleep(0.5)
            assert final_status == "completed", f"run ended as {final_status}"
            print("[15] agent run completed")

            _, msgs_body, _ = http("GET", f"{base}/api/sessions/{session_id}/messages", token)
            contents = [message.get("content", "") for message in json.loads(msgs_body)]
            joined = " ".join(contents)
            assert "42" in joined, contents
            print("[16] response delivered to user:", [c for c in contents if "42" in c][:1])

            _, tools_body, _ = http("GET", f"{base}/api/tools", token)
            tool_names = {tool["name"] for tool in json.loads(tools_body)}
            assert "calculate" in tool_names
            print("[17] tool registry exposed, calculate available")

            log_text = gateway_log.read_text()
            assert SECRET not in log_text, "API key leaked into gateway log"
            assert SECRET not in joined, "API key leaked into conversation"
            print("[18] no secret leakage in gateway log or responses")

            print("RELEASE INSTALL E2E: PASS")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            mock.shutdown()
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
