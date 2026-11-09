from pathlib import Path

import pytest

from eurus.coding.office_plugin import OfficePlugin, office_plugin
from eurus.plugins import PluginManager
from eurus.tools.base import ToolRegistry


def test_office_plugin_manifest_and_gateway_tool() -> None:
    plugin = office_plugin(object(), ToolRegistry())
    tools = {tool.name: tool for tool in plugin.tools()}
    assert set(tools) == {"office_specialist"}
    assert all(tool.mutates and tool.requires_approval for tool in tools.values())
    assert plugin.manifest.plugin_id == "office-artifacts"
    assert {"office.production", "office.docx", "office.xlsx", "office.pptx"}.issubset(
        plugin.manifest.capabilities
    )


def test_office_plugin_can_be_enabled_and_disabled(tmp_path: Path) -> None:
    manager = PluginManager(tmp_path / "plugins.db", eurus_version="99.0.0")
    registry = ToolRegistry()
    plugin = OfficePlugin(object(), ToolRegistry())

    enabled = manager.enable(plugin, registry)
    assert enabled.enabled and enabled.healthy
    assert set(enabled.tools) == {"office_specialist"}
    assert set(enabled.tools).issubset(registry.names())

    disabled = manager.disable("office-artifacts")
    assert not disabled.enabled and disabled.healthy
    assert not set(enabled.tools).intersection(registry.names())
    assert manager.inspect("office-artifacts").enabled is False


def test_office_plugin_collides_with_existing_gateway(tmp_path: Path) -> None:
    from eurus.office.tools import office_specialist_tool

    registry = ToolRegistry([office_specialist_tool(object(), ToolRegistry())])
    manager = PluginManager(tmp_path / "plugins.db", eurus_version="99.0.0")
    with pytest.raises(ValueError, match="collision"):
        manager.enable(OfficePlugin(object(), ToolRegistry()), registry)
