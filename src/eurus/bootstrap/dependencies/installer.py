from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import sys
from collections.abc import Callable

from eurus.bootstrap.models import InstallOutcome
from eurus.logs import redact

INSTALL_TIMEOUT_SECONDS = 900.0
OUTPUT_TAIL_LINES = 12


async def run_pip(
    arguments: list[str],
    *,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
    on_line: Callable[[str], object] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[int, str]:
    executable = sys.executable or "python3"
    command = [executable, "-m", "pip", *arguments]
    env = dict(os.environ) if environ is None else dict(environ)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_NO_INPUT", "1")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        return 127, f"cannot start pip: {exc}"
    collected: list[str] = []

    async def _read() -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            collected.append(text)
            if on_line is not None:
                try:
                    result = on_line(text)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

    reader = asyncio.create_task(_read())
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        reader.cancel()
        tail = "\n".join(collected[-OUTPUT_TAIL_LINES:])
        return 124, f"pip timed out after {timeout:g}s\n{tail}"
    with contextlib.suppress(Exception):
        await reader
    return returncode, "\n".join(collected)


def sanitize_output(text: str) -> str:
    return redact(text)


def install_targets(
    targets: list[str],
    *,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
    on_line: Callable[[str], object] | None = None,
    environ: dict[str, str] | None = None,
    dry_run_only: bool = False,
) -> InstallOutcome:
    if not targets:
        return InstallOutcome(success=True, targets=(), returncode=0, output_tail="")
    arguments = [
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--prefer-binary",
        *targets,
    ]
    if dry_run_only:
        arguments.insert(2, "--dry-run")
    returncode, output = asyncio.run(
        run_pip(arguments, timeout=timeout, on_line=on_line, environ=environ)
    )
    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
    sanitized = sanitize_output(tail)
    if returncode != 0:
        return InstallOutcome(
            success=False,
            targets=tuple(targets),
            returncode=returncode,
            output_tail=sanitized,
            error=sanitize_output(output[-2000:]),
        )
    return InstallOutcome(success=True, targets=tuple(targets), returncode=0, output_tail=sanitized)


def upgrade_targets(
    targets: list[str],
    *,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
) -> InstallOutcome:
    if not targets:
        return InstallOutcome(success=True, targets=(), returncode=0, output_tail="")
    arguments = [
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--prefer-binary",
        "--upgrade",
        *targets,
    ]
    returncode, output = asyncio.run(
        run_pip(
            arguments,
            timeout=timeout,
            environ=environ,
        )
    )
    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
    return InstallOutcome(
        success=returncode == 0,
        targets=tuple(targets),
        returncode=returncode,
        output_tail=sanitize_output(tail),
        error=sanitize_output(output[-2000:]) if returncode != 0 else "",
    )


def pip_command_preview(arguments: list[str]) -> str:
    executable = sys.executable or "python3"
    return shlex.join([executable, "-m", "pip", *arguments])
