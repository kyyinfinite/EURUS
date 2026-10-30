from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eurus.bootstrap.configuration.env_manager import EnvFile, global_env_path
from eurus.bootstrap.configuration.secrets import mask_secret, validate_telegram_token
from eurus.bootstrap.models import BROKEN, MISSING, OPTIONAL, READY, UNKNOWN, CheckResult
from eurus.logs import redact

TELEGRAM_API_BASE = "https://api.telegram.org"
TEST_TIMEOUT_SECONDS = 15.0
TELEGRAM_TOKEN_ENV = "EURUS_TELEGRAM_BOT_TOKEN"
ALLOWLIST_ENV = "EURUS_TELEGRAM_ALLOWED_USER_IDS"


@dataclass(slots=True)
class TelegramSetupReport:
    token_env: str = TELEGRAM_TOKEN_ENV
    token_present: bool = False
    token_valid_format: bool = False
    token_masked: str = ""
    allowlist: str = ""
    aiogram: str = UNKNOWN
    connection: str = UNKNOWN
    bot_username: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_env": self.token_env,
            "token_present": self.token_present,
            "token_valid_format": self.token_valid_format,
            "token_masked": self.token_masked,
            "allowlist": self.allowlist,
            "aiogram": self.aiogram,
            "connection": self.connection,
            "bot_username": self.bot_username,
        }


def inspect_telegram(environ: dict[str, str]) -> TelegramSetupReport:
    report = TelegramSetupReport()
    token = environ.get(TELEGRAM_TOKEN_ENV, "").strip()
    report.token_present = bool(token)
    report.token_valid_format = bool(token) and not validate_telegram_token(token)
    report.token_masked = mask_secret(token) if token else ""
    report.allowlist = environ.get(ALLOWLIST_ENV, "").strip()
    import importlib.metadata

    try:
        importlib.metadata.version("aiogram")
        report.aiogram = READY
    except importlib.metadata.PackageNotFoundError:
        report.aiogram = MISSING
    return report


async def _get_me(token: str) -> tuple[bool, str]:
    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, redact(f"network error: {type(exc).__name__}: {exc}", (token,))
    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            return False, "unexpected response"
        if payload.get("ok"):
            result = payload.get("result", {})
            username = str(result.get("username", ""))
            return True, f"bot @{username}" if username else "token accepted"
        return False, "telegram replied with ok=false"
    if response.status_code == 401:
        return False, "token rejected (401 unauthorized)"
    return False, f"HTTP {response.status_code}"


def test_telegram_token(token: str) -> tuple[bool, str]:
    return asyncio.run(_get_me(token))


def save_telegram_token(environ: dict[str, str], token: str) -> Path:
    path = global_env_path(environ)
    env = EnvFile.load(path)
    env.set(TELEGRAM_TOKEN_ENV, token.strip())
    env.save()
    environ[TELEGRAM_TOKEN_ENV] = token.strip()
    return path


def save_allowlist(environ: dict[str, str], value: str) -> Path:
    path = global_env_path(environ)
    normalized = ",".join(item.strip() for item in value.split(",") if item.strip())
    env = EnvFile.load(path)
    env.set(ALLOWLIST_ENV, normalized)
    env.save()
    environ[ALLOWLIST_ENV] = normalized
    return path


def telegram_check_result(report: TelegramSetupReport) -> CheckResult:
    if not report.token_present:
        return CheckResult(
            name="telegram",
            status=OPTIONAL if report.aiogram == READY else MISSING,
            detail="no bot token configured",
            hint="run: eurus setup telegram",
            optional=True,
        )
    if not report.token_valid_format:
        return CheckResult(
            name="telegram",
            status=BROKEN,
            detail=f"{TELEGRAM_TOKEN_ENV} has an unexpected format",
            hint="run: eurus setup telegram to re-enter the token",
            optional=True,
        )
    if report.aiogram != READY:
        return CheckResult(
            name="telegram",
            status=MISSING,
            detail="aiogram is not installed",
            hint="run: eurus bootstrap (Python packages) or pip install -e '.[telegram]'",
            optional=True,
        )
    detail = f"token configured ({report.token_masked})"
    if report.allowlist:
        detail += f"; allowlist={report.allowlist}"
    else:
        detail += "; no user allowlist set"
    return CheckResult(name="telegram", status=READY, detail=detail, optional=True)
