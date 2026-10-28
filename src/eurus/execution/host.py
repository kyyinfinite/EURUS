from __future__ import annotations

import asyncio
import os
import shutil
import signal
from contextlib import suppress
from pathlib import Path

from eurus.errors import ToolError
from eurus.execution.bounded import spawn_readers
from eurus.execution.sandbox import ExecutionRequest, ExecutionResult
from eurus.termux import TERMUX_ENV, is_termux

SAFE_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "PREFIX",
    "VIRTUAL_ENV",
    "PYTHONPATH",
)


def _termux_preload(value: str) -> bool:
    prefix = os.environ.get("PREFIX", "")
    return bool(prefix) and all(part.startswith(prefix) for part in value.split(":") if part)


def scrubbed_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in SAFE_ENV if name in os.environ}
    if is_termux():
        for name in TERMUX_ENV:
            if name in os.environ:
                env[name] = os.environ[name]
        preload = os.environ.get("LD_PRELOAD", "")
        if preload and _termux_preload(preload):
            env["LD_PRELOAD"] = preload
    return env


def _executable(argv0: str) -> str:
    if argv0 == "/bin/sh" and not Path(argv0).exists():
        return shutil.which("sh") or argv0
    return argv0


class HostSandbox:
    isolated: bool = False

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.argv or any(not isinstance(part, str) for part in request.argv):
            raise ToolError("command must be a non-empty argument list")
        if not self.workspace.is_dir():
            raise ToolError("workspace does not exist")
        relative = request.cwd.removeprefix("/workspace").lstrip("/")
        cwd = (self.workspace / relative).resolve()
        if cwd != self.workspace and self.workspace not in cwd.parents:
            raise ToolError("working directory is outside the workspace")
        if not cwd.is_dir():
            raise ToolError("working directory does not exist")

        argv = (_executable(request.argv[0]), *request.argv[1:])
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=scrubbed_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ToolError(f"cannot start command: {exc}") from exc

        limit = request.max_output_bytes
        stdout_task, stderr_task = await spawn_readers(process, limit)
        timed_out = False
        try:
            try:
                async with asyncio.timeout(min(request.timeout_seconds, 3600.0)):
                    await process.wait()
            except TimeoutError:
                timed_out = True
                self._kill(process)
                await process.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            self._kill(process)
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

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError, PermissionError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
