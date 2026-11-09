import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eurus.audio.realtime_openai import (
    OpenAIRealtimeSession,
    RealtimeSessionError,
    build_realtime_transport,
)
from eurus.config import build_config
from eurus.errors import ConfigError
from eurus.gateway.web.realtime import RealtimeBridge, RealtimeBridgeError
from eurus.tools.base import Tool, ToolContext, ToolRegistry

PCM_BYTES = b"\x01\x00" * 128


def make_config(realtime_model: str = "") -> Any:
    raw: dict[str, Any] = {
        "providers": {
            "openai": {
                "protocol": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            }
        },
        "agent": {"model": "openai/gpt-4o"},
    }
    if realtime_model:
        raw["audio"] = {"realtime_model": realtime_model}
    return build_config(raw, environ={"OPENAI_API_KEY": "k"})


class ScriptedUpstream:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def serve(self, websocket: Any) -> None:
        await websocket.send(json.dumps({"type": "session.created", "session": {}}))
        try:
            async for raw in websocket:
                event = json.loads(raw)
                self.received.append(event)
                if event.get("type") == "session.update":
                    await websocket.send(json.dumps({"type": "session.updated"}))
                elif event.get("type") == "input_audio_buffer.append":
                    await websocket.send(
                        json.dumps(
                            {"type": "input_audio_buffer.speech_started", "audio_start_ms": 0}
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "transcript": "halo eurus",
                            }
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "response.function_call_arguments.done",
                                "call_id": "call-1",
                                "name": "get_time",
                                "arguments": "{}",
                            }
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "response.audio.delta",
                                "delta": base64.b64encode(PCM_BYTES).decode("ascii"),
                            }
                        )
                    )
                    await websocket.send(json.dumps({"type": "response.audio.done"}))
                    await websocket.send(
                        json.dumps({"type": "response.done", "response": {"status": "completed"}})
                    )
                elif event.get("type") == "conversation.item.create":
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "response.audio.delta",
                                "delta": base64.b64encode(PCM_BYTES).decode("ascii"),
                            }
                        )
                    )
                    await websocket.send(json.dumps({"type": "response.audio.done"}))
                    await websocket.send(
                        json.dumps({"type": "response.done", "response": {"status": "completed"}})
                    )
        except Exception:
            pass


