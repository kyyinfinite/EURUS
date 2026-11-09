from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from eurus.config import ConfigError, load_config
from eurus.errors import ToolError
from eurus.execution.envelope import (
    ExecutionEnvelope,
    IsolationBackend,
    NetworkProfile,
)
from eurus.execution.sandbox import ContainerSandbox, ExecutionRequest
from eurus.network.policy import EgressPolicy, NetworkPolicyEngine
from eurus.network.proxy import EgressProxyServer
from eurus.tools.base import ToolContext
from eurus.tools.shell import shell_tool

PAGE_HTML = b"<html><body><p>Integration fetch works</p></body></html>"


class FakeStream:
    async def read(self, *args: Any) -> bytes:
        return b""


class FakeProcess:
    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = FakeStream()
        self.stderr = FakeStream()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


class Origin:
    def __init__(self, *, mode: str = "http", body: bytes = PAGE_HTML) -> None:
        self.mode = mode
        self.body = body
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            if self.mode == "redirect":
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/latest/meta-data/\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    + f"Content-Length: {len(self.body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + self.body
                )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()


def local_engine(origin_port: int, **policy_kwargs) -> NetworkPolicyEngine:
    policy = EgressPolicy(
        profile=NetworkProfile.WEB_READ,
        allowed_ports=frozenset({80, 443, origin_port}),
        allow_private_addresses=True,
        requests_per_minute=0,
        **policy_kwargs,
    )

    def resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    return NetworkPolicyEngine(policy, resolver=resolver)


def make_config(tmp_path: Path, overrides: dict[str, Any]) -> Any:
    return load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={
            "agent": {"model": "groq/llama-3.3-70b-versatile"},
            "network_security": {"proxy_bind": "127.0.0.1"},
            **overrides,
        },
    )


def make_router(config: Any) -> Any:
    from eurus.providers.factory import build_router
    from eurus.providers.testing import FakeTransport, openai_stream

    transport = FakeTransport(streams=[openai_stream("ok")])
    return build_router(config, transport=transport, environ={"GROQ_API_KEY": "k"})


