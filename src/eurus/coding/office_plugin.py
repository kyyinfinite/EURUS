from __future__ import annotations

from eurus.office.config import OfficeConfig
from eurus.office.tools import office_specialist_tool
from eurus.plugins import PluginManifest
from eurus.providers.router import Router
from eurus.tools.base import Tool, ToolRegistry


class OfficePlugin:
    manifest = PluginManifest(
        plugin_id="office-artifacts",
        version="2.0.0",
        description=(
            "Exposes the Eurus Office Specialist gateway for delegated Word, Excel, and "
            "PowerPoint production with QA and revision loops."
        ),
        capabilities=("office.production", "office.docx", "office.xlsx", "office.pptx"),
    )

    def __init__(
        self,
        router: Router | None = None,
        parent_registry: ToolRegistry | None = None,
        config: OfficeConfig | None = None,
    ) -> None:
        self._router = router
        self._parent_registry = parent_registry
        self._config = config
        self._active = False

    def tools(self) -> tuple[Tool, ...]:
        if self._router is None or self._parent_registry is None:
            return ()
        return (office_specialist_tool(self._router, self._parent_registry, self._config),)

    def start(self, config: dict[str, str]) -> None:
        self._active = True

    def close(self) -> None:
        self._active = False


def office_plugin(
    router: Router | None = None,
    parent_registry: ToolRegistry | None = None,
    config: OfficeConfig | None = None,
) -> OfficePlugin:
    return OfficePlugin(router, parent_registry, config)
