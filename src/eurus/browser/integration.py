from __future__ import annotations

from pathlib import Path

from eurus.browser.manager import BrowserManager, BrowserSettings
from eurus.config import BrowserConfig
from eurus.tools.base import ToolRegistry

MANAGER_ATTRIBUTE = "_eurus_browser_manager"

BROWSER_TOOL_NAMES = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_select_option",
    "browser_hover",
    "browser_scroll",
    "browser_wait_for",
    "browser_back",
    "browser_tabs",
    "browser_handle_dialog",
    "browser_screenshot",
    "browser_upload",
    "browser_download",
    "browser_extract",
    "browser_read",
    "browser_close",
)


def registry_manager(registry: ToolRegistry) -> BrowserManager | None:
    manager = getattr(registry, MANAGER_ATTRIBUTE, None)
    return manager if isinstance(manager, BrowserManager) else None


def browser_manager_for(
    registry: ToolRegistry,
    settings: BrowserSettings,
    *,
    workspace: Path | None = None,
) -> BrowserManager:
    manager = registry_manager(registry)
    if manager is not None:
        if workspace is not None:
            manager.set_workspace(workspace)
        return manager
    manager = BrowserManager(settings, workspace=workspace)
    setattr(registry, MANAGER_ATTRIBUTE, manager)
    return manager


def ensure_browser_tools(
    registry: ToolRegistry,
    settings: BrowserConfig | BrowserSettings | None,
    *,
    workspace: Path | None = None,
) -> list[str]:
    if settings is None:
        return []
    if isinstance(settings, BrowserSettings):
        resolved = settings
    elif isinstance(settings, BrowserConfig):
        resolved = BrowserSettings.from_config(settings)
    else:
        resolved = BrowserSettings.from_config(settings)
    if not resolved.enabled:
        return []
    from eurus.browser.tools import browser_tools

    manager = browser_manager_for(registry, resolved, workspace=workspace)
    registered: list[str] = []
    for tool in browser_tools(manager):
        if tool.name not in registry:
            registry.register(tool)
            registered.append(tool.name)
    return registered


async def close_registry_browser(registry: ToolRegistry) -> None:
    manager = registry_manager(registry)
    if manager is not None:
        await manager.close_all()


def registry_has_browser_tools(registry: ToolRegistry) -> bool:
    return any(name in registry for name in BROWSER_TOOL_NAMES[:3])


__all__ = [
    "BROWSER_TOOL_NAMES",
    "MANAGER_ATTRIBUTE",
    "browser_manager_for",
    "close_registry_browser",
    "ensure_browser_tools",
    "registry_has_browser_tools",
    "registry_manager",
]