async def test_realtime_session_connect_and_wire_events() -> None:
    import websockets

    upstream = ScriptedUpstream()
    server = await websockets.serve(upstream.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        from eurus.catalog import BUILTIN_PROVIDERS

        session = OpenAIRealtimeSession(
            BUILTIN_PROVIDERS["openai"],
            "k",
            model="gpt-4o-realtime-preview",
            instructions="You are EURUS.",
            voice="alloy",
            tools=[{"type": "function", "name": "get_time", "description": "clock"}],
            base_url=f"http://127.0.0.1:{port}/v1",
        )
        await session.connect()
        assert session.state == "listening"
        await session.send_audio(PCM_BYTES)
        seen: list[dict[str, Any]] = []
        async for event in session.events():
            seen.append(event)
            if event.get("type") == "response.done":
                break
        await session.send_tool_result("call-1", json.dumps({"time": "now"}))
        await asyncio.sleep(0.2)
        await session.close()
        types = [event.get("type") for event in seen]
        assert "input_audio_buffer.speech_started" in types
        assert "conversation.item.input_audio_transcription.completed" in types
        assert "response.audio.delta" in types
        assert "response.done" in types
        sent_types = [event.get("type") for event in upstream.received]
        assert sent_types[0] == "session.update"
        update = upstream.received[0]["session"]
        assert update["instructions"] == "You are EURUS."
        assert update["voice"] == "alloy"
        assert update["input_audio_format"] == "pcm16"
        assert update["turn_detection"]["type"] == "server_vad"
        assert update["tools"][0]["name"] == "get_time"
        assert "input_audio_buffer.append" in sent_types
        append = next(e for e in upstream.received if e["type"] == "input_audio_buffer.append")
        assert base64.b64decode(append["audio"]) == PCM_BYTES
        assert any(e["type"] == "conversation.item.create" for e in upstream.received)
        tool_output = next(e for e in upstream.received if e["type"] == "conversation.item.create")
        assert tool_output["item"]["call_id"] == "call-1"
        assert session.state == "closed"
    finally:
        server.close()
        await server.wait_closed()


async def test_realtime_session_interrupt_sends_cancel() -> None:
    import websockets

    upstream = ScriptedUpstream()
    server = await websockets.serve(upstream.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        from eurus.catalog import BUILTIN_PROVIDERS

        session = OpenAIRealtimeSession(
            BUILTIN_PROVIDERS["openai"],
            "k",
            model="gpt-4o-realtime-preview",
            base_url=f"http://127.0.0.1:{port}/v1",
        )
        await session.connect()
        await session.interrupt()
        await asyncio.sleep(0.1)
        sent_types = [event.get("type") for event in upstream.received]
        assert "response.cancel" in sent_types
        assert "input_audio_buffer.clear" in sent_types
        await session.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_realtime_session_connect_failure() -> None:
    from eurus.catalog import BUILTIN_PROVIDERS

    session = OpenAIRealtimeSession(
        BUILTIN_PROVIDERS["openai"],
        "k",
        model="gpt-4o-realtime-preview",
        connect_timeout=0.5,
        base_url="http://127.0.0.1:9/v1",
    )
    with pytest.raises(RealtimeSessionError, match="realtime connection"):
        await session.connect()


def test_build_realtime_transport_requires_capability() -> None:
    config = make_config("openai/gpt-4o")
    with pytest.raises(ConfigError, match="realtime audio support"):
        build_realtime_transport(config)


def test_build_realtime_transport_requires_openai_protocol() -> None:
    raw: dict[str, Any] = {
        "providers": {"goog": {"protocol": "gemini", "base_url": "https://x", "api_key_env": "G"}},
        "agent": {"model": "goog/gemini-2.0"},
        "audio": {"realtime_model": "goog/gemini-2.0"},
    }
    config = build_config(raw, environ={"G": "g"})
    with pytest.raises(ConfigError, match="OpenAI-compatible"):
        build_realtime_transport(config)


def test_build_realtime_transport_none_without_model() -> None:
    assert build_realtime_transport(make_config()) is None


def test_build_realtime_transport_constructs() -> None:
    transport = build_realtime_transport(make_config("openai/gpt-4o-realtime-preview"))
    assert transport is not None
    assert transport.model == "gpt-4o-realtime-preview"


class FakeRealtimeSession:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.audio_sent: list[bytes] = []
        self.interrupts = 0
        self.tool_results: list[tuple[str, str]] = []
        self.closed = False
        self.state = "listening"
        self._events: list[dict[str, Any]] = list(events or [])
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self._events:
            self._queue.put_nowait(event)

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_sent.append(pcm)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def reconnect(self) -> None:
        return None

    async def send_tool_result(self, call_id: str, output: str) -> None:
        self.tool_results.append((call_id, output))

    async def close(self) -> None:
        self.closed = True

    async def events(self) -> Any:
        while True:
            event = await self._queue.get()
            yield event


class FakeTransport:
    def __init__(self, session: FakeRealtimeSession, fail: Exception | None = None) -> None:
        self.session = session
        self.fail = fail
        self.model = "gpt-4o-realtime-preview"
        self.voice = "alloy"
        self.opened = 0

    async def open_session(self, *, instructions: str = "", tools: Any = ()) -> FakeRealtimeSession:
        self.opened += 1
        self.instructions = instructions
        self.tools = list(tools)
        if self.fail is not None:
            raise self.fail
        return self.session


class FakeStore:
    def __init__(self) -> None:
        self.sessions = [{"id": "s-1", "title": "chat"}]

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.sessions

    def load_messages(self, session_id: str) -> list[Any]:
        class Msg:
            def __init__(self, role: str, content: str) -> None:
                self.role = role
                self.content = content

        return [Msg("user", "kita bahas rencana EURUS"), Msg("assistant", "siap")]


async def _noop(*args: Any, **kwargs: Any) -> str:
    return "12:00"


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def get_time(arguments: dict[str, Any], context: ToolContext) -> str:
        del arguments, context
        return "12:00 UTC"

    async def mutate(arguments: dict[str, Any], context: ToolContext) -> str:
        del arguments, context
        return "changed"

    registry.register(
        Tool(
            name="get_time",
            description="current time",
            parameters={"type": "object", "properties": {}},
            handler=get_time,
        )
    )
    registry.register(
        Tool(
            name="dangerous_write",
            description="mutating",
            parameters={"type": "object", "properties": {}},
            handler=mutate,
            mutates=True,
            requires_approval=True,
        )
    )
    return registry


def make_bridge(
    transport: Any, config: Any = None, max_concurrent: int = 4
) -> tuple[RealtimeBridge, Any]:
    ctx = SimpleNamespace()
    ctx.config = config or make_config()
    ctx.store = FakeStore()
    ctx.runtime = SimpleNamespace(registry=make_registry(), OWNER_ID="eurus")
    bridge = RealtimeBridge(ctx, transport, max_concurrent=max_concurrent)
    return bridge, ctx


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: int | None = None
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.incoming.get(), timeout=5.0)

    def client_disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect"})

    def client_audio(self, data: bytes) -> None:
        self.incoming.put_nowait({"bytes": data})

    def client_json(self, payload: dict[str, Any]) -> None:
        self.incoming.put_nowait({"text": json.dumps(payload)})


def test_bridge_status_without_transport() -> None:
    bridge, _ctx = make_bridge(None)
    status = bridge.status()
    assert status.enabled is False
    assert "not configured" in status.detail


async def test_bridge_create_session_governance() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    view = bridge.create_session("s-1", "eurus")
    assert view["model"] == "gpt-4o-realtime-preview"
    assert view["sample_rate"] == 24000
    assert view["bridge_id"]
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()), max_concurrent=1)
    bridge.create_session("s-1", "eurus")
    occupying = asyncio.current_task()
    assert occupying is not None
    bridge._active["occupied"] = occupying
    with pytest.raises(RealtimeBridgeError, match="too many concurrent"):
        bridge.create_session("s-1", "eurus")


