from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

from eurus.bootstrap.detection.architecture import is_wheel_platform
from eurus.bootstrap.models import (
    ArchitectureInfo,
    CapabilityReport,
    PackageManagerInfo,
    PlatformInfo,
    PythonInfo,
)

BROWSER_PROBE_HOSTS = (("connectivity.eurus.invalid", 443),)
DNS_PROBE_HOST = "1.1.1.1"
DNS_PROBE_PORT = 53
CONNECT_PROBE_HOST = "1.1.1.1"
CONNECT_PROBE_PORT = 443
PROBE_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    platform: PlatformInfo
    architecture: ArchitectureInfo
    python: PythonInfo
    package_manager: PackageManagerInfo
    capabilities: CapabilityReport

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.to_dict(),
            "architecture": self.architecture.to_dict(),
            "python": self.python.to_dict(),
            "package_manager": self.package_manager.to_dict(),
            "capabilities": self.capabilities.to_dict(),
        }


def probe_dns(host: str = DNS_PROBE_HOST, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        return socket.getaddrinfo(host, None) is not None
    except OSError:
        return False


def probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def network_available(environ: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if environ is None else dict(environ)
    if env.get("EURUS_LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "local-only"
    if probe_tcp(CONNECT_PROBE_HOST, CONNECT_PROBE_PORT):
        return "yes"
    if probe_dns():
        return "limited"
    return "no"


def compiler_available() -> str:
    for name in ("cc", "gcc", "clang"):
        if shutil.which(name):
            return "yes"
    return "no"


def wheel_support(arch: ArchitectureInfo) -> str:
    return is_wheel_platform(arch)


def chromium_snapshot(environ: dict[str, str] | None = None) -> tuple[str, str]:
    from eurus.browser.backend import find_chromium

    env = dict(os.environ) if environ is None else dict(environ)
    configured = env.get("EURUS_CHROMIUM", "").strip()
    try:
        found = find_chromium(configured)
    except Exception:
        return "", "missing"
    cache_marker = str(Path.home() / ".cache" / "ms-playwright")
    if found.startswith(cache_marker):
        return found, "playwright-managed"
    if "/chrome-linux" in found or "ms-playwright" in found:
        return found, "playwright-managed"
    return found, "system"


def cdp_endpoint_configured(environ: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if environ is None else dict(environ)
    return env.get("EURUS_CDP_ENDPOINT", "").strip()


def browser_capability(environ: dict[str, str] | None = None) -> tuple[str, str, str]:
    env = dict(os.environ) if environ is None else dict(environ)
    path, kind = chromium_snapshot(env)
    playwright_pkg = "yes" if shutil.which("playwright") or _module_exists("playwright") else "no"
    cdp_pkg = "yes" if _module_exists("websockets") else "no"
    if kind == "playwright-managed" and playwright_pkg == "yes":
        backend = "playwright"
    elif kind == "system" and (cdp_pkg == "yes" or playwright_pkg == "yes"):
        backend = "cdp+playwright" if playwright_pkg == "yes" else "cdp"
    elif kind == "system":
        backend = "cdp (websockets missing)"
    else:
        backend = "none"
    return path, kind, backend


def _module_exists(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def inspect_capabilities(environ: dict[str, str] | None = None) -> CapabilityReport:
    env = dict(os.environ) if environ is None else dict(environ)
    arch = None
    from eurus.bootstrap.detection.architecture import inspect_architecture

    arch = inspect_architecture(env)
    path, kind, backend = browser_capability(env)
    details = {
        "playwright_cli": "yes" if shutil.which("playwright") else "no",
        "websockets": "yes" if _module_exists("websockets") else "no",
        "proot_env": "yes"
        if any(
            env.get(name, "").strip()
            for name in ("PROOT_DISTRO_NAME", "PROOT_TMP_DIR", "PROOT_LOADER")
        )
        else "no",
    }
    return CapabilityReport(
        network=network_available(env),
        compiler=compiler_available(),
        browser_backend=backend,
        chromium=path,
        chromium_kind=kind,
        wheel_support=wheel_support(arch),
        details=details,
    )


def inspect_environment(environ: dict[str, str] | None = None) -> EnvironmentReport:
    env = dict(os.environ) if environ is None else dict(environ)
    from eurus.bootstrap.detection.architecture import inspect_architecture
    from eurus.bootstrap.detection.package_manager import detect_package_manager
    from eurus.bootstrap.detection.platform import inspect_platform
    from eurus.bootstrap.detection.python import inspect_python

    platform_info = inspect_platform(env)
    arch = inspect_architecture(env)
    python_info = inspect_python(env)
    manager = detect_package_manager(env)
    capabilities = inspect_capabilities(env)
    return EnvironmentReport(
        platform=platform_info,
        architecture=arch,
        python=python_info,
        package_manager=manager,
        capabilities=capabilities,
    )
