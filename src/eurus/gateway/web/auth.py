from __future__ import annotations

import json
import logging
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus import paths

log = logging.getLogger(__name__)

CREDENTIAL_FILE = "gateway-web.json"
LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]", "testserver", "testserver.local"}
)


class GatewayCredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else paths.home_dir() / CREDENTIAL_FILE

    def load_or_create(self) -> str:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                token = str(data.get("token", "")).strip()
                if token:
                    return token
            except (OSError, ValueError):
                log.warning("gateway credential file unreadable; regenerating")
        token = secrets.token_urlsafe(24)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(
            json.dumps(
                {"token": token, "created_at": datetime.now(UTC).isoformat()},
                indent=1,
            ),
            encoding="utf-8",
        )
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return token


def _token_from_request(request: Any) -> str:
    header = str(request.headers.get("authorization", ""))
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    query = request.query_params.get("token")
    return str(query).strip() if query else ""


def _request_origin(request: Any) -> str:
    origin = str(request.headers.get("origin", ""))
    if origin:
        return origin.rstrip("/")
    referer = str(request.headers.get("referer", ""))
    if referer:
        # Reduce referer to scheme://host[:port]
        parts = referer.split("://", 1)
        if len(parts) == 2:
            return f"{parts[0]}://{parts[1].split('/', 1)[0]}"
    return ""


class GatewaySecurity:
    def __init__(self, token: str, *, port: int, host: str) -> None:
        if not token:
            raise ValueError("gateway token must not be empty")
        self.token = token
        self.host = host
        self.port = port
        origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
            f"http://[::ffff:127.0.0.1]:{port}",
        }
        if host != "127.0.0.1":
            origins.add(f"http://{host}:{port}")
        self.allowed_origins = origins

    def host_allowed(self, host_header: str) -> bool:
        """Reject DNS-rebinding Host headers; loopback only unless overridden."""
        hostname = host_header.rsplit(":", 1)[0].strip("[]") if host_header else ""
        if not hostname:
            return False
        if self.host == "127.0.0.1":
            return hostname in LOOPBACK_HOSTS or hostname == "[::1]"
        return True

    def origin_allowed(self, origin: str) -> bool:
        if not origin:
            return True  # same-origin non-browser clients may omit Origin
        return origin.rstrip("/") in self.allowed_origins

    def token_ok(self, presented: str) -> bool:
        return secrets.compare_digest(presented, self.token)

    def check_http(self, request: Any, *, require_token: bool = True) -> tuple[bool, str]:
        host_header = request.headers.get("host", "")
        if not self.host_allowed(host_header):
            return False, "invalid host header"
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = _request_origin(request)
            if not self.origin_allowed(origin):
                return False, "cross-origin request rejected"
        if require_token and not self.token_ok(_token_from_request(request)):
            return False, "unauthorized"
        return True, ""

    def check_websocket(self, websocket: Any) -> tuple[bool, str]:
        host_header = websocket.headers.get("host", "")
        if not self.host_allowed(host_header):
            return False, "invalid host header"
        origin = websocket.headers.get("origin", "")
        if not self.origin_allowed(origin):
            return False, "cross-origin websocket rejected"
        if not self.token_ok(_token_from_request(websocket)):
            return False, "unauthorized"
        return True, ""

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{host}:{self.port}"