def test_bridge_session_for_owner_isolation() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    view = bridge.create_session("s-1", "eurus")
    with pytest.raises(RealtimeBridgeError, match="another owner"):
        bridge.session_for(view["bridge_id"], "someone-else")
    assert bridge.session_for(view["bridge_id"], "eurus").session_id == "s-1"


def test_bridge_session_for_expired() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    view = bridge.create_session("s-1", "eurus")
    entry = bridge._sessions[view["bridge_id"]]
    entry.expires_at = time.monotonic() - 1
    with pytest.raises(RealtimeBridgeError, match="expired"):
        bridge.session_for(view["bridge_id"], "eurus")


def test_bridge_realtime_tools_filters_mutating() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    tools = bridge.realtime_tools()
    names = [tool["name"] for tool in tools]
    assert "get_time" in names
    assert "dangerous_write" not in names


def test_bridge_conversation_instructions_includes_context() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    instructions = bridge.conversation_instructions("s-1")
    assert "EURUS" in instructions
    assert "rencana" in instructions


async def test_bridge_stream_relays_audio_and_tools() -> None:
    session = FakeRealtimeSession(
        events=[
            {"type": "input_audio_buffer.speech_started", "audio_start_ms": 0},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "jam berapa sekarang",
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "name": "get_time",
                "arguments": "{}",
            },
            {
                "type": "response.audio.delta",
                "delta": base64.b64encode(PCM_BYTES).decode("ascii"),
            },
            {"type": "response.audio.done"},
            {"type": "response.done", "response": {"status": "completed"}},
            {"__closed__": True},
        ]
    )
    transport = FakeTransport(session)
    bridge, _ctx = make_bridge(transport)
    view = bridge.create_session("s-1", "eurus")
    websocket = FakeWebSocket()
    stream_task = asyncio.create_task(bridge.stream(websocket, view["bridge_id"]))
    await asyncio.sleep(0.05)
    websocket.client_audio(PCM_BYTES)
    await asyncio.sleep(0.2)
    websocket.client_json({"type": "interrupt"})
    await asyncio.sleep(0.1)
    websocket.client_disconnect()
    await asyncio.wait_for(stream_task, timeout=5.0)
    assert session.audio_sent == [PCM_BYTES]
    assert session.interrupts == 1
    assert session.tool_results
    call_id, output = session.tool_results[0]
    assert call_id == "call-1"
    assert "12:00" in output
    sent_types = [message.get("type") for message in websocket.sent]
    assert "session.ready" in sent_types
    assert "user_transcript" in sent_types
    assert "assistant_audio_delta" in sent_types
    assert "response_done" in sent_types
    assert "tool_call" in sent_types
    assert "tool_result" in sent_types
    assert session.closed is True
    assert bridge._active == {}


