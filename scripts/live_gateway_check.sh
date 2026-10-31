#!/usr/bin/env bash
# Live end-to-end check: boots the real gateway (uvicorn) with the built
# frontend, exercises the API over HTTP, then shuts it down.
set -euo pipefail

HOME_DIR=$(mktemp -d /tmp/eurus-live-XXXX)
export EURUS_HOME="$HOME_DIR/eurus-home"
mkdir -p "$EURUS_HOME"
cat > "$EURUS_HOME/config.toml" <<'TOML'
[providers.dummy]
protocol = "openai"
base_url = "http://127.0.0.1:9777/v1"
api_key_env = "DUMMY_KEY"

[agent]
model = "dummy/dummy-model"
TOML
export DUMMY_KEY="live-smoke-key-0001"

# Mock OpenAI-compatible chat server (background).
python3 - > "$HOME_DIR/mock.log" 2>&1 <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        if self.path.endswith("chat/completions"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            chunks = [
                {"choices": [{"delta": {"content": "Live "}}]},
                {"choices": [{"delta": {"content": "smoke response."}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = {"data": [{"id": "dummy-model"}]}
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", 9777), Handler).serve_forever()
PY
MOCK_PID=$!
sleep 0.5

# Start the gateway (background), capture its output for the token URL.
python3 -m eurus gateway web --port 8712 > "$HOME_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!

for i in $(seq 1 60); do
  if rg -q "EURUS Web Gateway is ready" "$HOME_DIR/gateway.log" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

cat "$HOME_DIR/gateway.log"
URL=$(rg -o 'http://127\.0\.0\.1:8712/\?token=\S+' "$HOME_DIR/gateway.log" | head -1)
TOKEN=$(printf '%s' "$URL" | sed 's/.*token=//')

python3 - "$TOKEN" <<'PY'
import json, sys, time, urllib.request

token = sys.argv[1]
base = "http://127.0.0.1:8712"

def call(method, path, payload=None):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None

health = call("GET", "/api/health")
assert health["status"] == "ok", health
print("health ok")

# Static frontend served by the gateway process.
index = urllib.request.urlopen(base + "/", timeout=10).read().decode()
assert "EURUS" in index and "root" in index, index[:200]
print("frontend index served ok")

created = call("POST", "/api/sessions", {"title": "Live smoke"})
session_id = created["id"]
print("session created:", session_id[:8])

sent = call("POST", f"/api/sessions/{session_id}/messages", {"text": "ping"})
run_id = sent["run_id"]
for _ in range(120):
    run = call("GET", f"/api/runs/{run_id}")
    if run["status"] in {"completed", "failed"}:
        break
    time.sleep(0.25)
assert run["status"] == "completed", run
print("run completed via live server")

messages = call("GET", f"/api/sessions/{session_id}/messages")
contents = [m["content"] for m in messages]
assert "ping" in contents and "Live smoke response." in contents, contents
print("conversation persisted:", contents)

sessions = call("GET", "/api/sessions")
assert sessions[0]["message_count"] == 2, sessions
print("LIVE E2E OK")
PY

kill $GATEWAY_PID 2>/dev/null || true
kill $MOCK_PID 2>/dev/null || true
wait 2>/dev/null || true
echo "gateway log tail:"; tail -5 "$HOME_DIR/gateway.log"
cp "$HOME_DIR/gateway.log" /tmp/gw-debug.log; rm -rf "$HOME_DIR"
