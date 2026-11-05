from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.bootstrap.models import BROKEN, MISSING, OPTIONAL, READY, UNKNOWN, CheckResult
from eurus.mcp.manager import MCPToolManager

MCP_FILE_NAME = "mcp.json"


@dataclass(slots=True)
class MCPServerReport:
    name: str
    transport: str
    enabled: bool
    target: str
    executable_found: str = UNKNOWN
    connection: str = UNKNOWN
    tools: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "target": self.target,
            "executable_found": self.executable_found,
            "connection": self.connection,
            "tools": self.tools,
            "detail": self.detail,
        }


@dataclass(slots=True)
class MCPSetupReport:
    path: str = ""
    valid: bool = True
    error: str = ""
    servers: list[MCPServerReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "valid": self.valid,
            "error": self.error,
            "servers": [item.to_dict() for item in self.servers],
        }


def mcp_config_path(environ: dict[str, str] | None = None) -> Path:
    from eurus import paths

    return paths.home_dir(environ) / MCP_FILE_NAME


def ensure_mcp_file(environ: dict[str, str] | None = None) -> Path:
    path = mcp_config_path(environ)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"servers": []}, indent=2) + "\n", encoding="utf-8")
    return path


def inspect_mcp(environ: dict[str, str] | None = None) -> MCPSetupReport:
    path = mcp_config_path(environ)
    report = MCPSetupReport(path=str(path))
    if not path.exists():
        report.valid = True
        report.error = ""
        return report
    try:
        manager = MCPToolManager.load(path)
    except (ValueError, OSError) as exc:
        report.valid = False
        report.error = str(exc)
        return report
    for name, config in manager.configs.items():
        item = MCPServerReport(
            name=name,
            transport=config.transport,
            enabled=config.enabled,
            target=config.command or config.url,
        )
        if config.transport == "stdio":
            resolved = shutil.which(config.command)
            if resolved or Path(config.command).is_file():
                item.executable_found = READY
            else:
                item.executable_found = MISSING
                item.detail = f"command not found: {config.command}"
        else:
            item.executable_found = READY if config.url.startswith("https://") else BROKEN
            if not config.url.startswith("https://"):
                item.detail = "http transport requires an https:// URL"
        report.servers.append(item)
    return report


async def _connect_all(path: Path) -> list[MCPServerReport]:
    try:
        manager = MCPToolManager.load(path)
    except (ValueError, OSError):
        return []
    reports: list[MCPServerReport] = []
    try:
        await manager.connect()
        status_items = manager.status()
    except Exception:
        status_items = []
    lookup = {str(item["name"]): item for item in status_items}
    for name, config in manager.configs.items():
        item = MCPServerReport(
            name=name,
            transport=config.transport,
            enabled=config.enabled,
            target=config.command or config.url,
        )
        entry = lookup.get(name)
        if entry is not None:
            item.connection = READY if entry["connected"] else BROKEN
            item.tools = int(entry["tools"])
            if not entry["connected"]:
                item.detail = str(entry.get("error") or "connection failed")
        else:
            item.connection = UNKNOWN
            item.detail = "not tested"
        reports.append(item)
    with contextlib.suppress(Exception):
        await manager.close()
    return reports


def test_mcp_servers(environ: dict[str, str] | None = None) -> list[MCPServerReport]:
    path = mcp_config_path(environ)
    if not path.exists():
        return []
    try:
        return asyncio.run(_connect_all(path))
    except Exception as exc:
        fallback = inspect_mcp(environ)
        for item in fallback.servers:
            item.connection = UNKNOWN
            item.detail = f"test failed: {type(exc).__name__}: {exc}"
        return fallback.servers


def mcp_check_result(report: MCPSetupReport) -> CheckResult:
    if not report.valid:
        return CheckResult(
            name="mcp",
            status=BROKEN,
            detail=f"invalid {report.path}: {report.error}",
            hint="run: eurus setup mcp (backs up and repairs the file)",
            optional=True,
            repair_key="mcp_reset",
        )
    if not report.servers:
        return CheckResult(
            name="mcp", status=OPTIONAL, detail="no MCP servers configured", optional=True
        )
    broken = [item.name for item in report.servers if item.executable_found not in {READY, UNKNOWN}]
    if broken:
        return CheckResult(
            name="mcp",
            status=BROKEN,
            detail=f"unavailable MCP servers: {', '.join(broken)}",
            hint="fix the command path or remove the server: eurus mcp remove <name>",
            optional=True,
        )
    return CheckResult(
        name="mcp",
        status=READY,
        detail=f"{len(report.servers)} server(s) configured",
        optional=True,
    )