async def test_bridge_stream_rejects_mutating_tools() -> None:
    session = FakeRealtimeSession(
        events=[
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-9",
                "name": "dangerous_write",
                "arguments": "{}",
            },
            {"__closed__": True},
        ]
    )
    bridge, _ctx = make_bridge(FakeTransport(session))
    view = bridge.create_session("s-1", "eurus")
    websocket = FakeWebSocket()
    stream_task = asyncio.create_task(bridge.stream(websocket, view["bridge_id"]))
    await asyncio.sleep(0.2)
    websocket.client_disconnect()
    await asyncio.wait_for(stream_task, timeout=5.0)
    call_id, output = session.tool_results[0]
    assert call_id == "call-9"
    assert "tool_unavailable" in output


async def test_bridge_stream_provider_failure() -> None:
    transport = FakeTransport(FakeRealtimeSession(), fail=RuntimeError("no key"))
    bridge, _ctx = make_bridge(transport)
    view = bridge.create_session("s-1", "eurus")
    websocket = FakeWebSocket()
    await asyncio.wait_for(bridge.stream(websocket, view["bridge_id"]), timeout=5.0)
    assert websocket.closed == 1011
    assert any(message.get("type") == "error" for message in websocket.sent)


async def test_bridge_stream_enforces_deadline() -> None:
    session = FakeRealtimeSession()
    config = make_config()
    object.__setattr__(
        config.audio,
        "realtime_session_seconds",
        30,
    )
    bridge, _ctx = make_bridge(FakeTransport(session), config=config)
    view = bridge.create_session("s-1", "eurus")
    entry = bridge._sessions[view["bridge_id"]]
    entry.deadline_at = time.monotonic() - 1
    websocket = FakeWebSocket()
    await asyncio.wait_for(bridge.stream(websocket, view["bridge_id"]), timeout=5.0)
    assert any(message.get("type") == "session.expired" for message in websocket.sent)
    assert session.closed is True
    assert view["bridge_id"] not in bridge._sessions


def test_bridge_stream_unknown_session_closes() -> None:
    bridge, _ctx = make_bridge(FakeTransport(FakeRealtimeSession()))
    websocket = FakeWebSocket()

    async def run() -> None:
        await bridge.stream(websocket, "missing")

    asyncio.run(run())
    assert websocket.closed == 4404


async def test_realtime_config_validation() -> None:
    from eurus.audio.config import AudioConfig

    with pytest.raises(ValueError):
        AudioConfig(realtime_session_seconds=5)
    with pytest.raises(ValueError):
        AudioConfig(realtime_timeout_seconds=0)
    with pytest.raises(ValueError):
        AudioConfig(realtime_voice=" ")


def test_realtime_config_env_overrides() -> None:
    from eurus.config import env_overrides

    config = build_config(
        env_overrides(
            {
                "EURUS_REALTIME_MODEL": "openai/gpt-4o-realtime-preview",
                "EURUS_REALTIME_VOICE": "verse",
                "EURUS_REALTIME_SESSION_SECONDS": "600",
            }
        ),
        environ={
            "EURUS_REALTIME_MODEL": "openai/gpt-4o-realtime-preview",
            "EURUS_REALTIME_VOICE": "verse",
            "EURUS_REALTIME_SESSION_SECONDS": "600",
        },
    )
    assert config.audio.realtime_model == "openai/gpt-4o-realtime-preview"
    assert config.audio.realtime_voice == "verse"
    assert config.audio.realtime_session_seconds == 600.0


async def test_final_realtime_status_route() -> None:
    from fastapi.testclient import TestClient

    from eurus.config import load_config
    from eurus.gateway.web.app import build_context, create_app
    from eurus.gateway.web.auth import GatewaySecurity

    (Path("/tmp") / "eurus-b3-final").mkdir(exist_ok=True)
    home = Path("/tmp") / "eurus-b3-final"
    (home / "config.toml").write_text(
        '[providers.dummy]\nprotocol = "openai"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'api_key_env = "DUMMY_KEY"\n\n[agent]\nmodel = "dummy/dummy"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=home, environ={"EURUS_HOME": str(home)})
    security = GatewaySecurity("unit-test-token", host="127.0.0.1", port=8712)
    ctx = build_context(config, security=security, approval_timeout=60)
    app = create_app(ctx)
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer unit-test-token"}
        response = client.get("/api/realtime/status", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        session_id = client.post("/api/sessions", json={"title": "s"}, headers=auth).json()["id"]
        denied = client.post("/api/realtime/session", json={"session_id": session_id}, headers=auth)
        assert denied.status_code == 409
    await ctx.runtime.stop()
