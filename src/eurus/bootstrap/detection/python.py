from __future__ import annotations

import importlib.util
import os
import platform
import re
import subprocess
import sys

from eurus.bootstrap.models import PythonInfo


def _pip_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = (result.stdout or "").strip()
    if result.returncode == 0 and output:
        match = re.match(r"pip (\S+)", output)
        return match.group(1) if match else output
    return ""


def detect_venv(environ: dict[str, str] | None = None) -> tuple[bool, str]:
    env = dict(os.environ) if environ is None else dict(environ)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        return True, sys.prefix
    virtual = env.get("VIRTUAL_ENV", "").strip()
    if virtual:
        return True, virtual
    conda = env.get("CONDA_PREFIX", "").strip()
    if conda:
        return True, conda
    return False, ""


def inspect_python(environ: dict[str, str] | None = None) -> PythonInfo:
    env = dict(os.environ) if environ is None else dict(environ)
    version = platform.python_version()
    executable = sys.executable or "python3"
    is_venv, venv_path = detect_venv(env)
    pip_available = importlib.util.find_spec("pip") is not None
    pip_version = _pip_version(executable) if pip_available else ""
    return PythonInfo(
        version=version,
        major=sys.version_info[0],
        minor=sys.version_info[1],
        executable=executable,
        is_venv=is_venv,
        venv_path=venv_path,
        pip_available=pip_available,
        pip_version=pip_version,
    )
