from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

import eurus.execution.sandbox as sandbox_module
from eurus.config import load_config
from eurus.errors import ConfigError, ToolError
from eurus.execution import docker as docker_module
from eurus.execution.docker import (
    DockerSecurityError,
    RuntimeInfo,
    _parse_ps_output,
    default_seccomp_profile_path,
    probe_runtime,
    resolve_seccomp_profile,
    sanitize_label_value,
    sweep_orphans,
    validate_image_reference,
)
from eurus.execution.envelope import IsolationBackend, NetworkProfile
from eurus.execution.sandbox import (
    ContainerSandbox,
    ExecutionRequest,
    SandboxUnavailableError,
    resolve_container_user,
)
from eurus.tools.base import ToolContext


class FakeStream:
    async def read(self, size: int) -> bytes:
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
        pass


def test_validate_image_reference_accepts_valid_forms():
    assert validate_image_reference("python:3.12-alpine") == "python:3.12-alpine"
    assert validate_image_reference("library/python") == "library/python"
    digest = "sha256:" + "a" * 64
    reference = f"python@{digest}"
    assert validate_image_reference(reference) == reference


@pytest.mark.parametrize(
    "reference",
    ["", "-rm", "python:tag extra", "Python:3", "img$$", "python@" + "z" * 63],
)
def test_validate_image_reference_rejects_unsafe_forms(reference):
    with pytest.raises(DockerSecurityError):
        validate_image_reference(reference)


def test_digest_pin_requirement():
    digest = "sha256:" + "b" * 64
    with pytest.raises(DockerSecurityError, match="digest"):
        validate_image_reference("python:3.12-alpine", digest_required=True)
    pinned = f"python:3.12-alpine@{digest}"
    assert validate_image_reference(pinned, digest_required=True) == pinned


def test_image_allowlist_enforcement():
    allowlist = ["python:3.12-alpine", "ghcr.io/example/tool@sha256:" + "c" * 64]
    assert (
        validate_image_reference("python:3.12-alpine", allowlist=allowlist) == "python:3.12-alpine"
    )
    with pytest.raises(DockerSecurityError, match="allowlist"):
        validate_image_reference("python:3.13-alpine", allowlist=allowlist)
    digest = "sha256:" + "c" * 64
    assert (
        validate_image_reference(f"ghcr.io/example/tool@{digest}", allowlist=allowlist)
        == f"ghcr.io/example/tool@{digest}"
    )
    with pytest.raises(DockerSecurityError, match="allowlist"):
        validate_image_reference(f"ghcr.io/example/tool@sha256:{'d' * 64}", allowlist=allowlist)


def test_image_allowlist_invalid_entry_rejected():
    with pytest.raises(DockerSecurityError, match="allowlist entry"):
        validate_image_reference("python", allowlist=["bad image"])


def test_sanitize_label_value():
    assert sanitize_label_value("run/1 a") == "run-1-a"
    assert sanitize_label_value("") == "unknown"
    assert len(sanitize_label_value("x" * 200)) == 63


def test_parse_ps_output():
    output = "abc123\teurus.managed=true,eurus.run_id=run-1\ndef456\teurus.managed=true\n"
    containers = _parse_ps_output(output)
    assert [item.container_id for item in containers] == ["abc123", "def456"]
    assert containers[0].labels["eurus.run_id"] == "run-1"
    assert containers[1].labels == {"eurus.managed": "true"}


