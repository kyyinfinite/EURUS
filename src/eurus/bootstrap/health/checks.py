from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from typing import Any

from eurus.bootstrap.dependencies.inspector import inspect_all, inspect_core
from eurus.bootstrap.integrations.browser import browser_check_result, detect_browser
from eurus.bootstrap.integrations.mcp import inspect_mcp, mcp_check_result
from eurus.bootstrap.integrations.telegram import inspect_telegram, telegram_check_result
from eurus.bootstrap.models import (
    BLOCKED,
    BROKEN,
    CONFIG_UNSET,
    MISSING,
    OPTIONAL,
    OUTDATED,
    READY,
    UNKNOWN,
    CheckResult,
    ConfigUnset,
    HealthReport,
)
from eurus.catalog import resolve_api_key
from eurus.config import Config
from eurus.errors import ConfigError
from eurus.models import ModelRef

ENV_OVERRIDES_NAMES: tuple[str, ...] = (
    "EURUS_MODEL",
    "EURUS_SKILLS",
    "EURUS_FALLBACK",
    "EURUS_MAX_STEPS",
    "EURUS_TEMPERATURE",
    "EURUS_PERMISSION_MODE",
    "EURUS_WORKSPACE",
    "EURUS_LOG_LEVEL",
    "EURUS_LOG_FILE",
    "EURUS_LOG_JSON",
    "EURUS_TIMEOUT",
    "EURUS_MAX_RETRIES",
    "EURUS_LOCAL_ONLY",
    "EURUS_COOLDOWN",
    "EURUS_MAX_COOLDOWN",
    "EURUS_RETRY_JITTER",
    "EURUS_FIRST_TOKEN",
)


