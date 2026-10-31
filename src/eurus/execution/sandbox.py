from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from eurus.errors import ToolError
from eurus.execution.bounded import spawn_readers
from eurus.execution.docker import (
    MANAGED_LABEL,
    DockerSecurityError,
    RuntimeInfo,
    probe_runtime,
    sanitize_label_value,
    sweep_orphans,
    validate_image_reference,
)
from eurus.termux import is_termux

USER_PATTERN = re.compile(r"^(auto|[a-z_][a-z0-9_-]{0,31}|\d{1,5}(:\d{1,5})?)$")

SecurityEventHook = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    argv: tuple[str, ...]
    cwd: str = "/workspace"
    timeout_seconds: float = 120.0
    max_output_bytes: int = 20000
    network_enabled: bool = False
    network_profile: str = "offline"
    run_id: str = ""
    workspace_id: str = ""
    attempt: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


class SandboxUnavailableError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="sandbox_unavailable")


class Sandbox(Protocol):
    isolated: bool

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


def runtime_hint() -> str:
    if is_termux():
        return (
            'Docker cannot run on Termux. Set [sandbox] backend = "host" and '
            "required = false in your config (commands then run on this device, "
            "not isolated, and still need your approval)."
        )
    return (
        'Install the runtime, or set [sandbox] backend = "host" and required = false '
        "to run commands on this machine without isolation."
    )


def resolve_container_user(setting: str) -> str:
    if not USER_PATTERN.match(setting):
        raise ToolError("sandbox.user must be 'auto', a name, a numeric uid, or uid:gid")
    if setting != "auto":
        return setting
    if os.name == "posix":
        uid, gid = os.getuid(), os.getgid()
        if uid != 0:
            return f"{uid}:{gid}"
    return "1000:1000"


def _numeric_uid(user: str) -> str | None:
    head = user.split(":", 1)[0]
    return head if head.isdigit() else None