def test_probe_runtime_parses_server_info(monkeypatch):
    payload = json.dumps(
        {"ServerVersion": "27.0.3", "SecurityOptions": ["name=seccomp,profile=default", "rootless"]}
    ).encode()

    async def fake_exec(*argv: str, **kwargs):
        return FakeProcess(argv=argv, stdout=payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(docker_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    info = asyncio.run(probe_runtime("docker"))
    assert info.server_version == "27.0.3"
    assert info.rootless is True
    assert info.security_options[0].startswith("name=seccomp")


def test_probe_runtime_reports_unhealthy_daemon(monkeypatch):
    async def fake_exec(*argv: str, **kwargs):
        return FakeProcess(argv=argv, returncode=1, stderr=b"Cannot connect to the Docker daemon")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(docker_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(DockerSecurityError, match="unhealthy"):
        asyncio.run(probe_runtime("docker"))


def test_probe_runtime_requires_binary(monkeypatch):
    monkeypatch.setattr(docker_module.shutil, "which", lambda name: None)
    with pytest.raises(DockerSecurityError, match="unavailable"):
        asyncio.run(probe_runtime("docker"))


def test_sweep_orphans_filters_by_run_and_removes(monkeypatch):
    listing = (
        b"aaa111\teurus.managed=true,eurus.run_id=run-1\n"
        b"bbb222\teurus.managed=true,eurus.run_id=run-2\n"
    )
    removed: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs):
        if "ps" in argv:
            return FakeProcess(argv=argv, stdout=listing)
        removed.append(argv)
        return FakeProcess(argv=argv, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(docker_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    removed_ids = asyncio.run(sweep_orphans("docker", run_id="run-1"))
    assert removed_ids == ["aaa111"]
    assert removed and removed[0][1] == "rm"


def test_sweep_orphans_returns_empty_when_binary_missing(monkeypatch):
    monkeypatch.setattr(docker_module.shutil, "which", lambda name: None)
    assert asyncio.run(sweep_orphans("docker")) == []


def test_resolve_container_user(monkeypatch):
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(sandbox_module.os, "getgid", lambda: 1001, raising=False)
    assert resolve_container_user("auto") == "1000:1001"
    assert resolve_container_user("1000:1000") == "1000:1000"
    assert resolve_container_user("nobody") == "nobody"
    with pytest.raises(ToolError, match="sandbox.user"):
        resolve_container_user("rm -rf /")


def test_resolve_container_user_auto_as_root(monkeypatch):
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(sandbox_module.os, "getgid", lambda: 0, raising=False)
    assert resolve_container_user("auto") == "1000:1000"


def test_sandbox_hardened_arguments(tmp_path, monkeypatch):
    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs):
        argv_tuple = tuple(argv)
        if argv_tuple[1:2] == ("info",):
            payload = json.dumps({"ServerVersion": "27.0.3", "SecurityOptions": []}).encode()
            return FakeProcess(argv=argv_tuple, stdout=payload)
        spawned.append(argv_tuple)
        return FakeProcess(argv=argv_tuple, stdout=b"out", stderr=b"err")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    events: list[tuple[str, dict]] = []
    seccomp = str(tmp_path / "seccomp.json")
    Path(seccomp).write_text("{}", encoding="utf-8")
    sandbox = ContainerSandbox(
        tmp_path,
        image="python:3.12-alpine",
        image_allowlist=("python:3.12-alpine",),
        seccomp_profile=seccomp,
        apparmor_profile="eurus-sandbox",
        user="1000:1000",
        security_event=lambda event, fields: events.append((event, fields)),
    )
    (tmp_path / "marker").write_text("x", encoding="utf-8")
    result = asyncio.run(
        sandbox.execute(
            ExecutionRequest(
                argv=("/bin/sh", "-lc", "echo hi"),
                run_id="run 1",
                workspace_id=str(tmp_path),
                attempt="attempt-1",
            )
        )
    )
    assert result.exit_code == 0
    run_argv = spawned[0]
    assert run_argv[1] == "run"
    text = json.dumps(list(run_argv))
    for expected in [
        "--read-only",
        "--cap-drop",
        "no-new-privileges",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--env",
        "HOME=/tmp",
        "--label",
        f"seccomp={seccomp}",
        "apparmor=eurus-sandbox",
        "eurus.managed=true",
        "eurus.run_id=run-1",
    ]:
        assert expected in text, f"missing {expected} in docker run argv"
    assert run_argv.count("--security-opt") == 3


def test_sandbox_network_requires_non_offline_profile(tmp_path, monkeypatch):
    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs):
        argv_tuple = tuple(argv)
        if argv_tuple[1:2] == ("info",):
            return FakeProcess(argv=argv_tuple, stdout=b"{}")
        spawned.append(argv_tuple)
        return FakeProcess(argv=argv_tuple)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    sandbox = ContainerSandbox(tmp_path, network_enabled=True)
    asyncio.run(
        sandbox.execute(ExecutionRequest(argv=("/bin/sh", "-lc", "true"), network_enabled=True))
    )
    offline_argv = json.dumps(list(spawned[-1]))
    assert '"none"' in offline_argv
    asyncio.run(
        sandbox.execute(
            ExecutionRequest(
                argv=("/bin/sh", "-lc", "true"),
                network_enabled=True,
                network_profile=NetworkProfile.WEB_READ.value,
            )
        )
    )
    bridge_argv = json.dumps(list(spawned[-1]))
    assert '"bridge"' in bridge_argv


def test_sandbox_fail_closed_when_daemon_unhealthy(tmp_path, monkeypatch):
    async def fake_exec(*argv: str, **kwargs):
        return FakeProcess(argv=tuple(argv), returncode=1, stderr=b"daemon down")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    events: list[tuple[str, dict]] = []
    sandbox = ContainerSandbox(
        tmp_path, security_event=lambda event, fields: events.append((event, fields))
    )
    (tmp_path / "marker").write_text("x", encoding="utf-8")
    with pytest.raises(SandboxUnavailableError, match="host fallback is disabled"):
        asyncio.run(sandbox.execute(ExecutionRequest(argv=("/bin/sh", "-lc", "true"))))
    assert events and events[0][0] == "sandbox.unavailable"


def test_sandbox_fail_closed_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    sandbox = ContainerSandbox(tmp_path)
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        asyncio.run(sandbox.execute(ExecutionRequest(argv=("/bin/sh", "-lc", "true"))))


def test_sandbox_health_check_cached(tmp_path, monkeypatch):
    probes = 0

    async def fake_probe(runtime: str) -> RuntimeInfo:
        nonlocal probes
        probes += 1
        return RuntimeInfo(runtime=runtime, server_version="1", rootless=False, security_options=())

    async def fake_exec(*argv: str, **kwargs):
        return FakeProcess(argv=tuple(argv))

    monkeypatch.setattr(sandbox_module, "probe_runtime", fake_probe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    sandbox = ContainerSandbox(tmp_path, health_ttl_seconds=60.0)
    request = ExecutionRequest(argv=("/bin/sh", "-lc", "true"))
    asyncio.run(sandbox.execute(request))
    asyncio.run(sandbox.execute(request))
    assert probes == 1


def test_sandbox_rejects_image_outside_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    events: list[tuple[str, dict]] = []
    sandbox = ContainerSandbox(
        tmp_path,
        image="python:3.13-alpine",
        image_allowlist=("python:3.12-alpine",),
        security_event=lambda event, fields: events.append((event, fields)),
    )
    with pytest.raises(ToolError, match="allowlist"):
        asyncio.run(sandbox.execute(ExecutionRequest(argv=("/bin/sh", "-lc", "true"))))
    assert events and events[0][0] == "sandbox.image_rejected"


def test_sandbox_reconcile_orphans_emits_event(tmp_path, monkeypatch):
    async def fake_sweep(runtime: str, *, run_id: str | None = None):
        assert runtime == "docker"
        return ["abc123"]

    monkeypatch.setattr(sandbox_module, "sweep_orphans", fake_sweep)
    events: list[tuple[str, dict]] = []
    sandbox = ContainerSandbox(
        tmp_path, security_event=lambda event, fields: events.append((event, fields))
    )
    removed = asyncio.run(sandbox.reconcile_orphans())
    assert removed == ["abc123"]
    assert events and events[0][0] == "sandbox.orphans_reconciled"


def test_terminate_on_timeout_removes_orphan(tmp_path, monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **kwargs):
        calls.append(tuple(argv))
        returncode = 0 if argv[1] == "rm" else 1
        return FakeProcess(argv=tuple(argv), returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    events: list[tuple[str, dict]] = []
    sandbox = ContainerSandbox(
        tmp_path, security_event=lambda event, fields: events.append((event, fields))
    )
    cidfile = tmp_path / "container.id"
    cidfile.write_text("deadbeef", encoding="utf-8")
    asyncio.run(sandbox._terminate_on_timeout(cidfile))
    kinds = [event for event, _fields in events]
    assert "sandbox.orphan_removed" in kinds
    assert "sandbox.timeout_kill_failed" in kinds


def test_seccomp_profile_materialized_into_system_dir(tmp_path):
    system_dir = tmp_path / "system"
    resolved = resolve_seccomp_profile("default", system_dir=system_dir)
    materialized = Path(resolved)
    assert materialized.is_file()
    assert materialized.parent == system_dir / "docker"
    assert materialized.read_bytes() == default_seccomp_profile_path().read_bytes()
    assert (system_dir.stat().st_mode & 0o777) == 0o700
    assert (materialized.stat().st_mode & 0o777) == 0o600
    again = resolve_seccomp_profile("default", system_dir=system_dir)
    assert again == resolved


def test_seccomp_profile_custom_path_must_exist(tmp_path):
    with pytest.raises(DockerSecurityError, match="does not exist"):
        resolve_seccomp_profile(str(tmp_path / "missing.json"), system_dir=tmp_path)
    assert resolve_seccomp_profile("", system_dir=tmp_path) == ""
    profile = tmp_path / "custom.json"
    profile.write_text("{}", encoding="utf-8")
    assert resolve_seccomp_profile(str(profile), system_dir=tmp_path) == str(profile)


def test_config_sandbox_new_keys_defaults_and_validation(tmp_path):
    config = load_config(cwd=tmp_path, environ={"EURUS_HOME": str(tmp_path)})
    assert config.sandbox.user == "auto"
    assert config.sandbox.image_allowlist == ()
    assert config.sandbox.digest_pin_required is False
    assert config.sandbox.seccomp_profile == ""
    assert config.sandbox.apparmor_profile == ""
    path = tmp_path / "eurus.toml"
    path.write_text('[sandbox]\nuser = "nobody; id"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="sandbox.user"):
        load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path)})
    path.write_text('[sandbox]\nimage_allowlist = ["bad image"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="image_allowlist"):
        load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path)})
    path.write_text('[sandbox]\napparmor_profile = "bad profile!"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="apparmor_profile"):
        load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path)})
    path.write_text("[sandbox]\ndigest_pin_required = true\n", encoding="utf-8")
    config = load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path)})
    assert config.sandbox.digest_pin_required is True


