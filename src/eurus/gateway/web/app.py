from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from eurus import __version__
from eurus.autonomy_service import AutonomyService
from eurus.gateway.web.realtime import RealtimeBridgeError, build_realtime_bridge
from eurus.gateway.web.schemas import (
    AutonomyToggleRequest,
    ConfigPatchRequest,
    CreateMemoryRequest,
    CreateSessionRequest,
    MissionCancelRequest,
    MissionCreateRequest,
    MissionDispatchRequest,
    MissionFinalizeRequest,
    MissionPlanRequest,
    RealtimeSessionRequest,
    ResolveApprovalRequest,
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    SendMessageRequest,
    SkillPromoteRequest,
)
from eurus.gateway.web.service import GatewayContext, GatewayService, ServiceError
from eurus.media.limits import MediaLimits

log = logging.getLogger(__name__)

WS_CLOSE_POLICY = 4400
WS_CLOSE_AUTH = 4401
MAX_UPLOAD_BYTES = MediaLimits.from_environ().max_bytes
UPLOAD_CHUNK_BYTES = 256 * 1024
STATIC_DIR_ENV = "EURUS_GATEWAY_STATIC"
PACKAGED_STATIC_DIRNAME = "static"


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ServiceError("invalid", f"upload exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_payload(code: str, detail: str) -> dict[str, Any]:
    return {"error": {"code": code, "detail": detail}}


def _web_delivery_channel(hub: Any) -> Any:
    async def deliver(target: str, subject: str, body: str) -> None:
        await hub.broadcast(target or None, {"type": "delivery", "subject": subject, "body": body})

    return deliver


MAX_WEBSOCKET_CLIENTS = 16


class SlidingWindowRateLimiter:
    def __init__(self, *, window_seconds: float = 60.0, limit: int = 240) -> None:
        self.window_seconds = window_seconds
        self.limit = limit
        self._events: dict[str, list[float]] = {}
        self._guard = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._guard:
            events = self._events.get(key)
            if events is None:
                events = []
                self._events[key] = events
            while events and events[0] <= cutoff:
                events.pop(0)
            if len(events) >= self.limit:
                return False
            events.append(current)
            if len(self._events) > 4096:
                stale = [
                    name for name, kept in self._events.items() if not kept or kept[-1] <= cutoff
                ]
                for name in stale:
                    self._events.pop(name, None)
            return True


def service_owner_id(service: GatewayService) -> str:
    return str(getattr(service, "OWNER_ID", "eurus"))


def create_app(ctx: GatewayContext, *, static_dir: Path | None = None) -> FastAPI:
    service = GatewayService(ctx)
    ctx.realtime_bridge = build_realtime_bridge(ctx)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await ctx.runtime.start()
        ctx.hub.start_tailer()
        autonomy = AutonomyService(ctx.config, channels={"web": _web_delivery_channel(ctx.hub)})
        await autonomy.start()
        ctx.autonomy = autonomy
        log.info("EURUS web gateway started version=%s", __version__)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await autonomy.stop()
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
            content=_error_payload("internal", "internal gateway error; see server logs"),
        )

    api_limiter = SlidingWindowRateLimiter(window_seconds=60.0, limit=240)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/api"):
            exempt = path == "/api/health" and request.method == "GET"
            allowed, reason = ctx.security.check_http(request, require_token=not exempt)
            if not allowed:
                status = 401 if reason == "unauthorized" else 403
                return JSONResponse(status_code=status, content=_error_payload("forbidden", reason))
            if not exempt and not api_limiter.allow("token"):
                return JSONResponse(
                    status_code=429,
                    content=_error_payload("rate_limited", "too many requests; slow down"),
                    headers={"Retry-After": "10"},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api") else "no-cache"
        return response

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(ctx.uptime, 1),
            "runtime": ctx.runtime.health(),
            "web_ui": dict(ctx.web_ui),
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

    @app.get("/api/media")
    async def list_media(
        session_id: str | None = None, limit: int = Query(default=200, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        return service.list_media(session_id, limit)

    @app.post("/api/media", status_code=201)
    async def upload_media(
        session_id: str = Form(...), file: UploadFile = File(...)
    ) -> dict[str, Any]:
        data = await read_upload(file, MAX_UPLOAD_BYTES)
        return await service.upload_media(
            session_id, file.filename or "file", file.content_type, data
        )

    @app.get("/api/media/{media_id}")
    async def get_media(media_id: str) -> dict[str, Any]:
        return await service.get_media(media_id)

    @app.get("/api/media/{media_id}/content")
    async def media_content(media_id: str) -> FileResponse:
        path, media = await service.media_content(media_id)
        disposition = "attachment" if media.mime == "text/html" else "inline"
        return FileResponse(
            path,
            media_type=media.mime,
            filename=media.filename,
            headers={"Content-Disposition": f'{disposition}; filename="{media.filename}"'},
        )

    @app.delete("/api/media/{media_id}")
    async def delete_media(media_id: str) -> dict[str, Any]:
        deleted = await service.delete_media(media_id)
        if not deleted:
            raise ServiceError("not_found", f"unknown media {media_id}")
        return {"deleted": True, "id": media_id}

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

    @app.get("/api/config")
    async def read_config() -> dict[str, Any]:
        return service.read_config()

    @app.patch("/api/config")
    async def patch_config(payload: ConfigPatchRequest) -> dict[str, Any]:
        return await service.patch_config(payload.section, payload.values)

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

    @app.get("/api/logs")
    async def logs(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.log_snapshot(after_sequence, limit)

    @app.get("/api/missions")
    async def list_missions(
        status: str | None = None, limit: int = Query(default=50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        statuses = tuple(item.strip() for item in (status or "").split(",") if item.strip())
        return service.list_missions(statuses=statuses, limit=limit)

    @app.post("/api/missions", status_code=201)
    async def create_mission(payload: MissionCreateRequest) -> dict[str, Any]:
        return service.create_mission(payload.model_dump())

    @app.get("/api/missions/analytics")
    async def mission_analytics() -> dict[str, Any]:
        return service.mission_analytics()

    @app.get("/api/missions/{mission_id}")
    async def mission_detail(mission_id: str) -> dict[str, Any]:
        return service.mission_detail(mission_id)

    @app.post("/api/missions/{mission_id}/plan")
    async def plan_mission(mission_id: str, payload: MissionPlanRequest) -> dict[str, Any]:
        return service.plan_mission(mission_id, payload.tasks)

    @app.post("/api/missions/{mission_id}/start")
    async def start_mission(mission_id: str) -> dict[str, Any]:
        return service.start_mission(mission_id)

    @app.post("/api/missions/{mission_id}/cancel")
    async def cancel_mission(mission_id: str, payload: MissionCancelRequest) -> dict[str, Any]:
        return service.cancel_mission(mission_id, payload.reason)

    @app.post("/api/missions/{mission_id}/resume")
    async def resume_mission(mission_id: str) -> dict[str, Any]:
        return service.resume_mission(mission_id)

    @app.post("/api/missions/{mission_id}/dispatch")
    async def dispatch_mission(mission_id: str, payload: MissionDispatchRequest) -> dict[str, Any]:
        return service.dispatch_mission(mission_id, payload.limit)

    @app.post("/api/missions/{mission_id}/finalize")
    async def finalize_mission(mission_id: str, payload: MissionFinalizeRequest) -> dict[str, Any]:
        return service.finalize_mission(mission_id, payload.require_checks)

    @app.get("/api/missions/{mission_id}/tasks")
    async def mission_tasks(mission_id: str) -> list[dict[str, Any]]:
        return service.mission_tasks(mission_id)

    @app.get("/api/missions/{mission_id}/artifacts")
    async def mission_artifacts(mission_id: str) -> list[dict[str, Any]]:
        return service.mission_artifacts(mission_id)

    @app.get("/api/missions/{mission_id}/verifications")
    async def mission_verifications(mission_id: str) -> list[dict[str, Any]]:
        return service.mission_verifications(mission_id)

    @app.get("/api/missions/{mission_id}/reviews")
    async def mission_reviews(mission_id: str) -> list[dict[str, Any]]:
        return service.mission_reviews(mission_id)

    @app.get("/api/missions/{mission_id}/learning")
    async def mission_learning(mission_id: str) -> dict[str, Any]:
        return service.mission_learning_view(mission_id)

    @app.get("/api/artifacts")
    async def artifacts_list(
        run_id: str = "",
        limit: int = Query(default=50, ge=1, le=500),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return service.list_artifacts(run_id=run_id, limit=limit, cursor=cursor)

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact_detail(artifact_id: str) -> dict[str, Any]:
        return service.artifact_detail(artifact_id)

    @app.get("/api/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str) -> Response:
        data, media_type = service.artifact_content(artifact_id)
        return Response(content=data, media_type=media_type)

    @app.get("/api/autonomy")
    async def autonomy_status() -> dict[str, Any]:
        return service.autonomy_status()

    @app.post("/api/autonomy")
    async def autonomy_toggle(payload: AutonomyToggleRequest) -> dict[str, Any]:
        return service.autonomy_toggle(payload.subsystem, payload.enabled)

    @app.get("/api/schedules")
    async def list_schedules(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.list_schedules(limit)

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(payload: ScheduleCreateRequest) -> dict[str, Any]:
        values = payload.model_dump()
        if payload.telegram_chat_id:
            values["deliver"] = f"telegram:{payload.telegram_chat_id}"
        elif payload.deliver in (None, "telegram"):
            values["deliver"] = "web"
        return service.create_schedule(values)

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: str, payload: ScheduleUpdateRequest) -> dict[str, Any]:
        return service.update_schedule(schedule_id, payload.model_dump(exclude_unset=True))

    @app.delete("/api/schedules/{schedule_id}")
    async def remove_schedule(schedule_id: str) -> dict[str, Any]:
        return service.remove_schedule(schedule_id)

    @app.post("/api/schedules/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str) -> dict[str, Any]:
        return service.pause_schedule(schedule_id)

    @app.post("/api/schedules/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str) -> dict[str, Any]:
        return service.resume_schedule(schedule_id)

    @app.post("/api/schedules/{schedule_id}/run", status_code=202)
    async def run_schedule_now(schedule_id: str) -> dict[str, Any]:
        return service.run_schedule_now(schedule_id)

    @app.post("/api/schedules/{schedule_id}/dry-run")
    async def dry_run_schedule(schedule_id: str) -> dict[str, Any]:
        return await service.dry_run_schedule(schedule_id)

    @app.get("/api/jobs")
    async def list_jobs(
        status: str | None = None, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        statuses = tuple(item.strip() for item in (status or "").split(",") if item.strip())
        return service.list_jobs(statuses=statuses, limit=limit)

    @app.get("/api/deliveries")
    async def list_deliveries(
        status: str | None = None,
        channel: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        statuses = tuple(item.strip() for item in (status or "").split(",") if item.strip())
        return service.list_deliveries(statuses=statuses, channel=channel, limit=limit)

    @app.get("/api/skills/drafts")
    async def list_skill_drafts() -> list[dict[str, Any]]:
        return service.list_skill_drafts()

    @app.post("/api/skills/drafts/{name}/promote")
    async def promote_skill_draft(name: str, payload: SkillPromoteRequest) -> dict[str, Any]:
        return await service.promote_skill_draft(name, payload.outcomes, payload.checks)

    @app.get("/api/realtime/status")
    async def realtime_status() -> dict[str, Any]:
        bridge = ctx.realtime_bridge
        if bridge is None:
            return {"enabled": False, "detail": "realtime bridge is not initialized"}
        view: dict[str, Any] = bridge.status().view()
        return view

    @app.post("/api/realtime/session")
    async def create_realtime_session(payload: RealtimeSessionRequest) -> Any:
        if payload.session_id not in {row["id"] for row in ctx.store.list_sessions()}:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "detail": "unknown session"}},
            )
        try:
            return ctx.realtime_bridge.create_session(
                payload.session_id, owner_id=service_owner_id(service)
            )
        except RealtimeBridgeError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "conflict", "detail": str(exc)}},
            )

    @app.websocket("/api/realtime/stream/{bridge_id}")
    async def realtime_stream(websocket: WebSocket, bridge_id: str) -> None:
        allowed, reason = ctx.security.check_websocket(websocket)
        if not allowed:
            code = WS_CLOSE_AUTH if reason == "unauthorized" else WS_CLOSE_POLICY
            await websocket.close(code=code)
            return
        try:
            ctx.realtime_bridge.session_for(bridge_id, owner_id=service_owner_id(service))
        except RealtimeBridgeError:
            await websocket.close(code=4404)
            return
        await ctx.realtime_bridge.stream(websocket, bridge_id)

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

    dist, static_source = resolve_static_dir(static_dir)
    ctx.web_ui = {
        "available": dist is not None,
        "source": static_source,
        "path": str(dist) if dist is not None else None,
    }
    if dist is None:
        log.warning(
            "web UI assets unavailable; the gateway serves the API only "
            "(set %s to a built frontend directory, see docs/installation.md)",
            STATIC_DIR_ENV,
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content=_error_payload("not_found", f"unknown API route /{full_path}"),
            )
        if dist is None:
            return HTMLResponse(_frontend_missing_page(), status_code=503)
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            if "." in candidate.name:
                return JSONResponse(
                    status_code=404,
                    content=_error_payload("not_found", f"unknown asset /{full_path}"),
                )
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


def _usable_frontend_dir(path: Path) -> bool:
    return path.is_dir() and (path / "index.html").is_file()


def resolve_static_dir(static_dir: Path | None = None) -> tuple[Path | None, str]:
    override = os.environ.get(STATIC_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if _usable_frontend_dir(path):
            return path.resolve(), "environment"
        log.warning(
            "%s points to %s which is not a directory containing index.html; ignoring it",
            STATIC_DIR_ENV,
            override,
        )
    if static_dir is not None:
        if _usable_frontend_dir(static_dir):
            return static_dir.resolve(), "explicit"
        log.warning(
            "configured static directory %s does not contain index.html; ignoring it",
            static_dir,
        )
    packaged = Path(__file__).resolve().parent / PACKAGED_STATIC_DIRNAME
    if _usable_frontend_dir(packaged):
        return packaged, "packaged"
    repository = Path(__file__).resolve().parents[4] / "frontend" / "dist"
    if _usable_frontend_dir(repository):
        return repository, "repository"
    return None, "unavailable"


def _frontend_missing_page() -> str:
    return (
        "<h1>EURUS Web Gateway</h1>"
        "<p>The API is running, but no web UI build is available at this location.</p>"
        "<ul>"
        "<li>Use a wheel that bundles the web UI, or build it from source: "
        "<code>npm ci &amp;&amp; npm run build</code> inside <code>frontend/</code> "
        "of the source tree.</li>"
        f"<li>Or set <code>{STATIC_DIR_ENV}</code> to a directory containing the built "
        "frontend (its <code>index.html</code>).</li>"
        "</ul>"
        "<p>See docs/installation.md in the project repository.</p>"
    )


def build_context(
    config: Any,
    *,
    security: Any,
    approval_timeout: float = 600.0,
) -> GatewayContext:
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
        async def startup(self, sockets: Any = None) -> None:
            await super().startup(sockets=sockets)
            print()
            print("EURUS Web Gateway is ready:")
            print(f"  {url}")
            ui = getattr(ctx, "web_ui", {}) or {}
            if ui.get("available"):
                print(f"  Web UI: {ui.get('source')} ({ui.get('path')})")
            else:
                print(
                    "  Web UI: unavailable; the gateway serves the API only."
                    " Set EURUS_GATEWAY_STATIC to a built frontend directory"
                    " (see docs/installation.md)."
                )
            print("  (keep this terminal open; press Ctrl+C to stop)")
            print()
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(url)

    server = BannerServer(config)
    await server.serve()
    await asyncio.sleep(0)
