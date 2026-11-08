from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus.audio.realtime_openai import (
    REALTIME_SAMPLE_RATE,
    OpenAIRealtimeSession,
    OpenAIRealtimeTransport,
)
from eurus.errors import EurusError

log = logging.getLogger(__name__)

BRIDGE_MINT_TTL_SECONDS = 60.0
MAX_BRIDGE_SESSIONS = 4
MAX_CONTEXT_MESSAGES = 8
MAX_INSTRUCTION_CHARS = 12_000
TOOL_TIMEOUT_SECONDS = 60.0
REALTIME_SYSTEM_PREAMBLE = (
    "You are EURUS in a realtime voice conversation. Speak naturally and concisely in the "
    "user's language (default to Bahasa Indonesia when they speak Indonesian). Only read-only "
    "tools are available in voice mode; if a task needs file changes or approvals, tell the "
    "user to switch to text chat."
)


@dataclass(slots=True)
class BridgeSession:
    bridge_id: str
    owner_id: str
    session_id: str
    created_at: float
    expires_at: float
    deadline_at: float
    active: bool = False


@dataclass(slots=True)
class RealtimeStatus:
    enabled: bool
    model: str = ""
    voice: str = ""
    active_sessions: int = 0
    detail: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "voice": self.voice,
            "active_sessions": self.active_sessions,
            "detail": self.detail,
        }


class RealtimeBridgeError(EurusError):
    pass


