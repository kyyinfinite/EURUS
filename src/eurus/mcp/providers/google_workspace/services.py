"""Persisted enable/disable state for Google Workspace services.

Stored in ``integrations/google-workspace.json`` under the Eurus home
directory, separately from the external MCP server configuration
(``mcp.json``), so existing configurations are untouched. All services ship
disabled; users enable them individually.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from eurus.mcp.providers.google_workspace.catalog import SERVICES

CONFIG_FILE_NAME = "google-workspace.json"
DEFAULT_TIMEOUT_SECONDS = 45.0


@dataclass(slots=True)
class ServiceSettings:
    enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(slots=True)
class GoogleWorkspaceSettings:
    services: dict[str, ServiceSettings] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def defaults(cls) -> GoogleWorkspaceSettings:
        return cls(
            services={service.service_id: ServiceSettings(enabled=False) for service in SERVICES}
        )

    @classmethod
    def load(cls, path: Path) -> GoogleWorkspaceSettings:
        settings = cls(path=path)
        settings.services = {service.service_id: ServiceSettings() for service in SERVICES}
        if not path.exists():
            return settings
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return settings
        if not isinstance(raw, dict):
            return settings
        services = raw.get("services")
        if isinstance(services, dict):
            for service_id, entry in services.items():
                if service_id not in settings.services or not isinstance(entry, dict):
                    continue
                current = settings.services[service_id]
                current.enabled = bool(entry.get("enabled", False))
                try:
                    timeout = float(entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
                except (TypeError, ValueError):
                    timeout = DEFAULT_TIMEOUT_SECONDS
                current.timeout_seconds = min(max(timeout, 1.0), 300.0)
        return settings

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("settings path is not configured")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": 1,
            "services": {
                service_id: {
                    "enabled": item.enabled,
                    "timeout_seconds": item.timeout_seconds,
                }
                for service_id, item in self.services.items()
            },
        }
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def is_enabled(self, service_id: str) -> bool:
        entry = self.services.get(service_id)
        return bool(entry and entry.enabled)

    def set_enabled(self, service_id: str, enabled: bool) -> None:
        if service_id not in self.services:
            from eurus.mcp.providers.google_workspace.catalog import service_by_id

            service_by_id(service_id)
        self.services[service_id].enabled = enabled

    def enabled_ids(self) -> list[str]:
        return [service.service_id for service in SERVICES if self.is_enabled(service.service_id)]


def settings_path(home: Path) -> Path:
    return home / "integrations" / CONFIG_FILE_NAME


__all__ = [
    "CONFIG_FILE_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "GoogleWorkspaceSettings",
    "ServiceSettings",
    "settings_path",
]
