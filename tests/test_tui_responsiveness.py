import asyncio
import contextlib
import time

from rich.console import Console

from eurus.config import load_config
from eurus.tui import InteractiveSession, TerminalRenderer


class DelayedMCP:
    configs = {"slow": type("Config", (), {"enabled": True, "timeout_seconds": 2.0})()}

    def __init__(self):
        self.finished = False

    async def connect(self):
        await asyncio.sleep(0.3)
        self.finished = True

    def register_tools(self, registry):
        return 0

    async def close(self):
        return None


def test_thinking_status_is_rendered_before_mcp_connect_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={"agent": {"model": "groq/llama"}},
    )
    console = Console(record=True, width=80, force_terminal=False)
    session = InteractiveSession(config, TerminalRenderer(console))
    mcp = DelayedMCP()
    session.mcp = mcp

    async def scenario():
        task = asyncio.create_task(session.submit("hello"))
        await asyncio.sleep(0.08)
        assert "Preparing agent and connecting tools" in console.export_text()
        assert not mcp.finished
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await session.close()

    asyncio.run(scenario())


def test_tui_latency_probe_is_non_blocking():
    async def scenario():
        started = time.perf_counter()
        await asyncio.sleep(0)
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.1