class RealtimeBridge:
    def __init__(
        self,
        ctx: Any,
        transport: OpenAIRealtimeTransport | None,
        *,
        max_concurrent: int = MAX_BRIDGE_SESSIONS,
    ) -> None:
        self.ctx = ctx
        self.transport = transport
        self.max_concurrent = max(1, max_concurrent)
        self._sessions: dict[str, BridgeSession] = {}
        self._active: dict[str, asyncio.Task[None]] = {}

    def status(self) -> RealtimeStatus:
        if self.transport is None:
            return RealtimeStatus(enabled=False, detail="realtime voice is not configured")
        return RealtimeStatus(
            enabled=True,
            model=self.transport.model,
            voice=self.transport.voice,
            active_sessions=len(self._active),
        )

    def create_session(self, session_id: str, owner_id: str) -> dict[str, Any]:
        if self.transport is None:
            raise RealtimeBridgeError("realtime voice is not configured")
        self._evict_expired()
        if len(self._active) >= self.max_concurrent:
            raise RealtimeBridgeError(
                f"too many concurrent realtime sessions (limit {self.max_concurrent})"
            )
        now = time.monotonic()
        bridge_id = uuid.uuid4().hex
        config = self.ctx.config.audio
        self._sessions[bridge_id] = BridgeSession(
            bridge_id=bridge_id,
            owner_id=owner_id,
            session_id=session_id,
            created_at=now,
            expires_at=now + BRIDGE_MINT_TTL_SECONDS,
            deadline_at=now + config.realtime_session_seconds,
        )
        return {
            "bridge_id": bridge_id,
            "model": self.transport.model,
            "voice": self.transport.voice,
            "sample_rate": REALTIME_SAMPLE_RATE,
            "format": "pcm16",
            "expires_in": BRIDGE_MINT_TTL_SECONDS,
        }

    def session_for(self, bridge_id: str, owner_id: str) -> BridgeSession:
        entry = self._sessions.get(bridge_id)
        if entry is None:
            raise RealtimeBridgeError("unknown realtime bridge session")
        if entry.owner_id != owner_id:
            raise RealtimeBridgeError("realtime bridge session belongs to another owner")
        if entry.active:
            raise RealtimeBridgeError("realtime bridge session is already active")
        if time.monotonic() > entry.expires_at:
            self._sessions.pop(bridge_id, None)
            raise RealtimeBridgeError("realtime bridge session expired before connecting")
        return entry

    def conversation_instructions(self, session_id: str) -> str:
        parts: list[str] = [REALTIME_SYSTEM_PREAMBLE]
        try:
            messages = self.ctx.store.load_messages(session_id)
        except Exception:
            messages = []
        for message in messages[-MAX_CONTEXT_MESSAGES:]:
            content = (message.content or "").strip()
            if not content:
                continue
            speaker = "user" if message.role == "user" else "assistant"
            parts.append(f"{speaker}: {content[:600]}")
        instructions = "\n".join(parts)
        return instructions[:MAX_INSTRUCTION_CHARS]

    def realtime_tools(self) -> list[dict[str, Any]]:
        registry = getattr(self.ctx.runtime, "registry", None)
        if registry is None:
            return []
        tools: list[dict[str, Any]] = []
        for spec in registry.specs():
            tool = registry.get(spec.name)
            if tool is None:
                continue
            if tool.mutates or tool.requires_approval or tool.interactive:
                continue
            if not tool.parallel_safe:
                continue
            declaration: dict[str, Any] = {
                "type": "function",
                "name": spec.name,
                "description": spec.description[:1024],
            }
            if spec.parameters:
                declaration["parameters"] = spec.parameters
            tools.append(declaration)
        return tools

    async def stream(self, websocket: Any, bridge_id: str) -> None:
        entry = self._sessions.get(bridge_id)
        if entry is None:
            await websocket.close(code=4404)
            return
        if bridge_id in self._active:
            await websocket.close(code=4409)
            return
        await websocket.accept()
        if self.transport is None:
            await websocket.send_json(
                {"type": "error", "detail": "realtime voice is not configured"}
            )
            await websocket.close(code=1011)
            return
        task = asyncio.current_task()
        if task is None:
            return
        self._active[bridge_id] = task
        entry.active = True
        session: OpenAIRealtimeSession | None = None
        try:
            session = await self.transport.open_session(
                instructions=self.conversation_instructions(entry.session_id),
                tools=self.realtime_tools(),
            )
            await websocket.send_json(
                {
                    "type": "session.ready",
                    "model": self.transport.model,
                    "voice": self.transport.voice,
                    "sample_rate": REALTIME_SAMPLE_RATE,
                    "deadline_seconds": max(0, int(entry.deadline_at - time.monotonic())),
                }
            )
            await self._relay(websocket, session, entry)
        except Exception as exc:
            log.warning("realtime bridge failed bridge_id=%s error=%s", bridge_id, exc)
            try:
                await websocket.send_json(
                    {"type": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
                )
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            self._active.pop(bridge_id, None)
            if entry is not None:
                entry.active = False
            if session is not None:
                await session.close()
            self._sessions.pop(bridge_id, None)

    async def _relay(
        self, websocket: Any, session: OpenAIRealtimeSession, entry: BridgeSession
    ) -> None:
        events = session.events()
        relay = asyncio.create_task(self._pump_events(websocket, session, events, entry))
        try:
            while True:
                if time.monotonic() >= entry.deadline_at:
                    await websocket.send_json({"type": "session.expired"})
                    await websocket.close(code=1000)
                    break
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=min(5.0, max(0.5, entry.deadline_at - time.monotonic())),
                )
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes"):
                    await session.send_audio(message["bytes"])
                elif message.get("text"):
                    await self._handle_client_json(websocket, session, message["text"])
        except TimeoutError:
            await websocket.send_json({"type": "session.expired"})
            await websocket.close(code=1000)
        except Exception as exc:
            if "disconnect" not in type(exc).__name__.lower():
                log.debug("realtime relay ended: %s", exc)
        finally:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await relay

    async def _handle_client_json(
        self, websocket: Any, session: OpenAIRealtimeSession, raw: str
    ) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            await websocket.send_json({"type": "error", "detail": "invalid json"})
            return
        if not isinstance(message, dict):
            await websocket.send_json({"type": "error", "detail": "invalid frame"})
            return
        kind = str(message.get("type", ""))
        if kind == "interrupt":
            await session.interrupt()
        elif kind == "ping":
            await websocket.send_json({"type": "pong", "time": time.time()})
        else:
            await websocket.send_json({"type": "error", "detail": f"unknown type {kind}"})

    async def _pump_events(
        self,
        websocket: Any,
        session: OpenAIRealtimeSession,
        events: Any,
        entry: BridgeSession,
    ) -> None:
        async for event in events:
            if time.monotonic() >= entry.deadline_at:
                return
            forwarded = self._forward_event(event)
            if forwarded is not None:
                try:
                    await websocket.send_json(forwarded)
                except Exception:
                    return
            tool_call = self._pending_tool_call(event)
            if tool_call is not None:
                await self._execute_tool(websocket, session, tool_call, entry)

    def _forward_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(event.get("type", ""))
        if kind == "input_audio_buffer.speech_started":
            return {"type": "user_speech_started", "audio_start_ms": event.get("audio_start_ms", 0)}
        if kind == "conversation.item.input_audio_transcription.completed":
            return {"type": "user_transcript", "text": str(event.get("transcript", ""))}
        if kind == "response.audio_transcript.delta":
            return {"type": "assistant_transcript_delta", "delta": str(event.get("delta", ""))}
        if kind == "response.audio.delta":
            return {
                "type": "assistant_audio_delta",
                "audio": str(event.get("delta", "")),
                "mime": "audio/pcm;rate=24000",
            }
        if kind == "response.audio.done":
            return {"type": "assistant_audio_done"}
        if kind == "response.done":
            response = event.get("response") or {}
            status = str(response.get("status", "completed")) if isinstance(response, dict) else ""
            return {"type": "response_done", "status": status}
        if kind == "error":
            error = event.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            return {"type": "error", "detail": str(detail or "realtime error")[:400]}
        return None

    def _pending_tool_call(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if str(event.get("type", "")) != "response.function_call_arguments.done":
            return None
        return {
            "call_id": str(event.get("call_id", "")),
            "name": str(event.get("name", "")),
            "arguments": str(event.get("arguments", "") or "{}"),
        }

    async def _execute_tool(
        self,
        websocket: Any,
        session: OpenAIRealtimeSession,
        call: dict[str, Any],
        entry: BridgeSession,
    ) -> None:
        call_id = call["call_id"]
        name = call["name"]
        registry = getattr(self.ctx.runtime, "registry", None)
        tool = registry.get(name) if registry is not None else None
        if tool is None or tool.mutates or tool.requires_approval or tool.interactive:
            await session.send_tool_result(
                call_id,
                json.dumps(
                    {
                        "error": "tool_unavailable",
                        "detail": (
                            "This tool is not available in realtime voice mode; "
                            "ask the user to switch to text chat."
                        ),
                    }
                ),
            )
            return
        await websocket.send_json({"type": "tool_call", "name": name, "call_id": call_id})
        try:
            arguments = json.loads(call["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
        except ValueError as exc:
            await session.send_tool_result(
                call_id, json.dumps({"error": "invalid_arguments", "detail": str(exc)})
            )
            return
        from eurus.tools.base import ToolContext

        workspace = Path(self.ctx.config.permissions.workspace).expanduser().resolve()
        context = ToolContext(
            workspace=workspace,
            timeout=TOOL_TIMEOUT_SECONDS,
            session_id=entry.session_id,
            owner_id=self._owner_id(),
            run_id=f"realtime-{uuid.uuid4().hex[:12]}",
        )
        try:
            output = await asyncio.wait_for(tool.handler(arguments, context), TOOL_TIMEOUT_SECONDS)
            text = output.text if hasattr(output, "text") else str(output)
            is_error = bool(getattr(output, "is_error", False))
        except TimeoutError:
            text = json.dumps({"error": "tool_timeout", "detail": name})
            is_error = True
        except Exception as exc:
            text = json.dumps({"error": "tool_failed", "detail": f"{type(exc).__name__}: {exc}"})
            is_error = True
        await websocket.send_json(
            {"type": "tool_result", "name": name, "call_id": call_id, "is_error": is_error}
        )
        await session.send_tool_result(call_id, text)

    def _owner_id(self) -> str:
        return getattr(self.ctx.runtime, "OWNER_ID", "eurus")

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for bridge_id, entry in list(self._sessions.items()):
            if not entry.active and now > entry.expires_at:
                self._sessions.pop(bridge_id, None)


def build_realtime_bridge(ctx: Any) -> RealtimeBridge:
    from eurus.audio.realtime_openai import build_realtime_transport

    transport = None
    try:
        transport = build_realtime_transport(ctx.config)
    except EurusError as exc:
        log.info("realtime voice unavailable: %s", exc)
    return RealtimeBridge(ctx, transport)
