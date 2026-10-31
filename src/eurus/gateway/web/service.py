"""Interface-independent application services for the web gateway.

Every method here delegates to canonical EURUS stores and runtime objects
(``SessionStore``, ``RunStore``, ``EventStore``, ``AgentRuntime`` via
:class:`eurus.gateway.web.runtime.WebRuntime`, ``TomlManager``). No agent
logic, verification logic or persistence lives in this module — it only
orchestrates existing components and shapes the results for transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from eurus import __version__, paths
from eurus.bootstrap.configuration.toml_manager import TomlManager
from eurus.catalog import builtin_providers, is_ready
from eurus.config import Config, load_config
from eurus.errors import EurusError
from eurus.gateway.web.approval import WebApprovalBroker
from eurus.gateway.web.hub import (
    ACTIVE_STATUSES,
    SessionHub,
    WebRunHandle,
    is_active,
)
from eurus.gateway.web.runtime import OWNER_ID, WebRuntime
from eurus.logs import collect_secrets
from eurus.media import (
    MediaError,
    MediaStore,
    compose_attachment_block,
)
from eurus.providers.capabilities import capability_view
from eurus.runtime.models import Run
from eurus.sessions import SessionStore
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore

log = logging.getLogger(__name__)

EDITABLE_SECTIONS = (
    "agent",
    "auxiliary",
    "permissions",
    "logging",
    "network",
    "tools",
    "sandbox",
    "learning",
    "memory",
    "verification",
    "planning",
    "browser",
    "providers",
)

API_ERRORS = {
    "run_active": ("a run is already active for this session", 409),
    "not_found": ("resource not found", 404),
    "invalid": ("invalid request", 422),
}


class ServiceError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        message, status = API_ERRORS.get(code, ("internal error", 500))
        super().__init__(detail or message)
        self.code = code
        self.detail = detail or message
        self.status = status


@dataclass(slots=True)
class GatewayContext:
    """Everything the FastAPI routes need, wired once at startup."""

    config: Config
    store: SessionStore
    run_store: RunStore
    event_store: EventStore
    runtime: WebRuntime
    broker: WebApprovalBroker
    hub: SessionHub
    log_buffer: Any
    security: Any
    started_at: float
    config_revision: int = 1

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at


class GatewayService:
    """Orchestrates canonical EURUS components for the HTTP/WebSocket API."""

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._media_store: MediaStore | None = None

    @property
    def media(self) -> MediaStore:
        """Canonical media store (lazy: creates ~/.eurus/media on first use)."""
        if self._media_store is None:
            self._media_store = MediaStore(database=self.ctx.store.path)
        return self._media_store

    # -- helpers ---------------------------------------------------------------

    def _session_exists(self, session_id: str) -> bool:
        return any(row["id"] == session_id for row in self.ctx.store.list_sessions())

    def _active_rows(self) -> list[Run]:
        try:
            rows = self.ctx.run_store.list_runs(statuses=tuple(ACTIVE_STATUSES), limit=1000)
        except Exception:
            log.debug("active run query failed", exc_info=True)
            return []
        now = time.time()
        fresh = [run for run in rows if is_active(run, now=now)]
        in_process = {handle.run_id for handle in self.ctx.hub.web_runs.values()}
        known = {run.run_id for run in fresh}
        for run_id in in_process:
            if run_id not in known:
                run = self.ctx.run_store.get(run_id)
                if run is not None:
                    fresh.append(run)
        return fresh

    def _run_origin(self, run: Run) -> str:
        if run.owner_id == OWNER_ID or run.run_id in self.ctx.hub.web_runs:
            return "web"
        return "external"

    def _run_view(self, run: Run) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "objective": run.objective,
            "status": run.status.value,
            "stop_reason": run.stop_reason,
            "owner_id": run.owner_id,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "origin": self._run_origin(run),
        }

    @staticmethod
    def _message_views(messages: list[Any]) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            views.append(
                {
                    "sequence": index,
                    "role": message.role,
                    "content": message.content,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in message.tool_calls
                    ],
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                }
            )
        return views

    # -- sessions ---------------------------------------------------------------

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self.ctx.store.list_sessions)
        active_by_session: dict[str, str] = {}
        for run in self._active_rows():
            if run.session_id:
                active_by_session.setdefault(run.session_id, run.run_id)
        for row in rows:
            row["active_run_id"] = active_by_session.get(str(row["id"]))
        return rows

    async def create_session(self, title: str) -> dict[str, Any]:
        session_id = uuid.uuid4().hex

        def create() -> None:
            self.ctx.store.create_session(session_id, title)

        await asyncio.to_thread(create)
        return {
            "id": session_id,
            "title": title,
            "created_at": "",
            "updated_at": "",
            "message_count": 0,
            "active_run_id": None,
        }

    async def delete_session(self, session_id: str) -> bool:
        if self.ctx.hub.web_runs and any(
            handle.session_id == session_id for handle in self.ctx.hub.web_runs.values()
        ):
            raise ServiceError("run_active", "stop the active run before deleting")
        deleted = await asyncio.to_thread(self.ctx.store.delete_session, session_id)
        return deleted

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not await asyncio.to_thread(self._session_exists, session_id):
            raise ServiceError("not_found", f"unknown session {session_id}")
        messages = await asyncio.to_thread(self.ctx.store.load_messages, session_id)
        return self._message_views(messages)

    # -- chat execution ----------------------------------------------------------

    async def send_message(
        self, session_id: str, text: str, attachments: list[str] | None = None
    ) -> dict[str, Any]:
        text = text.strip()
        if not text and not attachments:
            raise ServiceError("invalid", "message text must not be empty")
        if not await asyncio.to_thread(self._session_exists, session_id):
            raise ServiceError("not_found", f"unknown session {session_id}")
        media_rows = await asyncio.to_thread(
            self._resolve_attachments, session_id, attachments or []
        )
        if media_rows:
            suffix = compose_attachment_block(media_rows)
            text = f"{text}\n{suffix}" if text else suffix
        if not text.strip():
            raise ServiceError("invalid", "message text must not be empty")
        for run in self._active_rows():
            if run.session_id == session_id:
                raise ServiceError(
                    "run_active", f"run {run.run_id} is already active for this session"
                )
        run_id = uuid.uuid4().hex
        handle = WebRunHandle(run_id=run_id, session_id=session_id)
        self.ctx.hub.register_web_run(handle)
        task = asyncio.create_task(self._execute_turn(handle, text), name=f"eurus-web-run-{run_id}")
        handle.task = task
        return {"session_id": session_id, "run_id": run_id}

    def _resolve_attachments(self, session_id: str, media_ids: list[str]) -> list[Any]:
        """Load media rows for a send, enforcing session ownership."""
        rows = []
        for media_id in media_ids[:8]:
            row = self.media.get(media_id)
            if row is None:
                raise ServiceError("not_found", f"unknown attachment {media_id}")
            if row.session_id != session_id:
                raise ServiceError("invalid", f"attachment {media_id} belongs to another session")
            rows.append(row)
        return rows

    # -- media ---------------------------------------------------------------------

    async def upload_media(
        self, session_id: str, filename: str, content_type: str | None, data: bytes
    ) -> dict[str, Any]:
        if not await asyncio.to_thread(self._session_exists, session_id):
            raise ServiceError("not_found", f"unknown session {session_id}")
        try:
            row = await asyncio.to_thread(
                self.media.save,
                session_id,
                filename,
                data,
                declared_mime=(content_type or None),
            )
        except MediaError as exc:
            raise ServiceError("invalid", str(exc)) from exc
        return row.view()

    async def get_media(self, media_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self.media.get, media_id)
        if row is None:
            raise ServiceError("not_found", f"unknown media {media_id}")
        return row.view()

    async def media_content(self, media_id: str) -> Any:
        """Return (path, media) for FileResponse; path is validated to exist."""
        from pathlib import Path

        row = await asyncio.to_thread(self.media.get, media_id)
        if row is None:
            raise ServiceError("not_found", f"unknown media {media_id}")
        path = Path(row.storage_path)
        if not path.is_file():
            raise ServiceError("not_found", f"media payload missing for {media_id}")
        return path, row

    async def delete_media(self, media_id: str) -> bool:
        return await asyncio.to_thread(self.media.delete, media_id)

    async def _execute_turn(self, handle: WebRunHandle, text: str) -> None:
        run_id = handle.run_id
        session_id = handle.session_id
        hub = self.ctx.hub

        async def on_event(event: Any) -> None:
            await hub.publish_agent_event(run_id, session_id, event)

        try:
            await self.ctx.runtime.run_turn(session_id, text, run_id, on_event=on_event)
        except asyncio.CancelledError:
            log.info("web run cancelled run_id=%s", run_id)
        except Exception as exc:
            log.warning("web run failed run_id=%s error_type=%s", run_id, type(exc).__name__)
            with contextlib.suppress(Exception):
                await hub.publish_run_status(
                    run_id, session_id, "failed", stop_reason=f"{type(exc).__name__}: {exc}"[:300]
                )
        finally:
            status = "failed"
            stop_reason = None
            try:
                run = await asyncio.to_thread(self.ctx.run_store.get, run_id)
                if run is not None:
                    status = run.status.value
                    stop_reason = run.stop_reason
            except Exception:
                log.debug("post-run status lookup failed run_id=%s", run_id, exc_info=True)
            with contextlib.suppress(Exception):
                await self.ctx.broker.cancel_task(run_id)
            with contextlib.suppress(Exception):
                await hub.publish_run_status(run_id, session_id, status, stop_reason=stop_reason)
            hub.release_web_run(run_id)
            with contextlib.suppress(Exception):
                await hub.broadcast(session_id, {"type": "sessions_changed"})

    # -- runs --------------------------------------------------------------------

    async def list_runs(self, session_id: str | None, limit: int = 100) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self.ctx.run_store.list_runs, limit=min(max(limit, 1), 1000))
        views = [self._run_view(run) for run in rows]
        if session_id is not None:
            views = [view for view in views if view["session_id"] == session_id]
        return views

    async def get_run(self, run_id: str) -> dict[str, Any]:
        run = await asyncio.to_thread(self.ctx.run_store.get, run_id)
        if run is None:
            raise ServiceError("not_found", f"unknown run {run_id}")
        return self._run_view(run)

    async def run_events(self, run_id: str, after_sequence: int) -> list[dict[str, Any]]:
        run = await asyncio.to_thread(self.ctx.run_store.get, run_id)
        if run is None:
            raise ServiceError("not_found", f"unknown run {run_id}")
        events = await asyncio.to_thread(
            self.ctx.event_store.list_for_run,
            run_id,
            after_sequence=max(0, after_sequence),
            limit=1000,
        )
        return [
            {
                "event_id": event.event_id,
                "run_id": event.run_id,
                "event_type": event.event_type,
                "sequence_no": event.sequence_no,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
                "step_id": event.step_id,
                "attempt_id": event.attempt_id,
                "parent_run_id": event.parent_run_id,
            }
            for event in events
        ]

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = await asyncio.to_thread(self.ctx.run_store.get, run_id)
        if run is None:
            raise ServiceError("not_found", f"unknown run {run_id}")
        if run.status in ACTIVE_STATUSES:
            requested = await asyncio.to_thread(self.ctx.run_store.request_cancel, run_id)
            if not requested:
                raise ServiceError("invalid", "run can no longer be cancelled")
            if run.run_id in self.ctx.hub.web_runs:
                handle = self.ctx.hub.web_runs[run.run_id]
                if handle.task is not None and not handle.task.done():
                    # The engine polls persisted cancellation; the task will
                    # finish on its own shortly. Waiting keeps the API honest.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(handle.task), timeout=10.0)
        return await self.get_run(run_id)

    # -- approvals ----------------------------------------------------------------

    async def pending_approvals(self) -> list[dict[str, Any]]:
        return await self.ctx.broker.pending_views()

    async def resolve_approval(self, token: str, session_id: str, allowed: bool) -> bool:
        resolved = await self.ctx.broker.resolve_for_session(
            token, session_id=session_id, allowed=allowed
        )
        if resolved:
            await self.ctx.hub.broadcast(
                session_id,
                {
                    "type": "approval_resolved",
                    "session_id": session_id,
                    "token": token,
                    "decision": "approved" if allowed else "denied",
                },
            )
        return resolved

    # -- runtime / dashboard ------------------------------------------------------

    def runtime_status(self) -> dict[str, Any]:
        active_rows = self._active_rows()
        active_views = [self._run_view(run) for run in active_rows]
        recent = self.ctx.run_store.list_runs(limit=25)
        return {
            "active_runs": active_views,
            "recent_runs": [self._run_view(run) for run in recent],
            "runtime_health": self.ctx.runtime.health(),
            "pending_approvals": len(self.ctx.broker.pending),
            "session_count": len(self.ctx.store.list_sessions()),
            "websocket_clients": self.ctx.hub.client_count,
        }

    def dashboard(self) -> dict[str, Any]:
        status = self.runtime_status()
        sessions = self.ctx.store.list_sessions()[:10]
        for row in sessions:
            row["active_run_id"] = next(
                (run["run_id"] for run in status["active_runs"] if run["session_id"] == row["id"]),
                None,
            )
        try:
            events = self.ctx.store.event_metrics(days=7)
        except Exception:
            events = {}
        return {
            "runtime": status,
            "recent_sessions": sessions,
            "session_events": events,
            "diagnostics": self.diagnostics(),
        }

    def diagnostics(self) -> dict[str, Any]:
        db_path = paths.database_path()
        config_path = paths.user_config_path()
        providers = []
        for name, spec in sorted(self.ctx.config.providers.items()):
            providers.append(
                {
                    "name": name,
                    "protocol": spec.protocol,
                    "ready": is_ready(spec) if spec.api_key_env else True,
                }
            )
        try:
            mcp = self.ctx.runtime.mcp_runtime.diagnostics()
        except Exception:
            mcp = {"servers": [], "warnings": ["MCP diagnostics unavailable"]}
        return {
            "version": __version__,
            "python": _python_version(),
            "home_dir": str(paths.home_dir()),
            "config_path": str(config_path),
            "config_exists": config_path.is_file(),
            "database_path": str(db_path),
            "database_size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "uptime_seconds": round(self.ctx.uptime, 1),
            "providers": providers,
            "mcp": mcp,
            "sandbox_backend": self.ctx.config.sandbox.backend,
            "workspace": self.ctx.config.permissions.workspace,
        }

    # -- configuration --------------------------------------------------------------

    def read_config(self) -> dict[str, Any]:
        config_path = paths.user_config_path()
        return {
            "config": self.ctx.config.to_dict(),
            "config_path": str(config_path),
            "revision": self.ctx.config_revision,
            "editable_sections": list(EDITABLE_SECTIONS),
        }

    async def patch_config(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        if section not in EDITABLE_SECTIONS:
            raise ServiceError("invalid", f"section '{section}' is not editable")
        if not isinstance(values, dict):
            raise ServiceError("invalid", "values must be an object")
        config_path = paths.user_config_path()

        def apply() -> None:
            manager = TomlManager.load(config_path)
            if section == "providers":
                self._patch_providers(manager, values)
            else:
                manager.update_section(section, values)
            manager.validate()
            manager.save()

        try:
            await asyncio.to_thread(apply)
        except Exception as exc:
            raise ServiceError("invalid", f"configuration rejected: {exc}") from exc

        self.ctx.config_revision += 1
        live_reload = False
        reload_error: str | None = None
        if not self.ctx.hub.web_runs:
            try:
                await self._reload_runtime()
                live_reload = True
            except Exception as exc:
                reload_error = f"{type(exc).__name__}: {exc}"
        return {
            "applied": True,
            "revision": self.ctx.config_revision,
            "live_reload": live_reload,
            "restart_required": not live_reload,
            "reload_error": reload_error,
        }

    @staticmethod
    def _patch_providers(manager: TomlManager, values: dict[str, Any]) -> None:
        """Merge provider specs; ``null`` values remove a provider entry field."""
        section = manager.section("providers")
        for name, spec in values.items():
            if spec is None:
                section.pop(name, None)
                continue
            if not isinstance(spec, dict):
                raise ValueError(f"provider '{name}' must be an object")
            entry = dict(section.get(name) or {})
            for key, value in spec.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            section[name] = entry
        manager.data["providers"] = section

    async def _reload_runtime(self) -> None:
        fresh = await asyncio.to_thread(load_config)
        secrets = collect_secrets(fresh)
        self.ctx.log_buffer.update_secrets(secrets)
        old_runtime = self.ctx.runtime
        new_runtime = WebRuntime(fresh, self.ctx.broker)
        await new_runtime.start()
        self.ctx.runtime = new_runtime
        self.ctx.config = fresh
        await old_runtime.stop()

    # -- providers / tools / memory ---------------------------------------------------

    def list_providers(self) -> list[dict[str, Any]]:
        builtin_names = set(builtin_providers())
        providers = []
        for name, spec in sorted(self.ctx.config.providers.items()):
            if spec.api_key_env:
                status = "ready" if is_ready(spec) else "missing key"
            else:
                status = "no key needed"
            view = capability_view(spec.protocol)
            providers.append(
                {
                    "name": name,
                    "protocol": spec.protocol,
                    "base_url": spec.base_url,
                    "api_key_env": spec.api_key_env,
                    "status": status,
                    "builtin": name in builtin_names,
                    "capabilities": view["capabilities"],
                    "multimodal": view["multimodal"],
                }
            )
        return providers

    def list_media(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        """Recent media rows from the canonical store (newest first)."""
        if session_id is not None and not self._session_exists(session_id):
            raise ServiceError("not_found", f"unknown session {session_id}")
        rows = self.media.list_recent(limit, session_id=session_id)
        return [row.view() for row in rows]

    async def discover_provider(self, name: str) -> dict[str, Any]:
        spec = self.ctx.config.providers.get(name)
        if spec is None:
            raise ServiceError("not_found", f"unknown provider {name}")
        from eurus.catalog import resolve_api_key
        from eurus.providers.discovery import discover

        try:
            result = await discover(spec, resolve_api_key(spec))
        except EurusError as exc:
            raise ServiceError("invalid", str(exc)) from exc
        return {
            "provider": result.provider,
            "error": result.error,
            "models": [
                {"id": model.id, "label": model.label, "context": model.context}
                for model in result.models
            ],
        }

    def list_tools(self) -> list[dict[str, Any]]:
        tools = []
        for tool in self.ctx.runtime.registry.all():
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "mutates": tool.mutates,
                    "requires_approval": tool.requires_approval,
                    "parallel_safe": tool.parallel_safe,
                    "parameters": tool.parameters,
                }
            )
        return sorted(tools, key=lambda item: item["name"])

    def mcp_status(self) -> dict[str, Any]:
        manager = self.ctx.runtime.mcp_runtime.manager
        rows = [
            {
                "name": row.get("name", ""),
                "transport": row.get("transport", ""),
                "enabled": bool(row.get("enabled", False)),
                "connected": bool(row.get("connected", False)),
                "tools": int(row.get("tools", 0)),
                "state": str(row.get("state", "")),
                "error": str(row.get("error") or ""),
            }
            for row in manager.status()
        ]
        return {"servers": rows, "diagnostics": self.ctx.runtime.mcp_runtime.diagnostics()}

    async def list_memories(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)

        def query_memories() -> list[dict[str, Any]]:
            if query.strip():
                return self.ctx.store.search_memories(query.strip(), limit=limit)
            return self.ctx.store.list_memories(limit=limit)

        rows = await asyncio.to_thread(query_memories)
        return [self._memory_view(row) for row in rows]

    async def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        def add() -> int:
            return self.ctx.store.add_memory(
                payload["content"],
                tags=payload.get("tags", ""),
                category=payload.get("category", "general"),
                memory_type=payload.get("memory_type", "fact"),
                confidence=payload.get("confidence", 0.7),
                source="user",
                trust="user_approved",
            )

        try:
            memory_id = await asyncio.to_thread(add)
        except ValueError as exc:
            raise ServiceError("invalid", str(exc)) from exc
        row = await asyncio.to_thread(self.ctx.store.get_memory, memory_id)
        if row is None:
            raise ServiceError("invalid", "memory could not be read back after insert")
        return self._memory_view(row)

    async def delete_memory(self, memory_id: int) -> bool:
        return await asyncio.to_thread(self.ctx.store.delete_memory, memory_id)

    async def set_memory_trust(self, memory_id: int, trust: str) -> bool:
        try:
            return await asyncio.to_thread(self.ctx.store.set_memory_trust, memory_id, trust)
        except ValueError as exc:
            raise ServiceError("invalid", str(exc)) from exc

    @staticmethod
    def _memory_view(row: dict[str, Any]) -> dict[str, Any]:
        provenance = row.get("provenance")
        if isinstance(provenance, str):
            with contextlib.suppress(ValueError, TypeError):
                provenance = json.loads(provenance)
        return {
            "id": int(row["id"]),
            "content": str(row["content"]),
            "tags": str(row.get("tags") or ""),
            "category": str(row.get("category") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "memory_type": str(row.get("memory_type") or ""),
            "confidence": float(row.get("confidence") or 0.0),
            "source": str(row.get("source") or ""),
            "use_count": int(row.get("use_count") or 0),
            "last_used_at": row.get("last_used_at"),
            "scope": str(row.get("scope") or "global"),
            "trust": str(row.get("trust") or "derived"),
            "expires_at": row.get("expires_at"),
            "provenance": provenance,
        }

    # -- logs ---------------------------------------------------------------------

    def log_snapshot(self, after_sequence: int, limit: int) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = self.ctx.log_buffer.snapshot(
            after_sequence=max(0, after_sequence), limit=min(max(limit, 1), 500)
        )
        return snapshot

    # -- lifecycle ------------------------------------------------------------------

    async def shutdown(self) -> None:
        for handle in list(self.ctx.hub.web_runs.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        if self.ctx.hub.web_runs:
            await asyncio.wait(
                [handle.task for handle in self.ctx.hub.web_runs.values() if handle.task],
                timeout=8.0,
            )
        await self.ctx.hub.stop_tailer()
        await self.ctx.runtime.stop()


def _python_version() -> str:
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
