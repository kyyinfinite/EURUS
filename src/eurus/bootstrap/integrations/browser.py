from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eurus.bootstrap.models import BROKEN, MISSING, READY, UNKNOWN, CheckResult
from eurus.browser.backend import chromium_candidates, find_chromium
from eurus.config import Config

if TYPE_CHECKING:
    from eurus.browser.manager import BrowserSettings

PLAYWRIGHT_CACHE = Path.home() / ".cache" / "ms-playwright"
NAVIGATION_TEST_URL = "https://example.com"
CDP_PROBE_TIMEOUT = 4.0
BACKEND_START_TIMEOUT = 30.0

CHROMIUM_KIND_SYSTEM = "system"
CHROMIUM_KIND_PLAYWRIGHT = "playwright-managed"
CHROMIUM_KIND_REMOTE = "remote-cdp"
CHROMIUM_KIND_MISSING = "missing"


@dataclass(slots=True)
class BrowserSetupReport:
    playwright_package: str = UNKNOWN
    websockets_package: str = UNKNOWN
    chromium_path: str = ""
    chromium_kind: str = CHROMIUM_KIND_MISSING
    chromium_candidates: list[dict[str, str]] = field(default_factory=list)
    cdp_endpoint: str = ""
    cdp_reachable: str = UNKNOWN
    launchable: str = UNKNOWN
    backend_recommendation: str = ""
    profile_dir: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "playwright_package": self.playwright_package,
            "websockets_package": self.websockets_package,
            "chromium_path": self.chromium_path,
            "chromium_kind": self.chromium_kind,
            "chromium_candidates": self.chromium_candidates,
            "cdp_endpoint": self.cdp_endpoint,
            "cdp_reachable": self.cdp_reachable,
            "launchable": self.launchable,
            "backend_recommendation": self.backend_recommendation,
            "profile_dir": self.profile_dir,
            "detail": self.detail,
        }


def package_state(dist_name: str) -> str:
    import importlib.metadata

    try:
        importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return MISSING
    return READY


def classify_candidate(path: str) -> str:
    normalized = path.replace("\\", "/")
    if "ms-playwright" in normalized or "/chrome-linux" in normalized:
        return CHROMIUM_KIND_PLAYWRIGHT
    return CHROMIUM_KIND_SYSTEM


def detect_browser(
    environ: dict[str, str] | None = None, config: Config | None = None
) -> BrowserSetupReport:
    from eurus.termux import is_termux

    env = dict(environ) if environ is not None else None
    report = BrowserSetupReport()
    report.playwright_package = package_state("playwright")
    report.websockets_package = package_state("websockets")
    candidates = chromium_candidates()
    for candidate in candidates:
        exists = Path(candidate).is_file()
        report.chromium_candidates.append({"path": candidate, "exists": "yes" if exists else "no"})
    env_endpoint = (env or {}).get("EURUS_CDP_ENDPOINT", "").strip() if env else ""
    config_endpoint = config.browser.cdp_endpoint.strip() if config is not None else ""
    report.cdp_endpoint = env_endpoint or config_endpoint
    env_executable = (env or {}).get("EURUS_CHROMIUM", "").strip() if env else ""
    config_executable = config.browser.chromium_executable.strip() if config is not None else ""
    configured = env_executable or config_executable
    try:
        found = find_chromium(configured)
        report.chromium_path = found
        report.chromium_kind = classify_candidate(found)
        report.launchable = UNKNOWN
    except Exception:
        report.chromium_path = ""
        report.chromium_kind = CHROMIUM_KIND_MISSING
        report.launchable = BROKEN
    if report.cdp_endpoint:
        report.cdp_reachable, report.detail = probe_cdp_sync(report.cdp_endpoint)
    termux = is_termux(env)
    if report.chromium_kind == CHROMIUM_KIND_PLAYWRIGHT and report.playwright_package == READY:
        report.backend_recommendation = "playwright"
    elif report.chromium_kind == CHROMIUM_KIND_SYSTEM:
        if termux:
            report.backend_recommendation = (
                "cdp" if report.websockets_package == READY else "cdp (install browser-cdp extra)"
            )
        elif report.playwright_package == READY:
            report.backend_recommendation = "playwright (using system chromium)"
        elif report.websockets_package == READY:
            report.backend_recommendation = "cdp"
        else:
            report.backend_recommendation = "install playwright or websockets"
    elif report.playwright_package == READY:
        report.backend_recommendation = "playwright (run: python -m playwright install chromium)"
    elif report.websockets_package == READY:
        report.backend_recommendation = "cdp"
    else:
        report.backend_recommendation = "install the browser or browser-cdp extra"
    from eurus import paths

    report.profile_dir = str(paths.home_dir(env) / "browser" / "profile")
    return report


def probe_cdp_sync(endpoint: str, timeout: float = CDP_PROBE_TIMEOUT) -> tuple[str, str]:
    try:
        ok, detail = asyncio.run(probe_cdp(endpoint, timeout))
    except Exception as exc:
        return UNKNOWN, f"cdp probe error: {type(exc).__name__}: {exc}"
    return READY if ok else BROKEN, detail


def playwright_browsers_installed() -> bool:
    if not PLAYWRIGHT_CACHE.is_dir():
        return False
    try:
        return any(child.name.startswith("chromium-") for child in PLAYWRIGHT_CACHE.iterdir())
    except OSError:
        return False