def test_engine_reconciles_orphans_once_per_run(monkeypatch):
    from eurus.runtime.engine import AgentEngine

    calls: list[str] = []

    class StubSandbox:
        async def reconcile_orphans(self, *, run_id: str | None = None):
            calls.append(run_id or "")
            return []

    class StubAgent:
        sandbox = StubSandbox()

    engine = object.__new__(AgentEngine)
    engine.agent = StubAgent()
    asyncio.run(engine._reconcile_sandbox_orphans())
    assert len(calls) == 1

    class PlainAgent:
        pass

    engine.agent = PlainAgent()
    asyncio.run(engine._reconcile_sandbox_orphans())
    assert len(calls) == 1


def test_shell_tool_declares_capability_and_propagates_identity(tmp_path):
    from eurus.execution.envelope import ExecutionEnvelope
    from eurus.tools.shell import shell_tool

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
    assert tool.required_capabilities == ("shell.execute",)
    context = ToolContext(workspace=tmp_path, run_id="run-9", envelope=envelope)
    asyncio.run(tool.handler({"command": "true"}, context))
    request = requests[0]
    assert request.run_id == "run-9"
    assert request.workspace_id == str(tmp_path)
    assert request.network_profile == "offline"
    assert request.network_enabled is False


def test_factory_wires_envelope_journal_and_sandbox(tmp_path, monkeypatch, caplog):
    from eurus.agent.factory import build_agent
    from eurus.execution.sandbox import ContainerSandbox
    from eurus.providers.factory import build_router
    from eurus.providers.testing import FakeTransport, openai_stream

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={"agent": {"model": "groq/llama-3.3-70b-versatile"}},
    )
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = build_router(config, transport=transport, environ={"GROQ_API_KEY": "k"})
    with caplog.at_level(logging.WARNING):
        agent = build_agent(config, router, workspace=tmp_path)
    envelope = agent.context.envelope
    assert envelope is not None
    assert envelope.backend is IsolationBackend.CONTAINER
    assert envelope.network_profile is NetworkProfile.OFFLINE
    assert "shell.execute" in envelope.capabilities
    sandbox = getattr(agent, "sandbox", None)
    assert isinstance(sandbox, ContainerSandbox)
    journal = tmp_path / "security" / "execution.jsonl"
    assert journal.is_file()
    assert (journal.stat().st_mode & 0o777) == 0o600


def test_factory_host_mode_warns_and_audits(tmp_path, monkeypatch, caplog):
    from eurus.agent.factory import build_agent
    from eurus.execution.envelope import IsolationBackend
    from eurus.execution.host import HostSandbox
    from eurus.providers.factory import build_router
    from eurus.providers.testing import FakeTransport, openai_stream

    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    config = load_config(
        cwd=tmp_path,
        environ={"EURUS_HOME": str(tmp_path)},
        overrides={
            "agent": {"model": "groq/llama-3.3-70b-versatile"},
            "sandbox": {"backend": "host", "required": False},
        },
    )
    transport = FakeTransport(streams=[openai_stream("ok")])
    router = build_router(config, transport=transport, environ={"GROQ_API_KEY": "k"})
    with caplog.at_level(logging.WARNING):
        agent = build_agent(config, router, workspace=tmp_path)
    assert isinstance(agent.sandbox, HostSandbox)
    assert agent.context.envelope.backend is IsolationBackend.HOST
    assert any("without isolation" in record.message for record in caplog.records)
    journal = tmp_path / "security" / "execution.jsonl"
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("event") == "execution.host_mode" for item in entries)
