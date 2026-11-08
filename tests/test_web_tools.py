from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.agent import web_tools as web
from eurus.errors import ToolError
from eurus.execution.envelope import NetworkProfile
from eurus.network.policy import NetworkPolicyError, PolicySettings, build_policy_engine
from eurus.tools.base import ToolContext
from eurus.tools.builtin import default_registry


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_web_tools_are_registered_with_capability_tools() -> None:
    registry = default_registry(include_capability_tools=True)
    assert {"web_search", "web_fetch", "deep_search"}.issubset(set(registry.names()))


def test_policy_rejects_localhost_and_private_ip() -> None:
    engine = build_policy_engine(PolicySettings(profile=NetworkProfile.WEB_READ))
    with pytest.raises(NetworkPolicyError, match="forbidden private or local"):
        engine.check_url("http://127.0.0.1/admin")
    with pytest.raises(NetworkPolicyError, match="not reachable"):
        engine.check_url("http://localhost")


def test_policy_rejects_non_http_schemes() -> None:
    engine = build_policy_engine(PolicySettings(profile=NetworkProfile.WEB_READ))
    with pytest.raises(NetworkPolicyError, match="http or https"):
        engine.check_url("file:///etc/passwd")


def test_extract_fallback_removes_script_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "trafilatura", None)
    page = web._extract(
        "<html><body><h1>Title</h1><script>secret()</script><p>Useful text</p></body></html>",
        "https://example.com",
    )
    assert "Useful text" in page["text"]
    assert "secret" not in page["text"]


@pytest.mark.asyncio
async def test_search_web_formats_search_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_to_thread(function, *args):
        assert function is web._search_sync
        assert args == ("python web research", 3)
        return [{"title": "Docs", "url": "https://example.com/docs", "snippet": "Useful docs"}]

    monkeypatch.setattr(web.asyncio, "to_thread", fake_to_thread)
    output = await web._search_web({"query": "python web research", "limit": 3}, _context(tmp_path))
    data = json.loads(output)
    assert data["results"][0]["url"] == "https://example.com/docs"


@pytest.mark.asyncio
async def test_search_web_rejects_invalid_limit(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="limit must be between"):
        await web._search_web({"query": "test", "limit": 20}, _context(tmp_path))
