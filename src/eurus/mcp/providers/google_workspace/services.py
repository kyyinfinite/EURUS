from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.mcp.jsonio import atomic_write_text, read_json_dict
from eurus.mcp.providers.google_workspace.catalog import SERVICES

CONFIG_FILE_NAME = "google-workspace.json"
DEFAULT_TIMEOUT_SECONDS = 45.0
CONFIG_VERSION = 2

PRESETS = ("personal", "student", "workspace", "custom")
ACCESS_LEVELS = ("read", "standard", "full")
STARTUP_MODES = ("background", "lazy", "eager")
PUBLISH_STATUSES = ("testing", "production", "internal")

PRESET_SERVICES: dict[str, tuple[str, ...]] = {
    "personal": ("gmail", "drive", "calendar", "people"),
    "student": (
        "gmail",
        "drive",
        "docs",
        "sheets",
        "slides",
        "calendar",
        "classroom",
    ),
    "workspace": (
        "gmail",
        "drive",
        "docs",
        "sheets",
        "slides",
        "calendar",
        "chat",
        "people",
    ),
    "custom": (),
}


class GoogleConfigError(ValueError):
    pass


@dataclass(slots=True)
class ServiceSettings:
    enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(slots=True)
class GoogleWorkspaceSettings:
    services: dict[str, ServiceSettings] = field(default_factory=dict)
    path: Path | None = None
    preset: str = "personal"
    account: str = "auto"
    access: str = "standard"
    startup: str = "background"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    oauth_publish_status: str = "testing"
    oauth_redirect_port: int = 0
    tools_deny: list[str] = field(default_factory=list)
    load_warnings: list[str] = field(default_factory=list)

    @classmethod
    def defaults(cls) -> GoogleWorkspaceSettings:
        return cls(
            services={service.service_id: ServiceSettings(enabled=False) for service in SERVICES}
        )

    @classmethod
    def load(cls, path: Path) -> GoogleWorkspaceSettings:
        settings = cls.defaults()
        settings.path = path
        raw, warning = read_json_dict(path)
        if warning:
            settings.load_warnings.append(warning)
        if raw is None:
            return settings
        version = raw.get("version", 1)
        if version not in (1, 2):
            raise GoogleConfigError(
                f"google-workspace.json: unsupported 'version' {version!r} (expected 1 or 2)"
            )
        unknown = set(raw) - {
            "version",
            "services",
            "preset",
            "account",
            "access",
            "startup",
            "timeout",
            "oauth",
            "tools",
        }
        if unknown:
            raise GoogleConfigError(
                f"google-workspace.json: unknown key(s) {sorted(unknown)}; "
                "see docs/GOOGLE_OAUTH.md for the schema"
            )
        services = raw.get("services")
        if services is not None:
            if not isinstance(services, dict):
                raise GoogleConfigError("google-workspace.json: 'services' must be an object")
            for service_id, entry in services.items():
                if service_id not in settings.services:
                    known = ", ".join(sorted(settings.services))
                    raise GoogleConfigError(
                        f"google-workspace.json: unknown service '{service_id}' (known: {known})"
                    )
                if not isinstance(entry, dict):
                    raise GoogleConfigError(
                        f"google-workspace.json: services.{service_id} must be an object"
                    )
                extra = set(entry) - {"enabled", "timeout_seconds"}
                if extra:
                    raise GoogleConfigError(
                        f"google-workspace.json: services.{service_id} has unknown "
                        f"key(s) {sorted(extra)}"
                    )
                current = settings.services[service_id]
                enabled = entry.get("enabled", False)
                if not isinstance(enabled, bool):
                    raise GoogleConfigError(
                        f"google-workspace.json: services.{service_id}.enabled must be a boolean"
                    )
                current.enabled = enabled
                try:
                    timeout = float(entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
                except (TypeError, ValueError) as exc:
                    raise GoogleConfigError(
                        f"google-workspace.json: services.{service_id}.timeout_seconds "
                        "must be a number"
                    ) from exc
                if not 1.0 <= timeout <= 300.0:
                    raise GoogleConfigError(
                        f"google-workspace.json: services.{service_id}.timeout_seconds "
                        "must be between 1 and 300"
                    )
                current.timeout_seconds = timeout

        settings.preset = _choice(raw, "preset", PRESETS, settings.preset)
        settings.access = _choice(raw, "access", ACCESS_LEVELS, settings.access)
        settings.startup = _choice(raw, "startup", STARTUP_MODES, settings.startup)
        account = raw.get("account")
        if account is not None:
            if not isinstance(account, str) or not account.strip():
                raise GoogleConfigError(
                    "google-workspace.json: 'account' must be a non-empty string"
                )
            settings.account = account.strip()
        settings.timeout = _timeout(raw, settings.timeout)
        oauth = raw.get("oauth")
        if oauth is not None:
            if not isinstance(oauth, dict):
                raise GoogleConfigError("google-workspace.json: 'oauth' must be an object")
            extra = set(oauth) - {"publish_status", "redirect_port"}
            if extra:
                raise GoogleConfigError(
                    f"google-workspace.json: oauth has unknown key(s) {sorted(extra)}"
                )
            status = oauth.get("publish_status")
            if status is not None:
                if str(status) not in PUBLISH_STATUSES:
                    raise GoogleConfigError(
                        f"google-workspace.json: oauth.publish_status must be one of "
                        f"{PUBLISH_STATUSES}"
                    )
                settings.oauth_publish_status = str(status)
            port = oauth.get("redirect_port")
            if port is not None:
                try:
                    port_value = int(port)
                except (TypeError, ValueError) as exc:
                    raise GoogleConfigError(
                        "google-workspace.json: oauth.redirect_port must be an integer"
                    ) from exc
                if not 0 <= port_value <= 65535:
                    raise GoogleConfigError(
                        "google-workspace.json: oauth.redirect_port must be 0-65535"
                    )
                settings.oauth_redirect_port = port_value
        tools = raw.get("tools")
        if tools is not None:
            if not isinstance(tools, dict):
                raise GoogleConfigError("google-workspace.json: 'tools' must be an object")
            extra = set(tools) - {"deny"}
            if extra:
                raise GoogleConfigError(
                    f"google-workspace.json: tools has unknown key(s) {sorted(extra)}"
                )
            deny = tools.get("deny")
            if deny is not None:
                if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
                    raise GoogleConfigError(
                        "google-workspace.json: tools.deny must be a list of strings"
                    )
                settings.tools_deny = [item for item in deny if item.strip()]
        return settings

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("settings path is not configured")
        payload = {
            "version": CONFIG_VERSION,
            "preset": self.preset,
            "account": self.account,
            "access": self.access,
            "startup": self.startup,
            "timeout": self.timeout,
            "oauth": {
                "publish_status": self.oauth_publish_status,
                "redirect_port": self.oauth_redirect_port,
            },
            "tools": {"deny": list(self.tools_deny)},
            "services": {
                service_id: {
                    "enabled": item.enabled,
                    "timeout_seconds": item.timeout_seconds,
                }
                for service_id, item in self.services.items()
            },
        }
        atomic_write_text(target, json.dumps(payload, indent=2) + "\n")

    def is_enabled(self, service_id: str) -> bool:
        entry = self.services.get(service_id)
        return bool(entry and entry.enabled)

    def apply_preset(self, preset: str) -> list[str]:
        if preset not in PRESET_SERVICES:
            known = ", ".join(PRESETS)
            raise GoogleConfigError(f"unknown preset {preset!r} (known: {known})")
        self.preset = preset
        for service_id in PRESET_SERVICES[preset]:
            self.services[service_id].enabled = True
        return self.enabled_ids()

    def set_enabled(self, service_id: str, enabled: bool) -> None:
        if service_id not in self.services:
            from eurus.mcp.providers.google_workspace.catalog import service_by_id

            service_by_id(service_id)
        self.services[service_id].enabled = enabled

    def enabled_ids(self) -> list[str]:
        return [service.service_id for service in SERVICES if self.is_enabled(service.service_id)]

    def timeout_for(self, service_id: str) -> float:
        entry = self.services.get(service_id)
        return entry.timeout_seconds if entry else self.timeout


def _choice(raw: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = raw.get(key)
    if value is None:
        return default
    value = str(value)
    if value not in allowed:
        raise GoogleConfigError(
            f"google-workspace.json: '{key}' must be one of {allowed}, got {value!r}"
        )
    return value


def _timeout(raw: dict[str, Any], default: float) -> float:
    value = raw.get("timeout")
    if value is None:
        return default
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise GoogleConfigError("google-workspace.json: 'timeout' must be a number") from exc
    if not 1.0 <= timeout <= 300.0:
        raise GoogleConfigError("google-workspace.json: 'timeout' must be between 1 and 300")
    return timeout


def settings_path(home: Path) -> Path:
    return home / "integrations" / CONFIG_FILE_NAME


__all__ = [
    "ACCESS_LEVELS",
    "CONFIG_FILE_NAME",
    "CONFIG_VERSION",
    "DEFAULT_TIMEOUT_SECONDS",
    "GoogleConfigError",
    "GoogleWorkspaceSettings",
    "PRESET_SERVICES",
    "PUBLISH_STATUSES",
    "PRESETS",
    "ServiceSettings",
    "STARTUP_MODES",
    "settings_path",
]