class ContainerSandbox:
    isolated: bool = True

    def __init__(
        self,
        workspace: Path,
        *,
        runtime: str = "docker",
        image: str = "python:3.12-alpine",
        memory_mb: int = 1024,
        cpu_limit: float = 1.0,
        pids_limit: int = 128,
        network_enabled: bool = False,
        user: str = "auto",
        image_allowlist: tuple[str, ...] | list[str] = (),
        digest_pin_required: bool = False,
        seccomp_profile: str = "",
        apparmor_profile: str = "",
        security_event: SecurityEventHook | None = None,
        health_ttl_seconds: float = 30.0,
    ) -> None:
        self.workspace = workspace.resolve()
        self.runtime = runtime
        self.image = image
        self.memory_mb = memory_mb
        self.cpu_limit = cpu_limit
        self.pids_limit = pids_limit
        self.network_enabled = network_enabled
        self.user_setting = user
        self.image_allowlist = tuple(image_allowlist)
        self.digest_pin_required = digest_pin_required
        self.seccomp_profile = seccomp_profile
        self.apparmor_profile = apparmor_profile
        self.security_event = security_event
        self.health_ttl_seconds = max(0.0, health_ttl_seconds)
        self._health_until = 0.0
        self._health_info: RuntimeInfo | None = None

    def _emit(self, event: str, **fields: Any) -> None:
        if self.security_event is None:
            return
        with suppress(Exception):
            self.security_event(event, fields)

    async def _ensure_runtime_healthy(self) -> RuntimeInfo:
        now = time.monotonic()
        if self._health_info is not None and now < self._health_until:
            return self._health_info
        try:
            info = await probe_runtime(self.runtime)
        except DockerSecurityError as exc:
            self._health_info = None
            self._health_until = 0.0
            self._emit("sandbox.unavailable", runtime=self.runtime, reason=str(exc))
            raise SandboxUnavailableError(
                f"sandbox runtime '{self.runtime}' is unavailable; host fallback is disabled. "
                f"{exc} {runtime_hint()}"
            ) from exc
        self._health_info = info
        self._health_until = now + self.health_ttl_seconds
        return info

    def _validated_image(self) -> str:
        try:
            return validate_image_reference(
                self.image,
                allowlist=self.image_allowlist,
                digest_required=self.digest_pin_required,
            )
        except DockerSecurityError as exc:
            self._emit("sandbox.image_rejected", image=self.image, reason=str(exc))
            raise ToolError(str(exc), code=exc.code or "") from exc

    def _labels(self, request: ExecutionRequest) -> list[tuple[str, str]]:
        labels = [(MANAGED_LABEL, "true")]
        if request.run_id:
            labels.append(("eurus.run_id", sanitize_label_value(request.run_id)))
        if request.workspace_id:
            labels.append(("eurus.workspace_id", sanitize_label_value(request.workspace_id)))
        if request.attempt:
            labels.append(("eurus.attempt", sanitize_label_value(request.attempt)))
        return labels

    async def reconcile_orphans(self, *, run_id: str | None = None) -> list[str]:
        removed = await sweep_orphans(self.runtime, run_id=run_id)
        if removed:
            self._emit("sandbox.orphans_reconciled", count=len(removed), runtime=self.runtime)
        return removed

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.argv or any(not isinstance(part, str) for part in request.argv):
            raise ToolError("sandbox command must be a non-empty argument list")
        if not self.workspace.is_dir():
            raise ToolError("sandbox workspace does not exist")
        runtime_path = shutil.which(self.runtime)
        if runtime_path is None:
            self._emit("sandbox.unavailable", runtime=self.runtime, reason="binary missing")
            raise SandboxUnavailableError(
                f"sandbox runtime '{self.runtime}' is unavailable; host fallback is disabled. "
                + runtime_hint()
            )
        image = self._validated_image()
        user = resolve_container_user(self.user_setting)
        await self._ensure_runtime_healthy()
        relative_cwd = request.cwd.removeprefix("/workspace").lstrip("/")
        cwd = (self.workspace / relative_cwd).resolve()
        if cwd != self.workspace and self.workspace not in cwd.parents:
            raise ToolError("working directory is outside the workspace")
        if not cwd.is_dir():
            raise ToolError("working directory does not exist")

        with tempfile.TemporaryDirectory(prefix="eurus-sandbox-") as temp_dir:
            cidfile = Path(temp_dir) / "container.id"
            network_on = request.network_enabled and request.network_profile != "offline"
            network = "bridge" if (self.network_enabled and network_on) else "none"
            command = [
                runtime_path,
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(self.pids_limit),
                "--memory",
                f"{self.memory_mb}m",
                "--cpus",
                str(self.cpu_limit),
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--mount",
                f"type=bind,src={self.workspace},dst=/workspace,rw",
                "--workdir",
                "/workspace",
                "--user",
                user,
            ]
            uid = _numeric_uid(user)
            if uid is not None and uid != "0":
                command.extend(["--env", "HOME=/tmp"])
            for key, value in self._labels(request):
                command.extend(["--label", f"{key}={value}"])
            if self.seccomp_profile:
                command.extend(["--security-opt", f"seccomp={self.seccomp_profile}"])
            if self.apparmor_profile:
                command.extend(["--security-opt", f"apparmor={self.apparmor_profile}"])
            command.extend([image, *request.argv])
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
            stdout_task, stderr_task = await spawn_readers(process, request.max_output_bytes)
            timed_out = False
            try:
                try:
                    async with asyncio.timeout(min(request.timeout_seconds, 3600.0)):
                        await process.wait()
                except TimeoutError:
                    timed_out = True
                    await self._terminate_on_timeout(cidfile)
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            except asyncio.CancelledError:
                await self._terminate_on_timeout(cidfile)
                if process.returncode is None:
                    process.kill()
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            return ExecutionResult(
                stdout.text,
                stderr.text,
                124 if timed_out else int(process.returncode or 0),
                timed_out=timed_out,
            )

    async def _terminate_on_timeout(self, cidfile: Path) -> None:
        runtime_path = shutil.which(self.runtime)
        if runtime_path is None:
            return
        container_id = ""
        with suppress(OSError, TimeoutError):
            container_id = cidfile.read_text(encoding="utf-8").strip()
        if not container_id:
            return
        killed = await self._docker_kill(runtime_path, container_id)
        removed = await self._docker_remove(runtime_path, container_id)
        if not killed:
            self._emit("sandbox.timeout_kill_failed", container_id=container_id)
        if removed:
            self._emit("sandbox.orphan_removed", container_id=container_id)

    async def _docker_kill(self, runtime_path: str, container_id: str) -> bool:
        with suppress(OSError, TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                runtime_path,
                "kill",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
            return killer.returncode == 0
        return False

    async def _docker_remove(self, runtime_path: str, container_id: str) -> bool:
        with suppress(OSError, TimeoutError):
            remover = await asyncio.create_subprocess_exec(
                runtime_path,
                "rm",
                "-f",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(remover.wait(), timeout=5)
            return remover.returncode == 0
        return False