def check_platform(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.detection.platform import inspect_platform

    platform = inspect_platform(environ)
    if platform.variant == "unknown":
        return CheckResult(
            name="platform", status=UNKNOWN, detail="platform could not be classified"
        )
    detail = f"{platform.label} ({platform.system}, confidence: {platform.confidence})"
    return CheckResult(name="platform", status=READY, detail=detail)


def check_python(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.detection.python import inspect_python

    info = inspect_python(environ)
    if not info.supported:
        return CheckResult(
            name="python",
            status=BLOCKED,
            detail=f"python {info.version} at {info.executable}",
            hint="eurus requires python 3.11 or newer",
        )
    detail = f"python {info.version} at {info.executable}"
    return CheckResult(name="python", status=READY, detail=detail)


def check_venv(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.detection.python import detect_venv

    is_venv, path = detect_venv(environ)
    if is_venv:
        return CheckResult(
            name="virtualenv", status=READY, detail=f"active virtual environment at {path}"
        )
    return CheckResult(
        name="virtualenv",
        status=OPTIONAL,
        detail="running on the system interpreter",
        hint="a virtual environment keeps pip packages isolated (python -m venv .venv)",
        optional=True,
    )


def check_core_dependencies(environ: dict[str, str]) -> CheckResult:
    packages = inspect_core()
    problems = [item for item in packages.values() if item.status != READY]
    if not problems:
        return CheckResult(
            name="core dependencies",
            status=READY,
            detail=f"{len(packages)} core packages installed",
            repair_key="pip_install",
            repair_payload={"targets": []},
        )
    detail = "; ".join(f"{item.name}: {item.status}" for item in problems)
    return CheckResult(
        name="core dependencies",
        status=BLOCKED if any(item.status in {MISSING, BROKEN} for item in problems) else OUTDATED,
        detail=detail,
        hint="run: eurus bootstrap --repair",
        repair_key="pip_install",
        repair_payload={"targets": [item.required_spec for item in problems]},
    )


def check_optional_dependencies(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.dependencies.catalog import FEATURES, feature_status

    packages = inspect_all()
    statuses = [feature_status(spec, packages) for spec in FEATURES]
    ready = [item.feature for item in statuses if item.status == READY]
    partial = [item.feature for item in statuses if item.status in {MISSING, OUTDATED}]
    if not ready and not partial:
        return CheckResult(
            name="optional features",
            status=OPTIONAL,
            detail="no optional features installed",
            optional=True,
        )
    detail = f"ready: {', '.join(ready) or 'none'}"
    if partial:
        detail += f"; incomplete: {', '.join(partial)}"
    return CheckResult(
        name="optional features",
        status=READY if not partial else OPTIONAL,
        detail=detail,
        optional=True,
    )


def load_config_safe(environ: dict[str, str]) -> Config | None:
    from eurus.config import load_config

    try:
        return load_config(environ=environ)
    except ConfigError:
        return None
    except Exception:
        return None


def check_configuration(environ: dict[str, str]) -> CheckResult:
    from eurus import paths

    user_config = paths.user_config_path(environ)
    if not user_config.is_file():
        return CheckResult(
            name="configuration",
            status=MISSING,
            detail=f"{user_config} does not exist",
            hint="run: eurus bootstrap (Configuration Manager) or eurus config init",
            repair_key="config_init",
        )
    config = load_config_safe(environ)
    if config is not None:
        return CheckResult(
            name="configuration", status=READY, detail=f"valid configuration ({user_config})"
        )
    return CheckResult(
        name="configuration",
        status=BROKEN,
        detail=f"{user_config} is invalid",
        hint="fix the TOML errors reported by: eurus config show",
        repair_key="config_repair",
    )


def check_storage(environ: dict[str, str]) -> CheckResult:
    from eurus import paths
    from eurus.bootstrap.state.store import is_world_readable

    home = paths.home_dir(environ)
    if not home.is_dir():
        return CheckResult(
            name="storage",
            status=MISSING,
            detail=f"{home} does not exist (read-only check; nothing was created)",
            hint="run: eurus bootstrap --repair",
            repair_key="home_dirs",
        )
    if is_world_readable(home):
        return CheckResult(
            name="storage",
            status=BROKEN,
            detail=f"{home} is readable by other users",
            hint="tighten permissions to 0700",
            repair_key="env_perms_home",
        )
    if not os.access(home, os.W_OK):
        return CheckResult(
            name="storage",
            status=BLOCKED,
            detail=f"{home} is not writable by the current user",
            hint="check disk permissions",
        )
    return CheckResult(
        name="storage", status=READY, detail=f"{home} is writable (verified read-only)"
    )


def check_env_permissions(environ: dict[str, str]) -> CheckResult:
    from eurus import paths
    from eurus.bootstrap.state.store import is_world_readable

    env_path = paths.home_dir(environ) / ".env"
    if not env_path.is_file():
        return CheckResult(
            name="env permissions", status=OPTIONAL, detail="no global .env file yet", optional=True
        )
    if is_world_readable(env_path):
        return CheckResult(
            name="env permissions",
            status=BROKEN,
            detail=f"{env_path} is readable by other users",
            hint="tighten permissions to 0600",
            repair_key="env_perms",
        )
    return CheckResult(
        name="env permissions", status=READY, detail=f"{env_path} has 0600 permissions"
    )


def check_database(environ: dict[str, str]) -> CheckResult:
    from eurus.home import HomeManager

    home = HomeManager(environ)
    database = home.database_file if home.database_file.exists() else home.legacy_database_file
    if not database.exists():
        return CheckResult(
            name="database",
            status=OPTIONAL,
            detail="sessions database not created yet",
            optional=True,
        )
    try:
        connection = sqlite3.connect(database, timeout=5)
        try:
            row = connection.execute("PRAGMA integrity_check(1)").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return CheckResult(
            name="database", status=BROKEN, detail=f"{database}: {exc}", optional=True
        )
    result = str(row[0]) if row else "unknown"
    if result == "ok":
        return CheckResult(name="database", status=READY, detail=f"{database} integrity ok")
    return CheckResult(
        name="database", status=BROKEN, detail=f"{database}: {result}", optional=True
    )


def config_error_detail(environ: dict[str, str], detail: str = "") -> str:
    if detail:
        return detail
    from eurus.config import load_config

    try:
        load_config(environ=environ)
    except ConfigError as exc:
        return str(exc)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return "configuration could not be loaded for an unknown reason"


def check_provider(
    environ: dict[str, str], config: Config | None, config_error: str = ""
) -> CheckResult:
    if config is None:
        detail = config_error_detail(environ, config_error)
        return CheckResult(
            name="provider",
            status=BROKEN,
            detail=f"configuration could not be loaded: {detail}",
            optional=False,
        )
    if not config.agent.model:
        return CheckResult(
            name="provider",
            status=MISSING,
            detail="no default model configured",
            hint="run: eurus setup provider",
            optional=True,
            repair_key="provider_setup",
        )
    try:
        ref = ModelRef.parse(config.agent.model)
    except Exception:
        return CheckResult(
            name="provider",
            status=BROKEN,
            detail=f"invalid model ref: {config.agent.model}",
            optional=True,
        )
    spec = config.providers.get(ref.provider)
    if spec is None:
        return CheckResult(
            name="provider",
            status=BROKEN,
            detail=f"unknown provider '{ref.provider}'",
            optional=True,
        )
    key = resolve_api_key(spec, environ)
    if not key and spec.api_key_env:
        return CheckResult(
            name="provider",
            status=MISSING,
            detail=f"{spec.api_key_env} is not set for {config.agent.model}",
            hint="run: eurus setup provider",
            optional=True,
            repair_key="provider_setup",
        )
    fallback = ", ".join(config.agent.fallback) if config.agent.fallback else "none"
    masked = f"{key[:4]}…" if key else "no key required"
    return CheckResult(
        name="provider",
        status=READY,
        detail=f"{config.agent.model} (key: {masked}; fallback: {fallback})",
        optional=True,
    )


def check_browser(environ: dict[str, str], config: Config | None) -> CheckResult:
    report = detect_browser(environ, config=config)
    result = browser_check_result(report)
    return result


def check_mcp(environ: dict[str, str]) -> CheckResult:
    report = inspect_mcp(environ)
    return mcp_check_result(report)


def check_telegram(environ: dict[str, str]) -> CheckResult:
    report = inspect_telegram(environ)
    return telegram_check_result(report)


def check_startup(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.startup.manager import detect_mechanisms, status_all

    mechanisms = detect_mechanisms(environ)
    statuses = status_all(environ)
    installed = [item for item in statuses if item.installed and item.service != "termux-boot"]
    boot = next((item for item in statuses if item.service == "termux-boot"), None)
    detail = f"mechanism: {mechanisms.recommended}"
    if installed:
        detail += "; installed: " + ", ".join(
            f"{item.service} ({item.state})" for item in installed
        )
    else:
        detail += "; no services installed"
    if boot is not None and boot.installed:
        detail += "; termux boot script present"
    status = READY if installed or (boot is not None and boot.installed) else OPTIONAL
    return CheckResult(
        name="startup",
        status=status,
        detail=detail,
        hint="run: eurus setup startup",
        optional=True,
    )


def check_network(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.detection.capabilities import network_available

    state = network_available(environ)
    if state == "yes":
        return CheckResult(name="network", status=READY, detail="internet reachable")
    if state == "limited":
        return CheckResult(
            name="network", status=UNKNOWN, detail="dns resolved but no outbound connection"
        )
    if state == "local-only":
        return CheckResult(
            name="network",
            status=OPTIONAL,
            detail="EURUS_LOCAL_ONLY is set; network checks skipped",
            optional=True,
        )
    return CheckResult(
        name="network", status=MISSING, detail="no network connectivity", optional=True
    )


BASE_CHECKS: tuple[Callable[[dict[str, str]], CheckResult], ...] = (
    check_platform,
    check_python,
    check_venv,
    check_core_dependencies,
    check_optional_dependencies,
    check_configuration,
    check_storage,
    check_env_permissions,
    check_database,
    check_network,
)


def run_checks(
    environ: dict[str, str] | None = None,
    *,
    full: bool = False,
    config: Config | None | ConfigUnset = CONFIG_UNSET,
    config_error: str = "",
) -> HealthReport:
    env = dict(os.environ) if environ is None else dict(environ)
    results: list[CheckResult] = []
    for check in BASE_CHECKS:
        try:
            results.append(check(env))
        except Exception as exc:
            results.append(
                CheckResult(
                    name=check.__name__,
                    status=UNKNOWN,
                    detail=f"check failed: {type(exc).__name__}: {exc}",
                )
            )
    resolved: Config | None = load_config_safe(env) if isinstance(config, ConfigUnset) else config
    try:
        results.append(check_provider(env, resolved, config_error))
    except Exception as exc:
        results.append(
            CheckResult(
                name="provider", status=UNKNOWN, detail=f"check failed: {type(exc).__name__}: {exc}"
            )
        )
    deferred: tuple[tuple[str, Callable[[], CheckResult]], ...] = (
        ("check_browser", lambda: check_browser(env, resolved)),
        ("check_mcp", lambda: check_mcp(env)),
        ("check_telegram", lambda: check_telegram(env)),
        ("check_startup", lambda: check_startup(env)),
    )
    for name, run in deferred:
        try:
            results.append(run())
        except Exception as exc:
            results.append(
                CheckResult(
                    name=name,
                    status=UNKNOWN,
                    detail=f"check failed: {type(exc).__name__}: {exc}",
                )
            )
    if full:
        results.extend(full_checks(env, resolved))
    return HealthReport(results=results)


def full_checks(environ: dict[str, str], config: Config | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(api_connectivity_result(environ, config))
    results.append(browser_runtime_result(environ, config))
    results.append(mcp_connectivity_result(environ))
    return results


def api_connectivity_result(environ: dict[str, str], config: Config | None) -> CheckResult:
    if config is None:
        return CheckResult(
            name="api connectivity", status=UNKNOWN, detail="configuration invalid", optional=True
        )
    if not config.agent.model:
        return CheckResult(
            name="api connectivity", status=OPTIONAL, detail="no model configured", optional=True
        )
    import asyncio

    from eurus.diagnose import probe_model
    from eurus.errors import EurusError
    from eurus.models import ModelRef
    from eurus.providers.httpx_transport import HttpxTransport

    try:
        ref = ModelRef.parse(config.agent.model)
    except EurusError as exc:
        return CheckResult(name="api connectivity", status=BROKEN, detail=str(exc), optional=True)

    async def _probe() -> Any:
        transport = HttpxTransport(timeout=45.0)
        try:
            return await probe_model(config, ref, transport=transport, environ=environ)
        finally:
            await transport.aclose()

    try:
        probe = asyncio.run(_probe())
    except Exception as exc:
        return CheckResult(
            name="api connectivity",
            status=UNKNOWN,
            detail=f"probe failed: {type(exc).__name__}: {exc}",
            optional=True,
        )
    if probe.ok:
        return CheckResult(
            name="api connectivity",
            status=READY,
            detail=f"{probe.model}: {probe.detail}",
            optional=True,
        )
    return CheckResult(
        name="api connectivity",
        status=MISSING,
        detail=f"{probe.model}: {probe.detail}",
        optional=True,
    )


def browser_runtime_result(environ: dict[str, str], config: Config | None) -> CheckResult:
    from eurus.bootstrap.integrations.browser import run_self_test

    ok, rows = run_self_test(False, environ, config=config)
    detail = "; ".join(f"{name}: {text}" for name, text in rows)
    if ok:
        return CheckResult(name="browser runtime", status=READY, detail=detail, optional=True)
    return CheckResult(
        name="browser runtime",
        status=MISSING,
        detail=detail,
        hint="run: eurus setup browser",
        optional=True,
    )


def mcp_connectivity_result(environ: dict[str, str]) -> CheckResult:
    from eurus.bootstrap.integrations.mcp import test_mcp_servers

    reports = test_mcp_servers(environ)
    if not reports:
        return CheckResult(
            name="mcp connectivity", status=OPTIONAL, detail="no MCP servers to test", optional=True
        )
    failed = [item.name for item in reports if item.connection not in {READY}]
    detail = "; ".join(f"{item.name}: {item.connection} ({item.tools} tools)" for item in reports)
    if failed:
        return CheckResult(name="mcp connectivity", status=MISSING, detail=detail, optional=True)
    return CheckResult(name="mcp connectivity", status=READY, detail=detail, optional=True)