async def probe_cdp(endpoint: str, timeout: float = CDP_PROBE_TIMEOUT) -> tuple[bool, str]:
    if not endpoint:
        return False, "no cdp endpoint configured"
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/json/version")
    except Exception as exc:
        return False, f"cdp probe failed: {type(exc).__name__}"
    if response.status_code == 200:
        payload = {}
        with contextlib.suppress(ValueError):
            payload = response.json()
        browser = str(payload.get("Browser", "unknown"))
        return True, f"cdp reachable: {browser}"
    return False, f"cdp replied with HTTP {response.status_code}"


def self_test_settings(
    environ: dict[str, str] | None = None, config: Config | None = None
) -> BrowserSettings:
    from eurus.browser.manager import BrowserSettings as _BrowserSettings

    env = environ or {}
    env_endpoint = env.get("EURUS_CDP_ENDPOINT", "").strip()
    config_endpoint = config.browser.cdp_endpoint.strip() if config is not None else ""
    env_executable = env.get("EURUS_CHROMIUM", "").strip()
    config_executable = config.browser.chromium_executable.strip() if config is not None else ""
    backend = config.browser.backend.strip().lower() if config is not None else "auto"
    return _BrowserSettings(
        enabled=True,
        backend=backend or "auto",
        cdp_endpoint=env_endpoint or config_endpoint,
        chromium_executable=env_executable or config_executable,
        headless=True,
        startup_timeout=BACKEND_START_TIMEOUT,
    )


async def launch_self_test(
    network: bool,
    environ: dict[str, str] | None = None,
    config: Config | None = None,
) -> tuple[bool, list[tuple[str, str]]]:
    from eurus.browser.manager import BrowserManager
    from eurus.errors import ToolError

    settings = self_test_settings(environ, config)
    manager = BrowserManager(settings)
    results: list[tuple[str, str]] = []
    try:
        try:
            await manager.backend.start()
        except (ToolError, Exception) as exc:
            results.append(("backend start", f"failed: {type(exc).__name__}: {exc}"))
            return False, results
        results.append(("backend start", "passed"))
        results.append(("backend name", manager.backend.name))
        if network:
            try:
                page = await manager.backend.open_page()
                outcome = await page.goto(NAVIGATION_TEST_URL, 20.0)
                results.append(
                    ("navigation", f"passed: HTTP {outcome.status} at {NAVIGATION_TEST_URL}")
                )
                await page.close()
            except Exception as exc:
                results.append(("navigation", f"failed: {type(exc).__name__}: {exc}"))
        else:
            results.append(("navigation", "skipped (offline mode)"))
        try:
            await manager.close_all()
            results.append(("cleanup", "passed"))
        except Exception as exc:
            results.append(("cleanup", f"failed: {type(exc).__name__}: {exc}"))
        ok = all(
            not name.startswith(("backend start", "cleanup")) or "passed" in detail
            for name, detail in results
        )
        return ok, results
    finally:
        with contextlib.suppress(Exception):
            await manager.close_all()


def run_self_test(
    network: bool,
    environ: dict[str, str] | None = None,
    config: Config | None = None,
) -> tuple[bool, list[tuple[str, str]]]:
    return asyncio.run(launch_self_test(network, environ, config))


def playwright_install_chromium(timeout: float = 600.0) -> tuple[int, str]:
    import sys

    if package_state("playwright") != READY:
        return 1, "playwright package is not installed"
    executable = sys.executable or "python3"
    return _run_playwright_install(executable, timeout)


def _run_playwright_install(executable: str, timeout: float) -> tuple[int, str]:
    import subprocess

    try:
        result = subprocess.run(
            [executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"playwright install failed: {type(exc).__name__}: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(output.splitlines()[-8:])
    return result.returncode, tail


def browser_check_result(report: BrowserSetupReport) -> CheckResult:
    if report.cdp_endpoint:
        if report.cdp_reachable == READY:
            detail = f"remote CDP {report.cdp_endpoint} operational ({report.detail})"
            return CheckResult(name="browser", status=READY, detail=detail, optional=True)
        if report.cdp_reachable == BROKEN:
            detail = f"remote CDP {report.cdp_endpoint}: {report.detail}"
            return CheckResult(
                name="browser",
                status=BROKEN,
                detail=detail,
                hint="check that the remote browser is running and the endpoint is correct",
                optional=True,
            )
        detail = f"remote CDP {report.cdp_endpoint} ({report.cdp_reachable})"
        return CheckResult(name="browser", status=UNKNOWN, detail=detail, optional=True)
    if report.chromium_kind == CHROMIUM_KIND_MISSING:
        status = MISSING
        detail = "no chromium executable found"
        hint = report.backend_recommendation
    elif report.launchable == BROKEN:
        status = BROKEN
        detail = f"chromium found at {report.chromium_path} but it could not launch"
        hint = report.backend_recommendation
    elif report.launchable == READY:
        status = READY
        detail = f"browser runtime operational ({report.chromium_kind}) at {report.chromium_path}"
        hint = ""
    else:
        status = UNKNOWN
        detail = (
            f"chromium ({report.chromium_kind}) at {report.chromium_path};"
            " launch not verified by this read-only check"
        )
        hint = "run: eurus setup browser to verify the backend"
    return CheckResult(name="browser", status=status, detail=detail, hint=hint, optional=True)
