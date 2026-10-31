"""FastAPI application for the local EURUS web gateway.

The app is a thin transport layer over :class:`eurus.gateway.web.service.GatewayService`.
It adds no behaviour of its own beyond request validation, authentication and
payload shaping. The React frontend build is served from the same process so
production use is a single command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from eurus import __version__
from eurus.gateway.web.schemas import (
    ConfigPatchRequest,
    CreateMemoryRequest,
    CreateSessionRequest,
    ResolveApprovalRequest,
    SendMessageRequest,
)
from eurus.gateway.web.service import GatewayContext, GatewayService, ServiceError

log = logging.getLogger(__name__)

WS_CLOSE_POLICY = 4400
WS_CLOSE_AUTH = 4401


def _error_payload(code: str, detail: str) -> dict[str, Any]:
    return {"error": {"code": code, "detail": detail}}


def create_app(ctx: GatewayContext, *, static_dir: Path | None = None) -> FastAPI:
    service = GatewayService(ctx)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await ctx.runtime.start()
        ctx.hub.start_tailer()
        log.info("EURUS web gateway started version=%s", __version__)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await service.shutdown()
            log.info("EURUS web gateway stopped")

    app = FastAPI(
        title="EURUS Web Gateway",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=_error_payload(exc.code, exc.detail))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        log.warning("unhandled gateway error error_type=%s", type(exc).__name__, exc_info=True)
        return JSONResponse(
            status_code=500,
            content=_error_payload("internal", f"{type(exc).__name__}: {exc}"),
        )

    # -- middleware: local security -------------------------------------------

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/api"):
            # Only the liveness probe is usable without a token; it stays
            # host-checked and is read-only.
            exempt = path == "/api/health" and request.method == "GET"
            allowed, reason = ctx.security.check_http(request, require_token=not exempt)
            if not allowed:
                status = 401 if reason == "unauthorized" else 403
                return JSONResponse(status_code=status, content=_error_payload("forbidden", reason))
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api") else "no-cache"
        return response

    # -- health & runtime -------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(ctx.uptime, 1),
            "runtime": ctx.runtime.health(),
        }

    @app.get("/api/runtime")
    async def runtime_status() -> dict[str, Any]:
        return service.runtime_status()

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        return service.dashboard()

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        return service.diagnostics()

    # -- sessions ----------------------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return await service.list_sessions()

    @app.post("/api/sessions", status_code=201)
    async def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        return await service.create_session(payload.title)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        deleted = await service.delete_session(session_id)
        return {"deleted": deleted, "session_id": session_id}

    @app.get("/api/sessions/{session_id}/messages")
    async def list_messages(session_id: str) -> list[dict[str, Any]]:
        return await service.list_messages(session_id)

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, payload: SendMessageRequest) -> dict[str, Any]:
        return await service.send_message(session_id, payload.text, payload.attachments)

    # -- media -----------------------------------------------------------------------

    @app.get("/api/media")
    async def list_media(
        session_id: str | None = None, limit: int = Query(default=200, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        return service.list_media(session_id, limit)

    @app.post("/api/media", status_code=201)
    async def upload_media(
        session_id: str = Form(...), file: UploadFile = File(...)
    ) -> dict[str, Any]:
        data = await file.read()
        return await service.upload_media(
            session_id, file.filename or "file", file.content_type, data
        )

    @app.get("/api/media/{media_id}")
    async def get_media(media_id: str) -> dict[str, Any]:
        return await service.get_media(media_id)

    @app.get("/api/media/{media_id}/content")
    async def media_content(media_id: str) -> FileResponse:
        path, media = await service.media_content(media_id)
        return FileResponse(
            path,
            media_type=media.mime,
            filename=media.filename,
            headers={"Content-Disposition": f"inline; filename=\"{media.filename}\""},
        )

    @app.delete("/api/media/{media_id}")
    async def delete_media(media_id: str) -> dict[str, Any]:
        deleted = await service.delete_media(media_id)
        if not deleted:
            raise ServiceError("not_found", f"unknown media {media_id}")
        return {"deleted": True, "id": media_id}

    # -- runs ----------------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs(
        session_id: str | None = None, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        return await service.list_runs(session_id, limit=limit)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return await service.get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str, after_sequence: int = Query(default=0, ge=0)
    ) -> list[dict[str, Any]]:
        return await service.run_events(run_id, after_sequence)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        return await service.cancel_run(run_id)

    # -- approvals -------------------------------------------------------------------

    @app.get("/api/approvals")
    async def pending_approvals() -> list[dict[str, Any]]:
        return await service.pending_approvals()

    @app.post("/api/approvals/{token}/resolve")
    async def resolve_approval(token: str, payload: ResolveApprovalRequest) -> dict[str, Any]:
        resolved = await service.resolve_approval(
            token, payload.session_id, payload.decision == "approved"
        )
        if not resolved:
            raise ServiceError("invalid", "approval token is not pending or session mismatch")
        return {"resolved": True, "decision": payload.decision}

    # -- configuration ------------------------------------------------------------------

    @app.get("/api/config")
    async def read_config() -> dict[str, Any]:
        return service.read_config()

    @app.patch("/api/config")
    async def patch_config(payload: ConfigPatchRequest) -> dict[str, Any]:
        return await service.patch_config(payload.section, payload.values)

    # -- providers / tools / mcp ------------------------------------------------------------

    @app.get("/api/providers")
    async def providers() -> list[dict[str, Any]]:
        return service.list_providers()

    @app.post("/api/providers/{name}/discover")
    async def discover_provider(name: str) -> dict[str, Any]:
        return await service.discover_provider(name)

    @app.get("/api/tools")
    async def tools() -> list[dict[str, Any]]:
        return service.list_tools()

    @app.get("/api/mcp")
    async def mcp_status() -> dict[str, Any]:
        return service.mcp_status()

    # -- memory ---------------------------------------------------------------------------

    @app.get("/api/memory")
    async def list_memories(
        query: str = "", limit: int = Query(default=50, ge=1, le=100)
    ) -> list[dict[str, Any]]:
        return await service.list_memories(query, limit)

    @app.post("/api/memory", status_code=201)
    async def add_memory(payload: CreateMemoryRequest) -> dict[str, Any]:
        return await service.add_memory(payload.model_dump())

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: int) -> dict[str, Any]:
        deleted = await service.delete_memory(memory_id)
        if not deleted:
            raise ServiceError("not_found", f"unknown memory {memory_id}")
        return {"deleted": True, "id": memory_id}

    @app.post("/api/memory/{memory_id}/trust")
    async def set_memory_trust(memory_id: int, trust: str = Query(...)) -> dict[str, Any]:
        updated = await service.set_memory_trust(memory_id, trust)
        if not updated:
            raise ServiceError("invalid", "trust level rejected or memory missing")
        return {"updated": True, "id": memory_id, "trust": trust}

    # -- logs ---------------------------------------------------------------------------------

    @app.get("/api/logs")
    async def logs(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.log_snapshot(after_sequence, limit)

    # -- websocket -------------------------------------------------------------------------------

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        allowed, reason = ctx.security.check_websocket(websocket)
        if not allowed:
            code = WS_CLOSE_AUTH if reason == "unauthorized" else WS_CLOSE_POLICY
            await websocket.close(code=code)
            return
        await websocket.accept()
        await ctx.hub.attach(websocket)
        try:
            await websocket.send_json(
                {"type": "hello", "version": __version__, "clients": ctx.hub.client_count}
            )
            while True:
                raw = await websocket.receive_text()
                try:
                    message = _parse_json_object(raw)
                except ValueError:
                    await websocket.send_json({"type": "error", "detail": "invalid json"})
                    continue
                kind = str(message.get("type", ""))
                if kind == "subscribe":
                    sessions = _clean_session_ids(message.get("sessions"))
                    ctx.hub.subscribe(websocket, sessions)
                    await websocket.send_json({"type": "subscribed", "sessions": sessions})
                elif kind == "unsubscribe":
                    sessions = _clean_session_ids(message.get("sessions"))
                    ctx.hub.unsubscribe(websocket, sessions)
                    await websocket.send_json({"type": "unsubscribed", "sessions": sessions})
                elif kind == "ping":
                    await websocket.send_json({"type": "pong", "time": _now()})
                elif kind == "refresh":
                    await websocket.send_json({"type": "sessions_changed"})
                else:
                    await websocket.send_json({"type": "error", "detail": f"unknown type {kind}"})
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("websocket handler error", exc_info=True)
        finally:
            await ctx.hub.detach(websocket)

    # -- static frontend ---------------------------------------------------------------------------

    dist = _resolve_static_dir(static_dir)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        if dist is None:
            return HTMLResponse(
                "<h1>EURUS Web Gateway</h1><p>Frontend build not found. "
                "Build it with <code>npm ci &amp;&amp; npm run build</code> inside "
                "<code>frontend/</code>, or run the Vite dev server instead.</p>",
                status_code=200,
            )
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
                return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(
            "<p>Frontend build is incomplete (missing index.html).</p>", status_code=500
        )

    return app


def _parse_json_object(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected a json object")
    return data


def _clean_session_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()][:64]


def _now() -> float:
    import time

    return time.time()


def _resolve_static_dir(static_dir: Path | None) -> Path | None:
    import os

    override = os.environ.get("EURUS_GATEWAY_STATIC", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    if static_dir is not None:
        return static_dir if static_dir.is_dir() else None
    default = Path(__file__).resolve().parents[4] / "frontend" / "dist"
    return default if default.is_dir() else None


def build_context(
    config: Any,
    *,
    security: Any,
    approval_timeout: float = 600.0,
) -> GatewayContext:
    """Wire the gateway context around the canonical runtime (startup path).

    Everything here already exists in EURUS: the SQLite stores, the approval
    broker, the agent runtime and the engine helpers. This function only
    assembles them for the web process.
    """
    import time

    from eurus import paths
    from eurus.gateway.web.approval import WebApprovalBroker
    from eurus.gateway.web.hub import SessionHub
    from eurus.gateway.web.logs import attach_ring_buffer
    from eurus.gateway.web.runtime import WebRuntime
    from eurus.logs import collect_secrets
    from eurus.sessions import SessionStore
    from eurus.storage.events import EventStore
    from eurus.storage.runs import RunStore

    database_path = paths.database_path()
    store = SessionStore(database_path)
    run_store = RunStore(database_path)
    event_store = EventStore(database_path)
    hub = SessionHub(run_store, event_store)
    broker = WebApprovalBroker(
        broadcast=hub.broadcast,
        timeout=approval_timeout,
        persistence_path=database_path,
    )
    log_buffer = attach_ring_buffer(secrets=collect_secrets(config))
    runtime = WebRuntime(config, broker)
    return GatewayContext(
        config=config,
        store=store,
        run_store=run_store,
        event_store=event_store,
        runtime=runtime,
        broker=broker,
        hub=hub,
        log_buffer=log_buffer,
        security=security,
        started_at=time.monotonic(),
    )


async def serve(
    ctx: GatewayContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8712,
    open_browser: bool = False,
    static_dir: Path | None = None,
) -> None:
    """Run the gateway until interrupted (entry point for ``eurus gateway web``)."""
    import webbrowser

    import uvicorn

    app = create_app(ctx, static_dir=static_dir)
    url = f"{ctx.security.url}/?token={ctx.security.token}"
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )

    class BannerServer(uvicorn.Server):
        async def startup(self, sockets: Any = None) -> None:  # noqa: D102
            await super().startup(sockets=sockets)
            print()
            print("EURUS Web Gateway is ready:")
            print(f"  {url}")
            print("  (keep this terminal open; press Ctrl+C to stop)")
            print()
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(url)

    server = BannerServer(config)
    await server.serve()
    await asyncio.sleep(0)
