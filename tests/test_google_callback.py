from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path

import pytest

from eurus.mcp.auth.accounts import (
    STATUS_CONNECTED,
    GoogleAccountManager,
)
from eurus.mcp.auth.callback import CallbackServer
from eurus.mcp.auth.oauth import OAuthFlowError


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _send_raw(port: int, payload: bytes, *, read_reply: bool) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    reply = b""
    if read_reply:
        reply = await asyncio.wait_for(reader.read(8192), timeout=5.0)
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()
    return reply


async def _drain_until(server: CallbackServer, coro_factory) -> None:
    for _ in range(100):
        if coro_factory():
            return
        await asyncio.sleep(0.02)


async def test_idle_preconnect_does_not_fail_login() -> None:
    server = CallbackServer(port=_free_port())
    async with server:
        wait_task = asyncio.create_task(server.wait_for_callback(timeout=5.0))
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        await asyncio.sleep(0.1)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)
        assert not wait_task.done(), "idle preconnect must not finish the login"
        await _send_raw(
            server.bound_port,
            b"GET /callback?code=abc&state=st1 HTTP/1.1\r\nHost: x\r\n\r\n",
            read_reply=True,
        )
        result = await asyncio.wait_for(wait_task, timeout=5.0)
        assert result.code == "abc"
        assert result.state == "st1"


async def test_early_callback_before_wait_for_callback() -> None:
    server = CallbackServer(port=_free_port())
    async with server:
        reply = await _send_raw(
            server.bound_port,
            b"GET /callback?code=early&state=st2 HTTP/1.1\r\nHost: x\r\n\r\n",
            read_reply=True,
        )
        assert b"200" in reply.split(b"\r\n", 1)[0]
        result = await server.wait_for_callback(timeout=5.0)
        assert result.code == "early"


async def test_non_callback_path_keeps_waiting() -> None:
    server = CallbackServer(port=_free_port())
    async with server:
        wait_task = asyncio.create_task(server.wait_for_callback(timeout=3.0))
        await asyncio.sleep(0.05)
        await _send_raw(
            server.bound_port, b"GET /favicon.ico HTTP/1.1\r\nHost: x\r\n\r\n", read_reply=True
        )
        await asyncio.sleep(0.1)
        assert not wait_task.done()
        wait_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait_task


async def test_explicit_oauth_error_completes_with_error() -> None:
    server = CallbackServer(port=_free_port())
    async with server:
        wait_task = asyncio.create_task(server.wait_for_callback(timeout=5.0))
        await asyncio.sleep(0.05)
        await _send_raw(
            server.bound_port,
            b"GET /callback?error=access_denied&state=st3 HTTP/1.1\r\nHost: x\r\n\r\n",
            read_reply=True,
        )
        with pytest.raises(OAuthFlowError, match="access_denied"):
            await wait_task


async def test_complete_manually_parses_url_or_code() -> None:
    server = CallbackServer(port=_free_port())
    async with server:
        result = server.complete_manually("http://127.0.0.1:1/callback?code=pasted&state=st4")
        assert result.code == "pasted"
        assert result.state == "st4"
        with pytest.raises(Exception, match="state"):
            server.complete_manually("http://127.0.0.1:1/callback?code=x")


def test_account_status_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    manager = GoogleAccountManager(path)
    account = manager.add("alice@example.com")
    manager.set_status(account.account_id, STATUS_CONNECTED)

    reloaded = GoogleAccountManager(path)
    assert reloaded.status_of(account.account_id) == STATUS_CONNECTED
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["statuses"][account.account_id] == STATUS_CONNECTED


def test_unknown_status_values_are_dropped_on_load(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "active": "x-1",
                "accounts": [{"account_id": "x-1", "email": "a@b.c"}],
                "statuses": {"x-1": "weird-status"},
            }
        ),
        encoding="utf-8",
    )
    manager = GoogleAccountManager(path)
    assert manager.status_of("x-1") == "logged_out"


def test_corrupt_accounts_json_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text("{broken json", encoding="utf-8")
    manager = GoogleAccountManager(path)
    assert manager.list() == []
    assert manager.load_warnings, "corrupt file must produce a warning"
    backups = list(tmp_path.glob("accounts.json.corrupt-*"))
    assert backups, "corrupt file must be backed up, not overwritten"
    assert backups[0].read_text(encoding="utf-8") == "{broken json"
    manager.add("bob@example.com")
    assert path.exists() and json.loads(path.read_text(encoding="utf-8"))["accounts"]
