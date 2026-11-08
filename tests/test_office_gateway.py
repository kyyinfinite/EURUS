from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.agent.factory import build_agent
from eurus.coding.document_tools import _document_create
from eurus.coding.office_plugin import office_plugin
from eurus.coding.tools import coding_tools
from eurus.config import Config, build_config
from eurus.errors import ToolError
from eurus.office.config import OfficeConfig
from eurus.office.tools import TOOL_NAME, office_specialist_tool, office_specialist_tools
from eurus.plugins import PluginManager
from eurus.tools.base import ToolContext, ToolRegistry

LEGACY_OFFICE_TOOLS = ("word_create", "excel_create", "powerpoint_create", "office_replace")


def test_coding_registry_has_no_direct_office_creation() -> None:
    names = {tool.name for tool in coding_tools()}
    for name in LEGACY_OFFICE_TOOLS:
        assert name not in names
    assert "document_team_review" not in names


def test_document_create_rejects_office_extensions(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path)
    for name in ("report.docx", "book.xlsx", "deck.pptx"):
        with pytest.raises(ToolError, match="office_specialist"):
            import asyncio

            asyncio.run(_document_create({"path": name, "content": "text"}, context))


def test_document_create_still_supports_text_formats(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path)

    import asyncio

    result = asyncio.run(
        _document_create({"path": "notes/brief.md", "content": "# Draft"}, context)
    )
    assert "brief.md" in result
    assert (tmp_path / "notes" / "brief.md").is_file()


def test_gateway_tool_registered_with_strict_contract() -> None:
    tool = office_specialist_tool(object(), ToolRegistry(), OfficeConfig())
    assert tool.name == TOOL_NAME
    assert tool.requires_approval
    assert tool.mutates
    assert not tool.parallel_safe
    assert tool.parameters["required"] == ["format", "objective"]
    assert tool.parameters["properties"]["format"]["enum"] == ["word", "excel", "powerpoint"]


def test_gateway_tools_list_helper() -> None:
    tools = office_specialist_tools(object(), ToolRegistry(), OfficeConfig())
    assert [tool.name for tool in tools] == [TOOL_NAME]


def test_gateway_rejects_unknown_format(tmp_path: Path) -> None:
    import asyncio

    from eurus.office.errors import OfficeError

    tool = office_specialist_tool(_FailingRouter(), ToolRegistry(), OfficeConfig())
    with pytest.raises(OfficeError, match="format must be"):
        asyncio.run(
            tool.handler(
                {"format": "pages", "objective": "Make something"},
                ToolContext(workspace=tmp_path),
            )
        )


class _FailingRouter:
    async def chat(self, messages, **kwargs):
        raise AssertionError("router must not be called for invalid arguments")


def test_gateway_disabled_by_config(tmp_path: Path) -> None:
    import asyncio

    from eurus.office.errors import OfficeError

    tool = office_specialist_tool(_FailingRouter(), ToolRegistry(), OfficeConfig(enabled=False))
    with pytest.raises(OfficeError, match="disabled"):
        asyncio.run(
            tool.handler(
                {"format": "word", "objective": "Make a report"},
                ToolContext(workspace=tmp_path),
            )
        )


def test_main_agent_factory_registers_only_the_unified_delegate(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path / "home"))
    config = build_config(
        {
            "agent": {"model": "openai/test-model"},
            "permissions": {"workspace": str(tmp_path)},
            "sandbox": {"backend": "host", "required": False},
        },
        {"OPENAI_API_KEY": "test-key"},
    )
    agent = build_agent(
        config,
        _StubRouter(),
        registry=ToolRegistry(),
        shell=False,
        verify=False,
        plan=False,
    )
    names = agent.registry.names()
    assert "delegate" in names
    assert TOOL_NAME not in names
    for name in LEGACY_OFFICE_TOOLS:
        assert name not in names
    assert "document_team_review" not in names
    assert "office_team_create" not in names
    delegate = agent.registry.get("delegate")
    assert delegate.parameters["properties"]["format"]["enum"] == ["word", "excel", "powerpoint"]


class _StubRouter:
    async def chat(self, messages, **kwargs):
        raise AssertionError("no model calls expected in this test")

    def aclose(self):
        return None


def test_office_plugin_exposes_gateway_when_configured(tmp_path: Path) -> None:
    plugin = office_plugin(_StubRouter(), ToolRegistry(), OfficeConfig())
    tools = plugin.tools()
    assert [tool.name for tool in tools] == [TOOL_NAME]
    assert plugin.manifest.plugin_id == "office-artifacts"
    assert "office.production" in plugin.manifest.capabilities


def test_office_plugin_unconfigured_returns_no_direct_tools() -> None:
    plugin = office_plugin()
    assert plugin.tools() == ()


def test_office_plugin_enables_in_manager(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = PluginManager(tmp_path / "plugins.db", eurus_version="99.0.0")
    plugin = office_plugin(_StubRouter(), ToolRegistry(), OfficeConfig())
    status = manager.enable(plugin, registry)
    assert status.enabled and status.healthy
    assert status.tools == (TOOL_NAME,)
    assert TOOL_NAME in registry.names()
    manager.disable("office-artifacts")
    assert TOOL_NAME not in registry.names()


def test_office_config_defaults_and_parsing() -> None:
    config = build_config({}, {})
    assert config.office.enabled is True
    assert config.office.max_revision_cycles == 3
    assert config.office.max_model_calls == 60
    parsed = build_config(
        {
            "office": {
                "enabled": False,
                "max_revision_cycles": 1,
                "max_model_calls": 20,
                "deadline_seconds": 600,
                "content_score_min": 80,
            }
        },
        {},
    )
    assert parsed.office.enabled is False
    assert parsed.office.max_revision_cycles == 1
    assert parsed.office.max_model_calls == 20
    assert parsed.office.deadline_seconds == 600.0
    assert parsed.office.content_score_min == 80.0
    from eurus.errors import ConfigError

    with pytest.raises(ConfigError):
        build_config({"office": {"max_revision_cycles": 99}}, {})
    with pytest.raises(ConfigError):
        build_config({"office": {"unknown_key": 1}}, {})


def test_config_to_dict_includes_office() -> None:
    config = Config()
    payload = config.to_dict()
    assert "office" in payload
    assert payload["office"]["max_revision_cycles"] == 3


def test_result_payload_shape_matches_contract() -> None:
    from eurus.office.models.review import FinalArtifactPackage
    from eurus.office.models.task import QualityScores

    package = FinalArtifactPackage(
        job_id="job-1",
        status="completed",
        format="docx",
        quality_status="passed",
        artifact_path="out.docx",
        revision_count=1,
        scores=QualityScores(content=90.0, visual=88.0, structural=97.0),
    )
    payload = json.loads(json.dumps(package.to_dict()))
    assert set(payload) >= {
        "job_id",
        "status",
        "format",
        "quality_status",
        "artifact_path",
        "preview_path",
        "qa_report_path",
        "revision_count",
        "summary",
        "warnings",
        "scores",
    }