class TestFactoryWiring:
    def test_non_offline_profile_builds_policy_and_proxy(self, tmp_path, monkeypatch) -> None:
        from eurus.agent.factory import build_agent

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = make_config(
            tmp_path,
            {"sandbox": {"network": True, "network_profile": "web_read"}},
        )
        agent = build_agent(config, make_router(config), workspace=tmp_path)
        envelope = agent.context.envelope
        assert envelope is not None
        assert envelope.network_profile is NetworkProfile.WEB_READ
        engine = agent.context.network_policy
        assert isinstance(engine, NetworkPolicyEngine)
        assert engine.policy.profile is NetworkProfile.WEB_READ
        proxy = agent.network_proxy
        assert isinstance(proxy, EgressProxyServer)
        assert proxy.bind_host == config.network_security.proxy_bind

    def test_offline_profile_has_no_proxy(self, tmp_path, monkeypatch) -> None:
        from eurus.agent.factory import build_agent

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = make_config(tmp_path, {})
        agent = build_agent(config, make_router(config), workspace=tmp_path)
        assert agent.context.envelope is not None
        assert agent.context.envelope.network_profile is NetworkProfile.OFFLINE
        assert isinstance(agent.context.network_policy, NetworkPolicyEngine)
        assert agent.context.network_policy.policy.profile is NetworkProfile.OFFLINE
        assert agent.network_proxy is None

    def test_legacy_network_flag_maps_to_web_read(self, tmp_path, monkeypatch) -> None:
        from eurus.agent.factory import build_agent

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = make_config(tmp_path, {"sandbox": {"network": True}})
        agent = build_agent(config, make_router(config), workspace=tmp_path)
        assert agent.context.envelope is not None
        assert agent.context.envelope.network_profile is NetworkProfile.WEB_READ

    def test_disabled_network_security_removes_policy(self, tmp_path, monkeypatch) -> None:
        from eurus.agent.factory import build_agent

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = make_config(
            tmp_path,
            {
                "sandbox": {"network": True, "network_profile": "web_read"},
                "network_security": {"enabled": False},
            },
        )
        agent = build_agent(config, make_router(config), workspace=tmp_path)
        assert agent.context.network_policy is None
        assert agent.network_proxy is None

    def test_invalid_profile_combinations_rejected(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        with pytest.raises(ConfigError, match="network_profile"):
            make_config(tmp_path, {"sandbox": {"network_profile": "galactic"}})
        with pytest.raises(ConfigError, match="host.*cannot enforce"):
            make_config(
                tmp_path,
                {"sandbox": {"backend": "host", "required": False, "network_profile": "web_read"}},
            )
        with pytest.raises(ConfigError, match="sandbox.network is false"):
            make_config(tmp_path, {"sandbox": {"network_profile": "web_read"}})

    def test_restricted_profile_requires_allowlist_in_config(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        with pytest.raises(ConfigError, match="restricted"):
            make_config(
                tmp_path,
                {"sandbox": {"network": True, "network_profile": "restricted"}},
            )
        config = make_config(
            tmp_path,
            {
                "sandbox": {"network": True, "network_profile": "restricted"},
                "network_security": {
                    "proxy_bind": "127.0.0.1",
                    "restricted_hosts": ["pypi.org"],
                },
            },
        )
        assert config.sandbox.network_profile == "restricted"
        assert config.network_security.restricted_hosts == ("pypi.org",)


class TestEngineLeaseLifecycle:
    def _agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from eurus.agent.factory import build_agent

        monkeypatch.setenv("EURUS_HOME", str(tmp_path))
        config = make_config(
            tmp_path,
            {"sandbox": {"network": True, "network_profile": "web_read"}},
        )
        return build_agent(config, make_router(config), workspace=tmp_path)

    def test_issue_and_revoke_lease(self, tmp_path, monkeypatch) -> None:
        from eurus.runtime.engine import AgentEngine

        async def scenario() -> None:
            agent = self._agent(tmp_path, monkeypatch)
            engine = AgentEngine(agent)
            url = await engine._issue_proxy_lease("run-lease-1")
            assert url.startswith("http://eurus:")
            proxy = agent.network_proxy
            assert proxy is not None
            assert proxy.lease_for("run-lease-1") is not None
            assert proxy.running
            engine._revoke_proxy_lease("run-lease-1")
            assert proxy.lease_for("run-lease-1") is None
            await proxy.stop()

        asyncio.run(scenario())

    def test_offline_profile_issues_no_lease(self, tmp_path, monkeypatch) -> None:
        from eurus.agent.factory import build_agent
        from eurus.runtime.engine import AgentEngine

        async def scenario() -> None:
            monkeypatch.setenv("EURUS_HOME", str(tmp_path))
            config = make_config(tmp_path, {})
            agent = build_agent(config, make_router(config), workspace=tmp_path)
            engine = AgentEngine(agent)
            url = await engine._issue_proxy_lease("run-offline")
            assert url == ""
            assert agent.network_proxy is None

        asyncio.run(scenario())

    def test_bind_run_context_carries_proxy_url(self, tmp_path, monkeypatch) -> None:
        from dataclasses import asdict

        from eurus.runtime.budgets import BudgetSpec
        from eurus.runtime.engine import AgentEngine
        from eurus.runtime.models import Run

        async def scenario() -> None:
            agent = self._agent(tmp_path, monkeypatch)
            engine = AgentEngine(agent)
            url = await engine._issue_proxy_lease("run-bind")
            run = Run(owner_id="local", objective="test", budget=asdict(BudgetSpec()))
            engine._bind_run_context(run, BudgetSpec(), url)
            assert agent.context.proxy_url == url
            assert agent.context.run_id == run.run_id
            engine._revoke_proxy_lease("run-bind")
            proxy = agent.network_proxy
            assert proxy is not None
            await proxy.stop()

        asyncio.run(scenario())


class TestSandboxProxyEnv:
    def _run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **request_kwargs
    ) -> tuple[list[tuple[str, ...]], Any]:
        spawned: list[tuple[str, ...]] = []

        async def fake_exec(*argv: str, **kwargs):
            argv_tuple = tuple(argv)
            if argv_tuple[1:2] == ("info",):
                return FakeProcess(argv=argv_tuple, stdout=b"{}")
            spawned.append(argv_tuple)
            return FakeProcess(argv=argv_tuple, stdout=b"out", stderr=b"err")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        from eurus.execution import sandbox as sandbox_module

        monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
        sandbox = ContainerSandbox(tmp_path, network_enabled=True)
        (tmp_path / "marker").write_text("x", encoding="utf-8")
        result = asyncio.run(
            sandbox.execute(ExecutionRequest(argv=("/bin/sh", "-lc", "true"), **request_kwargs))
        )
        return spawned, result

    def test_proxy_env_vars_added_when_configured(self, tmp_path, monkeypatch) -> None:
        spawned, result = self._run(
            tmp_path,
            monkeypatch,
            network_enabled=True,
            network_profile="web_read",
            proxy_url="http://eurus:secret-token@172.17.0.1:8123",
        )
        assert result.exit_code == 0
        text = json.dumps(list(spawned[0]))
        assert "HTTP_PROXY=http://eurus:secret-token@172.17.0.1:8123" in text
        assert "HTTPS_PROXY=http://eurus:secret-token@172.17.0.1:8123" in text
        assert "NO_PROXY=" in text
        assert spawned[0].count("--env") >= 6

    def test_no_proxy_env_without_proxy_url(self, tmp_path, monkeypatch) -> None:
        spawned, _result = self._run(
            tmp_path,
            monkeypatch,
            network_enabled=True,
            network_profile="web_read",
        )
        text = json.dumps(list(spawned[0]))
        assert "HTTP_PROXY=" not in text


class TestShellProxyPropagation:
    def test_proxy_url_flows_to_request(self, tmp_path) -> None:
        requests: list[ExecutionRequest] = []

        class RecordingSandbox:
            isolated = True

            async def execute(self, request: ExecutionRequest) -> Any:
                requests.append(request)

                class Result:
                    stdout = "ok"
                    stderr = ""
                    exit_code = 0
                    timed_out = False

                return Result()

        envelope = ExecutionEnvelope.authorize(
            workspace_root=str(tmp_path),
            backend=IsolationBackend.CONTAINER,
            network_profile=NetworkProfile.WEB_READ,
        )
        tool = shell_tool(RecordingSandbox())
        context = ToolContext(
            workspace=tmp_path,
            envelope=envelope,
            proxy_url="http://eurus:tok@172.17.0.1:9",
        )
        asyncio.run(tool.handler({"command": "true"}, context))
        assert requests[0].proxy_url == "http://eurus:tok@172.17.0.1:9"
        assert requests[0].network_enabled is True

    def test_offline_envelope_strips_proxy_url(self, tmp_path) -> None:
        requests: list[ExecutionRequest] = []

        class RecordingSandbox:
            isolated = True

            async def execute(self, request: ExecutionRequest) -> Any:
                requests.append(request)

                class Result:
                    stdout = "ok"
                    stderr = ""
                    exit_code = 0
                    timed_out = False

                return Result()

        envelope = ExecutionEnvelope.authorize(
            workspace_root=str(tmp_path), backend=IsolationBackend.CONTAINER
        )
        tool = shell_tool(RecordingSandbox())
        context = ToolContext(
            workspace=tmp_path,
            envelope=envelope,
            proxy_url="http://eurus:tok@172.17.0.1:9",
        )
        asyncio.run(tool.handler({"command": "true"}, context))
        assert requests[0].proxy_url == ""
        assert requests[0].network_enabled is False


class TestWebToolsPolicy:
    def test_offline_envelope_blocks_search_and_fetch(self, tmp_path) -> None:
        from eurus.agent import web_tools as web

        envelope = ExecutionEnvelope.authorize(
            workspace_root=str(tmp_path), backend=IsolationBackend.CONTAINER
        )
        context = ToolContext(workspace=tmp_path, envelope=envelope)
        with pytest.raises(ToolError, match="offline"):
            asyncio.run(web._search_web({"query": "x"}, context))
        with pytest.raises(ToolError, match="offline"):
            asyncio.run(web._fetch_webpage({"url": "https://example.com"}, context))

    def test_non_offline_envelope_allows_search(self, tmp_path, monkeypatch) -> None:
        from eurus.agent import web_tools as web

        envelope = ExecutionEnvelope.authorize(
            workspace_root=str(tmp_path),
            backend=IsolationBackend.CONTAINER,
            network_profile=NetworkProfile.WEB_READ,
        )
        context = ToolContext(workspace=tmp_path, envelope=envelope)

        def fake_search(query: str, limit: int) -> list[dict[str, str]]:
            return [{"title": "T", "url": "https://example.com", "snippet": "s"}]

        monkeypatch.setattr(web, "_search_sync", fake_search)
        output = asyncio.run(web._search_web({"query": "x"}, context))
        assert "example.com" in output

    def test_fetch_through_policy_against_local_origin(self, tmp_path) -> None:
        from eurus.agent import web_tools as web

        async def scenario() -> None:
            origin = Origin()
            await origin.start()
            try:
                policy = local_engine(origin.port)
                context = ToolContext(workspace=tmp_path, network_policy=policy)
                output = await web._fetch_webpage(
                    {"url": f"http://127.0.0.1:{origin.port}/"}, context
                )
                assert "Integration fetch works" in output
            finally:
                await origin.stop()

        asyncio.run(scenario())

    def test_fetch_rejects_redirect_to_metadata_endpoint(self, tmp_path) -> None:
        from eurus.agent import web_tools as web

        async def scenario() -> None:
            origin = Origin(mode="redirect")
            await origin.start()
            try:
                policy = local_engine(origin.port)
                context = ToolContext(workspace=tmp_path, network_policy=policy)
                with pytest.raises(ToolError, match="metadata"):
                    await web._fetch_webpage({"url": f"http://127.0.0.1:{origin.port}/"}, context)
            finally:
                await origin.stop()

        asyncio.run(scenario())

    def test_fetch_respects_host_allowlist(self, tmp_path) -> None:
        from eurus.agent import web_tools as web

        async def scenario() -> None:
            origin = Origin()
            await origin.start()
            try:
                policy = local_engine(origin.port)
                object.__setattr__(
                    policy.policy,
                    "host_allowlist",
                    ("example.com",),
                )
                context = ToolContext(workspace=tmp_path, network_policy=policy)
                with pytest.raises(ToolError, match="not permitted"):
                    await web._fetch_webpage({"url": f"http://127.0.0.1:{origin.port}/"}, context)
            finally:
                await origin.stop()

        asyncio.run(scenario())


class TestBrowserDownloadCap:
    def test_streaming_cap_discards_partial_file(self, tmp_path) -> None:
        from eurus.browser.manager import BrowserManager, BrowserSettings, PendingDownload

        cancelled: list[bool] = []
        destination = tmp_path / "download.bin"

        class SlowSource:
            async def save_as(self, path: str) -> None:
                with open(path, "wb") as handle:
                    for _ in range(12):
                        handle.write(b"x" * 262144)
                        handle.flush()
                        await asyncio.sleep(0.02)

            async def cancel(self) -> None:
                cancelled.append(True)

        async def scenario() -> None:
            settings = BrowserSettings(max_download_mb=1)
            manager = BrowserManager(settings, workspace=tmp_path, backend=object())
            handle = PendingDownload(
                token="t", suggested_name="d", url="http://x", source=SlowSource()
            )
            with pytest.raises(ToolError, match="partial file was discarded"):
                await manager.save_download(None, handle, destination)
            assert not destination.exists()
            assert cancelled

        asyncio.run(scenario())

    def test_small_download_saves_normally(self, tmp_path) -> None:
        from eurus.browser.manager import BrowserManager, BrowserSettings, PendingDownload

        destination = tmp_path / "small.bin"

        class FastSource:
            async def save_as(self, path: str) -> None:
                with open(path, "wb") as handle:
                    handle.write(b"data")

            async def cancel(self) -> None:
                pass

        async def scenario() -> None:
            settings = BrowserSettings(max_download_mb=1)
            manager = BrowserManager(settings, workspace=tmp_path, backend=object())
            handle = PendingDownload(
                token="t", suggested_name="s", url="http://x", source=FastSource()
            )
            result = await manager.save_download(None, handle, destination)
            assert result["bytes"] == 4
            assert destination.read_bytes() == b"data"

        asyncio.run(scenario())
