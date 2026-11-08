from __future__ import annotations

import pytest

from eurus.config import build_config
from eurus.errors import ConfigError
from eurus.research.config import ResearchConfig


def test_default_research_config() -> None:
    config = build_config({})
    assert config.research.enabled is True
    assert config.research.providers == ("ddgs",)
    assert config.research.default_mode == "standard"
    assert config.research.deep_max_rounds == 3
    assert config.research.browser_fallback is True


def test_research_section_is_parsed() -> None:
    config = build_config(
        {
            "research": {
                "enabled": False,
                "providers": ["ddgs", "brave", "tavily"],
                "deep_max_rounds": 5,
                "deep_deadline_seconds": 600.0,
                "browser_fetch_budget": 0,
                "cache_ttl_seconds": 60.0,
            }
        }
    )
    assert config.research.enabled is False
    assert config.research.providers == ("ddgs", "brave", "tavily")
    assert config.research.deep_max_rounds == 5
    assert config.research.deep_deadline_seconds == 600.0
    assert config.research.browser_fetch_budget == 0
    assert config.research.cache_ttl_seconds == 60.0


def test_research_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        build_config({"research": {"nonsense_key": True}})


def test_research_rejects_unknown_provider() -> None:
    with pytest.raises(ConfigError, match="unknown search provider"):
        build_config({"research": {"providers": ["ddgs", "bing"]}})


def test_research_rejects_out_of_range_values() -> None:
    with pytest.raises(ConfigError, match=r"\[research\]"):
        build_config({"research": {"deep_max_rounds": 99}})
    with pytest.raises(ConfigError, match=r"\[research\]"):
        build_config({"research": {"deep_deadline_seconds": 5.0}})


def test_research_settings_mapping() -> None:
    settings = ResearchConfig(
        enabled=True,
        providers=("ddgs", "brave"),
        deep_max_rounds=4,
        max_fetch_mb=5,
    )
    assert settings.providers == ("ddgs", "brave")
    assert settings.deep_max_rounds == 4
    assert settings.max_fetch_mb == 5
